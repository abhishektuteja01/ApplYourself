"""`.claude/hooks/confirmed_guard.sh`: a session can read a `.confirmed` file
but never write one, since that file is the user's approval."""

import json
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "confirmed_guard.sh"
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
        "uv run python -c 'from src.apply.confirmation import digest; print(digest(d))'",
        "python3 -c 'import hashlib, json; hashlib.sha256(json.dumps(d, sort_keys=True))'",
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


SUBMIT_ASK = HOOK.parent / "submit_ask.sh"


@pytest.mark.parametrize(
    ("command", "asks"),
    [
        ("uv run apply run --job-id x --submit", True),
        ("uv run --with rich apply run --limit 1 --submit --yes", True),
        ("uv run python -m src.apply_cli run --submit", True),
        ("uv run apply run --job-id x", False),
        ("uv run apply plan x --json", False),
        ("git commit -m 'no --submit-ish flag here'", False),
    ],
)
def test_submit_ask_asks_on_any_spelling_of_apply_submit(command, asks):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    out = subprocess.run(
        ["bash", str(SUBMIT_ASK)], input=payload, text=True, encoding="utf-8", capture_output=True
    )
    assert out.returncode == 0
    assert ('"permissionDecision": "ask"' in out.stdout) is asks
