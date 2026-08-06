""".githooks/pre-push — the half of the PII gate that reads commits, not files.

pii_scan.sh scans file CONTENT in the index. Commit metadata — the message, the
author and the committer — lives in no file, so nothing in that scan can see it.
That is how an address present in no file at any commit reached a public repo:
it was only ever in the author field, which GitHub renders on every commit page.

Each test builds a throwaway repo, commits into it with a chosen identity, and
feeds the hook the ref line git would send on stdin:
    <local-ref> <local-sha> <remote-ref> <remote-sha>
"""

import itertools
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / ".githooks" / "pre-push"
PII_SCAN = REPO_ROOT / "scripts" / "pii_scan.sh"
ZERO = "0" * 40

_counter = itertools.count()

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="the hook needs git and bash",
)


def _git(repo, *args, **kw):
    return subprocess.run(
        ["git", *args], cwd=repo, check=kw.pop("check", True),
        capture_output=True, text=True, encoding="utf-8", **kw
    )


def _repo(tmp_path, denylist="Quimby\nquimby@example.com\n"):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Safe Name")
    _git(tmp_path, "config", "user.email", "safe@users.noreply.github.com")
    (tmp_path / "profile").mkdir(exist_ok=True)
    (tmp_path / "profile" / "pii_denylist.txt").write_text(denylist, encoding="utf-8")
    # The hook shells out to the repo's own copy of the file scanner, so the
    # throwaway repo needs one. Copying the real script keeps this honest: a
    # change that breaks pii_scan.sh breaks these tests too.
    (tmp_path / "scripts").mkdir(exist_ok=True)
    shutil.copy(PII_SCAN, tmp_path / "scripts" / "pii_scan.sh")
    _git(tmp_path, "add", "-f", "scripts/pii_scan.sh")
    (tmp_path / "README.md").write_text("nothing personal\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    return tmp_path


def _commit(repo, message, *, email=None, name=None):
    # Unique content per commit, or git refuses an empty commit.
    (repo / "f.txt").write_text(f"line {next(_counter)}\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    env = dict(os.environ)
    if email:
        env |= {"GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_EMAIL": email}
    if name:
        env |= {"GIT_AUTHOR_NAME": name, "GIT_COMMITTER_NAME": name}
    _git(repo, "commit", "-q", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _push(repo, local_sha, remote_sha=ZERO):
    line = f"refs/heads/master {local_sha} refs/heads/master {remote_sha}\n"
    return subprocess.run(
        ["bash", str(HOOK), "origin", "git@example.com:x/y.git"],
        cwd=repo, input=line, capture_output=True, text=True, encoding="utf-8",
    )


def test_clean_history_passes(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs: a perfectly ordinary commit")
    result = _push(repo, sha)
    assert result.returncode == 0, result.stderr


def test_denylisted_author_email_is_caught(tmp_path):
    """The exact failure that reached production: an address in no file, ever."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs: innocuous message", email="quimby@example.com")
    result = _push(repo, sha)
    assert result.returncode == 1
    assert "author name/email" in result.stderr


def test_denylisted_committer_is_caught(tmp_path):
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs: innocuous", email="quimby@example.com")
    result = _push(repo, sha)
    assert "committer name/email" in result.stderr


def test_denylisted_commit_message_is_caught(tmp_path):
    """-G reads diffs only, so a message hit passes it untouched."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs: thanks to Quimby for the review")
    result = _push(repo, sha)
    assert result.returncode == 1
    assert "commit message" in result.stderr


def test_denylisted_config_email_is_caught_before_any_range(tmp_path):
    """The root cause, not the symptom: a denylisted user.email stamps every
    future commit. Caught even when the history itself is still clean."""
    repo = _repo(tmp_path)
    sha = _commit(repo, "docs: fine")
    _git(repo, "config", "user.email", "quimby@example.com")
    result = _push(repo, sha)
    assert result.returncode == 1
    assert "user.email is denylisted" in result.stderr


def test_branch_deletion_is_skipped(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "docs: fine")
    result = _push(repo, ZERO, remote_sha=ZERO)
    assert result.returncode == 0, result.stderr


def test_only_the_pushed_range_is_scanned(tmp_path):
    """A bad commit already on the remote is not re-reported: the gate is about
    what this push publishes, or it becomes noise that gets bypassed."""
    repo = _repo(tmp_path)
    bad = _commit(repo, "docs: thanks Quimby")
    good = _commit(repo, "docs: ordinary")
    result = _push(repo, good, remote_sha=bad)
    assert result.returncode == 0, result.stderr
