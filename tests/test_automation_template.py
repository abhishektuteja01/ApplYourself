"""The nightly LaunchAgent template and the scripts it starts.

A plist with a typo does not error — launchd just never runs the job. These
tests cover the parts that fail silently.
"""
from __future__ import annotations

import plistlib
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLIST = REPO_ROOT / "scripts" / "launchagent.example.plist"
SCRIPTS = sorted((REPO_ROOT / "scripts").rglob("*.sh"))

PLACEHOLDERS = {"__LABEL__": "com.example.applyourself.discovery", "__REPO__": "/tmp/repo"}


def _filled() -> dict:
    raw = PLIST.read_text(encoding="utf-8")
    for token, value in PLACEHOLDERS.items():
        raw = raw.replace(token, value)
    return plistlib.loads(raw.encode("utf-8"))


def test_plist_parses_with_only_the_documented_placeholders():
    found = set(re.findall(r"__[A-Z_]+__", PLIST.read_text(encoding="utf-8")))
    assert found == set(PLACEHOLDERS), f"undocumented placeholders: {found}"
    assert _filled()["Label"] == PLACEHOLDERS["__LABEL__"]


def test_plist_starts_the_nightly_pipeline_script():
    args = _filled()["ProgramArguments"]
    assert args[0] == "/bin/bash"
    script = REPO_ROOT / args[1].removeprefix(PLACEHOLDERS["__REPO__"] + "/")
    assert script.name == "nightly_pipeline.sh"
    assert script.is_file() and script.stat().st_mode & 0o111, f"{script} missing or not executable"


def test_the_log_directory_is_committed():
    """launchd does not create StandardOutPath's directory; without a committed
    logs/, the agent fails to spawn on a fresh clone with no log saying why."""
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", "logs/.gitkeep"],
                             cwd=REPO_ROOT, capture_output=True, encoding="utf-8")
    assert tracked.returncode == 0, "logs/.gitkeep is not tracked"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_every_script_is_valid_bash(script):
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
