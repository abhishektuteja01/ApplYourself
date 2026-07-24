"""Shared fixtures. Tests never read the gitignored profile/verticals.yaml —
the committed tests/fixtures/verticals.yaml (the real configured verticals,
kept content-identical to the live config) is injected into the
src.verticals singleton for every test."""

from pathlib import Path

import pytest

from src import verticals

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "verticals.yaml"


@pytest.fixture(autouse=True)
def cfg():
    config = verticals.load_verticals(FIXTURE_PATH)
    verticals.set_config(config)
    yield config
    verticals.set_config(None)
