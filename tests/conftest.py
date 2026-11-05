"""Shared fixtures. Tests never read the gitignored profile/verticals.yaml —
the committed tests/fixtures/verticals.yaml (three synthetic verticals:
example_primary/secondary/tertiary) is injected into the src.shared.verticals
singleton for every test. Coverage of the real config lives in
tests/test_real_config_drift.py, which skips when that file is absent."""

from pathlib import Path

import pytest

from src.shared import verticals

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
def _no_git_hook_env(monkeypatch):
    """A commit hook runs this suite with GIT_DIR and GIT_INDEX_FILE set, and a
    test that makes a temp repo would then write to the real one (it set
    core.bare=true once). Every git call a test makes starts clean."""
    import os

    for name in [n for n in os.environ if n.startswith("GIT_")]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _isolate_applications(tmp_path, monkeypatch):
    """Keep run reports, post-submit evidence and the submit ledger out of the
    real `applications/`. Tests that need a specific folder still set their own."""
    from src import app_cli, apply_cli
    from src.shared import paths

    apps = tmp_path / "applications"
    monkeypatch.setattr(paths, "APPLICATIONS", apps)
    monkeypatch.setattr(app_cli, "APPLICATIONS", apps)
    monkeypatch.setattr(apply_cli, "APPLICATIONS", apps)
    monkeypatch.setattr(apply_cli, "APPLY_RUNS", apps / "apply_runs")


@pytest.fixture(autouse=True)
def _isolate_answer_bank(tmp_path, monkeypatch):
    """Keep the user's real answer bank out of every test: a lesson written by
    a test must never reach profile/application_answers.yaml."""
    from src.apply import answers, bank

    monkeypatch.setattr(answers, "DEFAULT_PATH", tmp_path / "no-bank.yaml")
    monkeypatch.setattr(bank, "DEFAULT_PATH", tmp_path / "no-bank.yaml")


@pytest.fixture(autouse=True)
def _isolate_discovery_state(tmp_path, monkeypatch):
    """Keep the jobs store, board state, board lists and inbox out of the real
    repo. Every discovery writer resolves these names at call time."""
    from src.discovery import digest, live_view, store
    from src.discovery.sources.boards import base as boards
    from src.discovery.sources import inbox

    monkeypatch.setattr(store, "STORE", tmp_path / "jobs" / "jobs.parquet")
    monkeypatch.setattr(live_view, "CLEAN", tmp_path / "jobs" / "clean.parquet")
    monkeypatch.setattr(boards, "BOARD_STATE", tmp_path / "jobs" / "boards.parquet")
    monkeypatch.setattr(digest, "BOARD_STATE", tmp_path / "jobs" / "boards.parquet")
    monkeypatch.setattr(digest, "JOBS_RUNS", tmp_path / "jobs" / "runs")
    monkeypatch.setattr(boards, "BOARDS_DIR", tmp_path / "universe")
    monkeypatch.setattr(inbox, "INBOX", tmp_path / "inbox")


@pytest.fixture(autouse=True)
def clear_location_cache():
    """parse_location is lru_cached, so a test that patches COUNTRY_NAMES /
    CITIES_BY_NAME would otherwise see (or leak) another test's entries."""
    from src.shared.location.parser import parse_location

    parse_location.cache_clear()
    yield
    parse_location.cache_clear()
