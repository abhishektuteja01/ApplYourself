"""Shared fixtures. Tests never read the gitignored profile/verticals.yaml —
the committed tests/fixtures/verticals.yaml (three synthetic verticals:
example_primary/secondary/tertiary) is injected into the src.verticals
singleton for every test. Coverage of the real config lives in
tests/test_real_config_drift.py, which skips when that file is absent."""

from pathlib import Path

import pytest

from src import verticals

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "verticals.yaml"
REAL_CONFIG = Path(__file__).resolve().parent.parent / "profile" / "verticals.yaml"


def pytest_terminal_summary(terminalreporter):
    """Not pytest_report_header: the header is suppressed by -q, and
    `uv run pytest tests -q` is the documented command."""
    if not REAL_CONFIG.is_file():
        terminalreporter.write_line(
            "WARNING: profile/verticals.yaml absent — test_real_config_drift.py "
            "skipped; the live config is unchecked.", yellow=True)


@pytest.fixture(autouse=True)
def cfg():
    config = verticals.load_verticals(FIXTURE_PATH)
    verticals.set_config(config)
    yield config
    verticals.set_config(None)


@pytest.fixture(autouse=True)
def _example_resume_sources(monkeypatch):
    """Resume sources come from the committed example world, never the
    gitignored profile/resume.yaml, bullets.md or skills_master.md."""
    from src import resume_model

    profile = Path(__file__).resolve().parent.parent / "profile"
    monkeypatch.setattr(resume_model, "RESUME", profile / "resume.example.yaml")
    monkeypatch.setattr(resume_model, "BULLETS", profile / "bullets.example.md")
    monkeypatch.setattr(resume_model, "SKILLS", profile / "skills_master.example.md")


@pytest.fixture(autouse=True)
def _isolate_applications(tmp_path, monkeypatch):
    """Keep run reports, post-submit evidence and the submit ledger out of the
    real `applications/`. Tests that need a specific folder still set their own."""
    from src import paths
    from src import apply_cli

    apps = tmp_path / "applications"
    monkeypatch.setattr(paths, "APPLICATIONS", apps)
    monkeypatch.setattr(apply_cli, "APPLICATIONS", apps)
    monkeypatch.setattr(apply_cli, "APPLY_RUNS", apps / "apply_runs")


@pytest.fixture(autouse=True)
def _isolate_learned_store(tmp_path, monkeypatch):
    """Keep the user's real `profile/.apply_learned.jsonl` out of every test.

    `load_answers` folds that store into `rules:` by default, so without this a
    record learned from one live board would silently change what the fixture
    boards resolve to — and the failure would land in whichever test happened
    to share a keyword. Tests that exercise the store pass an explicit path.
    """
    from src.apply import learned

    monkeypatch.setattr(learned, "DEFAULT_LEARNED_PATH", tmp_path / "no-store.jsonl")


@pytest.fixture(autouse=True)
def _isolate_discovery_state(tmp_path, monkeypatch):
    """Keep the jobs store, board state, board lists and inbox out of the real
    repo. Every discovery writer resolves these names at call time."""
    from src.discovery import store
    from src.discovery.sources import boards, inbox

    monkeypatch.setattr(store, "STORE", tmp_path / "jobs" / "jobs.parquet")
    monkeypatch.setattr(store, "CLEAN", tmp_path / "jobs" / "clean.parquet")
    monkeypatch.setattr(boards, "BOARD_STATE", tmp_path / "jobs" / "boards.parquet")
    monkeypatch.setattr(boards, "BOARDS_DIR", tmp_path / "universe")
    monkeypatch.setattr(inbox, "INBOX", tmp_path / "inbox")
