"""scripts/pii_scan.sh — the gate that keeps denylisted strings out of tracked files.

Each test builds a throwaway git repo and runs the script inside it. Files are only
staged, never committed: `git ls-files` reads the index, so the scan sees them
without needing a commit or a git identity.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "pii_scan.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="pii_scan.sh needs git and bash",
)


def _repo(tmp_path, tracked, denylist="Quimby\n"):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for name, text in tracked.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        subprocess.run(["git", "add", "-f", name], cwd=tmp_path, check=True)
    if denylist is not None:
        (tmp_path / "profile").mkdir(exist_ok=True)
        (tmp_path / "profile" / "pii_denylist.txt").write_text(denylist)
    return tmp_path


def _scan(repo):
    return subprocess.run(
        ["bash", str(SCRIPT)], cwd=repo, capture_output=True, text=True
    )


def test_clean_repo_passes(tmp_path):
    result = _scan(_repo(tmp_path, {"README.md": "nothing personal here\n"}))
    assert result.returncode == 0
    assert "OK" in result.stdout


def test_hit_in_tracked_file_fails(tmp_path):
    result = _scan(_repo(tmp_path, {"docs.md": "written by Quimby\n"}))
    assert result.returncode == 1
    assert "docs.md" in result.stderr


def test_license_is_allowlisted(tmp_path):
    """MIT attribution puts the real name in a tracked file on purpose."""
    result = _scan(_repo(tmp_path, {"LICENSE": "Copyright (c) 2026 Quimby\n"}))
    assert result.returncode == 0


def test_vendored_universe_csv_is_allowlisted(tmp_path):
    result = _scan(_repo(tmp_path, {"data/universe/ashby.csv": "Quimby,x\n"}))
    assert result.returncode == 0


def test_untracked_file_is_not_scanned(tmp_path):
    repo = _repo(tmp_path, {"README.md": "clean\n"})
    (repo / "scratch.md").write_text("Quimby\n")
    assert _scan(repo).returncode == 0


def test_word_boundary_prevents_substring_false_positives(tmp_path):
    """The shape of the false positives seen for real: a short abbreviation, and a
    company prefix, both sitting inside longer innocent words. Illustrations here are
    fictional on purpose — real patterns in this file would trip the scan itself."""
    repo = _repo(
        tmp_path,
        {"data.csv": "nebulous,Corvair,approval\n"},
        denylist="NEB\nCORV\n",
    )
    assert _scan(repo).returncode == 0


def test_missing_denylist_skips_without_failing(tmp_path):
    """A fresh clone has no denylist; the gate must not block on that alone."""
    result = _scan(_repo(tmp_path, {"README.md": "clean\n"}, denylist=None))
    assert result.returncode == 0
    assert "pii_denylist.txt" in result.stdout


def test_denylist_with_only_comments_is_an_error(tmp_path):
    """Refuse to report a clean scan that never ran."""
    result = _scan(
        _repo(tmp_path, {"README.md": "clean\n"}, denylist="# just a comment\n\n")
    )
    assert result.returncode == 2
