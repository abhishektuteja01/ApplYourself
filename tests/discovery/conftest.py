"""Shared fixtures. Tests never read the gitignored profile/verticals.yaml —
the committed tests/discovery/fixtures/verticals.yaml (three synthetic
verticals, a byte-identical mirror of tests/fixtures/verticals.yaml) is
injected into the src.verticals singleton for every test."""

from pathlib import Path

import pytest

from src import verticals
from src.discovery import inbox, universe

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "verticals.yaml"


@pytest.fixture(autouse=True)
def cfg():
    config = verticals.load_verticals(FIXTURE_PATH)
    verticals.set_config(config)
    yield config
    verticals.set_config(None)


@pytest.fixture(autouse=True)
def isolate_universe_paths(monkeypatch, tmp_path):
    """universe.py resolves its ledger/CSV/watchlist paths from module-level
    absolutes under the repo root, so any test reaching update_health() writes
    into the real jobs/universe_health_<ats>.parquet — a fake slug there inflates
    the run report's pruned count, and a fixture slug colliding with a real one
    would bench a live board for 14 days. Redirect all three for every test;
    tests that patch them explicitly still win.

    HEALTH_DIR, not the per-ATS paths: health_path() derives from it, so one
    patch covers all three ledgers."""
    monkeypatch.setattr(universe, "HEALTH_DIR", tmp_path)
    monkeypatch.setattr(universe, "CSV_DIR", tmp_path / "universe_csv")
    monkeypatch.setattr(universe, "DEFAULT_COMPANIES_PATH", tmp_path / "companies.yaml")


@pytest.fixture(autouse=True)
def isolate_inbox_path(monkeypatch, tmp_path):
    """ingest_inbox() defaults to the repo-root inbox/ and *moves* what it reads
    into .processed/. Any test reaching InboxSource.fetch() would consume a real
    pending clip and discard its row into a tmp shard. Redirect for every test;
    tests that patch it explicitly still win."""
    monkeypatch.setattr(inbox, "INBOX", tmp_path / "inbox")
