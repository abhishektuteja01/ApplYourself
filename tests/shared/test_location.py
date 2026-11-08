"""The location gate over one case table, tests/shared/location_cases.yaml."""

from pathlib import Path

import pytest
import yaml

pytest.importorskip("postal.parser")

from applyourself.shared.location.parser import parse_location

TABLE = yaml.safe_load((Path(__file__).parent / "location_cases.yaml").read_text(encoding="utf-8"))


class _Cfg:
    def __init__(self, countries, abroad=None):
        from applyourself.discovery.config import LocationAllowlist

        self.location_allowlist = LocationAllowlist(countries)
        self._abroad = abroad or {}

    def abroad_lanes(self):
        return self._abroad


def _id(case):
    extra = case.get("lane") or case.get("title") or ""
    return f"{case.get('allow', 'wide')}:{case['loc']}" + (f"|{extra}" if extra else "")


@pytest.mark.parametrize("case", TABLE["cases"], ids=_id)
def test_location_case(case):
    from applyourself.shared.location.allowlist import Allowlist

    abroad = {k: set(v) for k, v in TABLE["abroad"].items()} if case.get("lane") else {}
    cfg = _Cfg(TABLE["allowlists"][case.get("allow", "wide")], abroad)
    got = Allowlist(cfg).check(case["loc"], case.get("lane", ""), case.get("title", ""))
    assert got.keep is case["keep"]
    if "shown" in case:
        assert got.location == case["shown"]
    if "countries" in case:
        p = parse_location(case["loc"].strip())
        assert sorted({p.country} - {""} or p.candidate_countries) == case["countries"]


@pytest.mark.parametrize("case", TABLE["titles"], ids=lambda c: c["title"])
def test_residency_countries(case):
    from applyourself.shared.location.allowlist import residency_countries
    from applyourself.shared.location.places import REGION_TOKEN_COUNTRIES

    want = case["countries"]
    want = set(REGION_TOKEN_COUNTRIES["europe"]) if want == "europe" else set(want)
    assert residency_countries(case["title"]) == want


# --- lru_cache on parse_location (D4.4) ---


def test_cache_is_transparent_first_call_equals_cached_call():
    parse_location.cache_clear()
    cold = parse_location("Austin, TX")
    warm = parse_location("Austin, TX")
    assert warm == cold
    assert parse_location.cache_info().hits == 1

    parse_location.cache_clear()
    recomputed = parse_location("Austin, TX")
    assert recomputed == cold


def test_a_primed_cache_entry_does_not_survive_into_the_next_test():
    # Pairs with the test below: primes the cache under the real tables, so a
    # missing conftest cache_clear would serve this stale answer there.
    assert parse_location("Germany").country == "Germany"


def test_monkeypatched_country_table_is_honoured(monkeypatch):
    from applyourself.shared import countries

    monkeypatch.setitem(countries.COUNTRY_NAMES, "germany", "Zzyzxland")
    assert parse_location("Germany").country == "Zzyzxland"
