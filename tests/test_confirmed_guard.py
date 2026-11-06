"""`.claude/hooks/confirmed_guard.sh`: a session can read a `.confirmed` file
but never write one, since that file is the user's approval."""

import json
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "confirmed_guard.sh"
F = "applications/x/v1/answers_override.json.confirmed"


def _run(tool_input: dict) -> int:
    payload = json.dumps({"tool_name": "Bash", "tool_input": tool_input})
    return subprocess.run(
        ["bash", str(HOOK)], input=payload, text=True, encoding="utf-8", capture_output=True
    ).returncode


@pytest.mark.parametrize(
    "command",
    [
        f"echo abc > {F}",
        f"printf x >>'{F}'",
        f"echo abc | tee -a {F}",
        f"cp /tmp/h {F}",
        f"mv /tmp/h {F}; ls",
        f"touch {F}",
        f"sed -i '' s/a/b/ {F}",
        f"python3 -c \"open('{F}', 'w').write('h')\"",
        f"uv run python -c \"from pathlib import Path; Path('{F}').write_text('h')\"",
        "uv run python -c 'from src.apply import confirmation; confirmation.record(p, d)'",
        "uv run python -c 'p.with_name(p.name + SIDECAR_SUFFIX).write_text(h)'",
    ],
)
def test_writes_are_blocked(command):
    assert _run({"command": command}) == 2


@pytest.mark.parametrize(
    "command",
    [
        f"cat {F}",
        f"cp {F} /tmp/backup",
        f"cmp {F} /tmp/other 2>/dev/null",
        f"python3 -c \"print(open('{F}').read())\"",
        "uv run apply confirm applications/x/v1/answers_override.json",
        "echo hi > notes.txt",
    ],
)
def test_reads_and_unrelated_commands_pass(command):
    assert _run({"command": command}) == 0


def test_the_file_tools_are_blocked_on_a_confirmed_file():
    assert _run({"file_path": f"/repo/{F}", "content": "h"}) == 2
    assert _run({"file_path": "/repo/applications/x/v1/answers_override.json"}) == 0
