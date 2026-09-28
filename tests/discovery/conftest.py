"""Shared fixtures. Tests never read the gitignored profile/verticals.yaml —
the committed tests/discovery/fixtures/verticals.yaml (three synthetic
verticals, a byte-identical mirror of tests/fixtures/verticals.yaml) is
injected into the src.verticals singleton for every test."""

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


@pytest.fixture(autouse=True)
def clear_location_cache():
    """parse_location is lru_cached, so a test that patches COUNTRY_NAMES /
    CITIES_BY_NAME would otherwise see (or leak) another test's entries."""
    from src.discovery.location import parse_location

    parse_location.cache_clear()
    yield
    parse_location.cache_clear()
