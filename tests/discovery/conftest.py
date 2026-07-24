"""Shared fixtures. Tests never read the gitignored profile/verticals.yaml —
the committed tests/fixtures/verticals.yaml (the real configured verticals,
kept content-identical to the live config) is injected into the
src.verticals singleton for every test."""

from pathlib import Path

import pytest

from src import verticals
from src.discovery import universe

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
    into the real jobs/universe_health.parquet — a fake slug there inflates the
    run report's pruned count, and a fixture slug colliding with a real one
    would bench a live board for 14 days. Redirect all three for every test;
    tests that patch them explicitly still win."""
    monkeypatch.setattr(universe, "HEALTH_PATH", tmp_path / "universe_health.parquet")
    monkeypatch.setattr(universe, "CSV_DIR", tmp_path / "universe_csv")
    monkeypatch.setattr(universe, "DEFAULT_COMPANIES_PATH", tmp_path / "companies.yaml")
