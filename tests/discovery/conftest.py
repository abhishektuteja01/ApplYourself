"""Discovery fixtures. The verticals fixture comes from tests/conftest.py."""

import pytest


@pytest.fixture(autouse=True)
def clear_location_cache():
    """parse_location is lru_cached, so a test that patches COUNTRY_NAMES /
    CITIES_BY_NAME would otherwise see (or leak) another test's entries."""
    from src.discovery.location import parse_location

    parse_location.cache_clear()
    yield
    parse_location.cache_clear()
