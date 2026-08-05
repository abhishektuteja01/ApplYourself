"""Shared fixtures. Tests never read the gitignored profile/verticals.yaml —
the committed tests/fixtures/verticals.yaml (three synthetic verticals:
example_primary/secondary/tertiary) is injected into the src.verticals
singleton for every test. Coverage of the real config lives in
tests/test_real_config_drift.py, which skips when that file is absent."""

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
