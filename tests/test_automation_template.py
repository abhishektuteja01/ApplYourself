"""The nightly-discovery LaunchAgent template (macOS, optional).

A plist with a typo does not error — launchd just never runs the job, and the
symptom is an empty shortlist nobody attributes to the scheduler. These tests
cover the parts that fail silently.
"""
from __future__ import annotations

import plistlib
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLIST = REPO_ROOT / "scripts" / "launchagent.example.plist"
WRAPPER = REPO_ROOT / "scripts" / "nightly_discovery.sh"

PLACEHOLDERS = {"__LABEL__": "com.example.applyourself.discovery", "__REPO__": "/tmp/repo"}


def _filled() -> dict:
    raw = PLIST.read_text(encoding="utf-8")
    for token, value in PLACEHOLDERS.items():
        raw = raw.replace(token, value)
    return plistlib.loads(raw.encode("utf-8"))


class TestPlist:
    def test_parses_once_placeholders_are_filled(self):
        assert _filled()["Label"] == PLACEHOLDERS["__LABEL__"]

    def test_declares_no_placeholder_beyond_the_documented_two(self):
        found = set(re.findall(r"__[A-Z_]+__", PLIST.read_text(encoding="utf-8")))
        assert found == set(PLACEHOLDERS), f"undocumented placeholders: {found}"

    def test_no_placeholder_survives_substitution(self):
        assert "__" not in str(_filled())

    def test_points_at_the_committed_wrapper(self):
        args = _filled()["ProgramArguments"]
        assert args[0] == "/bin/bash"
        assert args[1].endswith("/scripts/nightly_discovery.sh")

    def test_runs_before_the_workday_and_after_the_pmset_wake(self):
        """The wake is scheduled at 01:55, so the job must be later than that
        and still overnight."""
        when = _filled()["StartCalendarInterval"]
        assert when["Hour"] == 2 and when["Minute"] == 0

    def test_documents_the_pmset_wake(self):
        """Without a scheduled wake, a job whose time falls during sleep is
        skipped rather than deferred."""
        text = PLIST.read_text(encoding="utf-8")
        assert "pmset repeat wakeorpoweron" in text

    def test_logs_inside_the_repo(self):
        d = _filled()
        for key in ("StandardOutPath", "StandardErrorPath"):
            assert d[key].startswith(PLACEHOLDERS["__REPO__"] + "/logs/")

    def test_the_log_directory_is_committed(self):
        """launchd does not create the intermediate directory for
        StandardOutPath. Without a committed logs/, the agent fails to spawn on a
        fresh clone and produces no log explaining why."""
        gitkeep = REPO_ROOT / "logs" / ".gitkeep"
        assert gitkeep.exists(), "logs/.gitkeep missing"
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "logs/.gitkeep"],
            cwd=REPO_ROOT,
            capture_output=True,
            encoding="utf-8",
        )
        assert tracked.returncode == 0, "logs/.gitkeep exists but is not tracked"

    def test_actual_log_files_stay_ignored(self):
        """The directory ships; its contents must not. A discovery log names
        companies and search terms."""
        check = subprocess.run(
            ["git", "check-ignore", "logs/discovery_2026-01-01_000000.log"],
            cwd=REPO_ROOT,
            capture_output=True,
            encoding="utf-8",
        )
        assert check.returncode == 0, "log files are no longer gitignored"


class TestWrapper:
    def test_is_executable(self):
        assert WRAPPER.stat().st_mode & 0o111, f"{WRAPPER} is not executable"

    def test_is_valid_bash(self):
        result = subprocess.run(
            ["bash", "-n", str(WRAPPER)],
            capture_output=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stderr

    def test_needs_no_editing(self):
        assert "__" not in WRAPPER.read_text(encoding="utf-8")

    def test_hardens_path_for_launchd(self):
        """launchd's minimal PATH excludes every common uv location, so an
        unhardened wrapper dies with 'uv: command not found'."""
        text = WRAPPER.read_text(encoding="utf-8")
        assert "/opt/homebrew/bin" in text and "/usr/local/bin" in text
        assert "exit 127" in text, "must fail loudly when uv is absent"

    def test_holds_sleep_off_for_the_run(self):
        assert "caffeinate" in WRAPPER.read_text(encoding="utf-8")

    def test_runs_only_the_deterministic_step(self):
        """R7: no LLM in an unattended job. Scoring is a slash command, so it
        must not appear in anything the wrapper executes. Comments may mention
        it, hence the comment strip."""
        code = "\n".join(
            line
            for line in WRAPPER.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        ).lower()
        assert "run discover" in code
        assert "claude" not in code
        assert "/score" not in code
