"""The location gate over one case table, tests/shared/location_cases.yaml."""

from pathlib import Path

import pytest
import yaml

from applyourself.shared.countries import CC_TO_COUNTRY
from applyourself.shared.location.allowlist import Allowlist
from applyourself.shared.location.parser import allowed_codes, read_location

TABLE = yaml.safe_load((Path(__file__).parent / "location_cases.yaml").read_text(encoding="utf-8"))


class _Cfg:
    def __init__(self, countries, abroad=None):
        from applyourself.discovery.config import LocationAllowlist

        self.location_allowlist = LocationAllowlist(countries)
        self._abroad = abroad or {}

    def abroad_lanes(self):
        return self._abroad


def _id(case):
    extra = case.get("lane") or case.get("country") or ""
    return f"{case.get('allow', 'wide')}:{case['loc']}" + (f"|{extra}" if extra else "")


@pytest.mark.parametrize("case", TABLE["cases"], ids=_id)
def test_location_case(case):
    names = TABLE["allowlists"][case.get("allow", "wide")]
    abroad = {k: set(v) for k, v in TABLE["abroad"].items()} if case.get("lane") else {}
    got = Allowlist(_Cfg(names, abroad)).check(
        case["loc"], case.get("lane", ""), case.get("country", "")
    )
    assert got.keep is case["keep"]
    if got.keep:
        assert got.location == case["loc"].strip()  # the display is the raw text
    if "shown" in case:
        assert got.location == case["shown"]
    if "countries" in case:
        means = read_location(case["loc"].strip(), allowed_codes(names)).countries
        assert sorted(CC_TO_COUNTRY[c] for c in means) == case["countries"]


def test_a_primed_cache_entry_does_not_survive_into_the_next_test():
    # Pairs with the test below: a missing conftest cache_clear would serve this there.
    assert read_location("Germany").certain == "DE"


def test_monkeypatched_country_table_is_honoured(monkeypatch):
    from applyourself.shared.location import places

    monkeypatch.setitem(places.COUNTRY, "germany", "FR")
    assert read_location("Germany").certain == "FR"
