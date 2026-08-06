"""scripts/pii_scan.sh — the gate that keeps denylisted strings out of tracked files.

Each test builds a throwaway git repo and runs the script inside it. Files are only
staged, never committed: `git ls-files` reads the index, so the scan sees them
without needing a commit or a git identity.
"""

import os
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
        path.write_text(text, encoding="utf-8")
        subprocess.run(["git", "add", "-f", name], cwd=tmp_path, check=True)
    if denylist is not None:
        (tmp_path / "profile").mkdir(exist_ok=True)
        (tmp_path / "profile" / "pii_denylist.txt").write_text(denylist, encoding="utf-8")
    return tmp_path


def _scan(repo):
    return subprocess.run(
        ["bash", str(SCRIPT)], cwd=repo, capture_output=True, text=True,
        encoding="utf-8",
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
    (repo / "scratch.md").write_text("Quimby\n", encoding="utf-8")
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


def test_missing_denylist_fails_closed(tmp_path):
    """A missing or renamed denylist used to exit 0 — a green gate that never
    ran, which is the worst outcome available to a gate."""
    result = _scan(_repo(tmp_path, {"README.md": "clean\n"}, denylist=None))
    assert result.returncode == 2
    assert "pii_denylist.txt" in result.stderr


def test_missing_denylist_can_be_opted_out_of_explicitly(tmp_path):
    repo = _repo(tmp_path, {"README.md": "clean\n"}, denylist=None)
    result = subprocess.run(
        ["bash", str(SCRIPT)], cwd=repo, capture_output=True, text=True,
        encoding="utf-8", env={**os.environ, "PII_SCAN_ALLOW_MISSING": "1"},
    )
    assert result.returncode == 0


def test_tilde_prefix_matches_inside_a_longer_word(tmp_path):
    """-w is why a handle pattern misses the handle with a suffix — the exact
    case the real denylist had to special-case by hand."""
    repo = _repo(tmp_path, {"README.md": "see quimby01 for details\n"},
                 denylist="~quimby\n")
    assert _scan(repo).returncode == 1


def test_binary_file_is_refused_not_silently_skipped(tmp_path):
    """grep -I cannot read a .docx, so it used to count as scanned and clean
    while carrying a whole resume."""
    repo = _repo(tmp_path, {"README.md": "clean\n"})
    (repo / "resume.docx").write_bytes(b"PK\x03\x04\x00binary")
    subprocess.run(["git", "add", "-f", "resume.docx"], cwd=repo, check=True)
    result = _scan(repo)
    assert result.returncode == 1
    assert "resume.docx" in result.stderr


def test_shipped_example_denylist_scans_this_repo_clean(tmp_path):
    """README tells a new user to copy the example and run the gate. That
    failed: every pattern matched its own line in the tracked template, and two
    collided with real source. Nothing caught it, because no test ever ran the
    shipped example against the real tree.

    Runs against a throwaway copy of HEAD, never the real profile/ — replacing
    the developer's own denylist mid-test would quietly weaken the gate.
    """
    repo_root = SCRIPT.parent.parent
    clone = tmp_path / "clone"
    clone.mkdir()
    # The working tree's tracked files, not HEAD: the point is whether the
    # example ships clean against the repo as it stands.
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=repo_root,
                            capture_output=True, check=True).stdout
    for raw in listed.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode()
        src = repo_root / rel
        if not src.is_file():
            continue
        dest = clone / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    subprocess.run(["git", "init", "-q"], cwd=clone, check=True)
    subprocess.run(["git", "add", "-A"], cwd=clone, check=True)
    shutil.copyfile(clone / "profile" / "pii_denylist.example.txt",
                    clone / "profile" / "pii_denylist.txt")
    result = _scan(clone)
    assert result.returncode == 0, result.stdout + result.stderr


def test_denylist_with_only_comments_is_an_error(tmp_path):
    """Refuse to report a clean scan that never ran."""
    result = _scan(
        _repo(tmp_path, {"README.md": "clean\n"}, denylist="# just a comment\n\n")
    )
    assert result.returncode == 2
