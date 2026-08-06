import pytest

from src.discovery.location import parse_location


# A place is "positively non-US" (and thus dropped by the United-States
# allowlist) exactly when it resolves to a non-empty, non-US country.
def _is_foreign(raw):
    r = parse_location(raw)
    return bool(r.country) and r.country != "United States"


def _is_us(raw):
    return parse_location(raw).country == "United States"


def _is_kept_unresolved(raw):
    # No country at all -> falls into the filter's "nothing parsed -> keep"
    # branch (conservative: shown for manual review, not auto-dropped).
    return parse_location(raw).country == ""

def test_location_austin_tx():
    res = parse_location("Austin, TX")
    assert res.country == "United States"
    assert res.state == "TX"
    assert res.city == "Austin"
    assert res.remote is False

def test_location_remote():
    res = parse_location("Remote")
    assert res.remote is True
    assert res.country == ""
    assert res.state == ""
    assert res.city == ""

def test_location_empty():
    res = parse_location("")
    assert res.country == ""
    assert res.state == ""
    assert res.city == ""
    assert res.remote is False

def test_location_berlin_germany():
    res = parse_location("Berlin, Germany")
    assert res.country == "Germany"

def test_location_sf_bay_area():
    res = parse_location("San Francisco Bay Area")
    # city San Francisco OR all-empty
    if res.city == "San Francisco":
        assert res.country == "United States"
        assert res.state == "CA"
    else:
        assert res.country == ""
        assert res.state == ""
        assert res.city == ""

def test_location_multiple():
    res = parse_location("New York, London or Singapore")
    # must NOT resolve to a single US location (all-empty acceptable)
    if res.country != "":
        # if it resolves to something, it cannot be US-only. But spec says all-empty acceptable.
        assert res.country == ""
        assert res.state == ""
        assert res.city == ""

def test_location_bare_ca():
    res = parse_location("CA")
    assert res.country == ""
    assert res.state == ""
    assert res.city == ""

def test_location_remote_austin():
    res = parse_location("Remote - Austin, TX")
    assert res.remote is True
    assert res.country == "United States"
    assert res.state == "TX"
    assert res.city == "Austin"


# ---------------------------------------------------------------------
# Foreign-city recognition (the shortlist-leak fix). A bare foreign city
# name — no country suffix — must resolve to its country so the US-only
# allowlist drops it, instead of parsing to nothing and being kept.
# ---------------------------------------------------------------------

# The exact strings that leaked onto the 2026-07-24 shortlist.
@pytest.mark.parametrize("raw,country", [
    ("London", "United Kingdom"),
    ("Stockholm", "Sweden"),
    ("Lisbon", "Portugal"),
    ("Kuala Lumpur", "Malaysia"),       # multi-word foreign city (n-gram)
    ("Iasi", "Romania"),                # ASCII form of geonames "Iaşi"
])
def test_bare_foreign_city_resolves_to_country(raw, country):
    res = parse_location(raw)
    assert res.country == country
    assert res.state == ""
    assert res.city == ""
    assert _is_foreign(raw)


# Diacritic folding: accented spelling and its ASCII fold both resolve.
@pytest.mark.parametrize("raw", ["Iași", "Iasi", "Zürich", "Zurich", "São Paulo", "Sao Paulo"])
def test_foreign_city_diacritic_folding(raw):
    assert _is_foreign(raw)


# A wide spread of world cities from different regions, none a US namesake.
@pytest.mark.parametrize("raw", [
    "Berlin", "Toronto", "Tel Aviv", "Bengaluru", "Singapore",
    "Munich", "Tokyo", "Sydney", "Dubai", "Warsaw",
])
def test_assorted_foreign_cities_dropped(raw):
    assert _is_foreign(raw), f"{raw!r} should be positively non-US"


# Namesake protection is name-based, so some foreign cities that happen to
# share a US city's name stay US (conservative — we never over-drop). Amsterdam
# (Amsterdam, NY) is one; documented so the behavior is intentional, not a bug.
def test_foreign_city_sharing_us_name_stays_us():
    assert _is_us("Amsterdam")


# Explicit country suffix still works (regression on the pre-existing path).
@pytest.mark.parametrize("raw,country", [
    ("Berlin, Germany", "Germany"),
    ("London, United Kingdom", "United Kingdom"),
    ("London, UK", "United Kingdom"),
    ("Toronto, Canada", "Canada"),
])
def test_foreign_city_with_country_suffix(raw, country):
    assert parse_location(raw).country == country


# ---------------------------------------------------------------------
# Namesake protection: a foreign city that is ALSO a US city stays US, so
# we never over-drop legitimate US roles. These names are excluded from the
# foreign table because a US city bears them too.
# ---------------------------------------------------------------------

@pytest.mark.parametrize("raw,state", [
    ("Paris", "TX"),
    ("Vancouver", "WA"),
    ("Manchester", "NH"),
    ("Cambridge", "MA"),
    ("Cambridge, MA", "MA"),
    ("Paris, TX", "TX"),
])
def test_us_namesake_stays_us(raw, state):
    res = parse_location(raw)
    assert res.country == "United States"
    assert res.state == state
    assert _is_us(raw)


# ---------------------------------------------------------------------
# Multi-region reconciliation.
# ---------------------------------------------------------------------

def test_all_foreign_multicountry_dropped():
    # Two foreign countries, no US anywhere -> positively non-US -> drop.
    assert _is_foreign("London, United Kingdom; Stockholm, Sweden")


def test_all_foreign_multicity_dropped():
    # Two foreign cities, no country suffixes, no US -> still dropped.
    assert _is_foreign("London or Berlin")


def test_us_inclusive_global_role_kept():
    # A real US option among foreign ones -> don't guess, keep for review.
    assert _is_kept_unresolved("New York, London or Singapore")


def test_foreign_list_with_us_namesake_not_mislabeled():
    # The user's case: an all-European list whose only "US" signal is the
    # Paris/Texas namesake. We can't deterministically tell it from a genuine
    # US-inclusive list, so we conservatively KEEP it -- but crucially we do
    # NOT fabricate a confident "Paris, TX" label (the old bug).
    res = parse_location("London, Stockholm, Lisbon, Berlin, Paris")
    assert res.country == ""
    assert res.state == ""
    assert res.city == ""


def test_strong_us_anchor_with_foreign_city_kept():
    # "Austin, TX or London": US state code + a foreign city -> mixed ->
    # kept (not dropped), never mislabeled to a single foreign spot.
    assert _is_kept_unresolved("Austin, TX or London")


# ---------------------------------------------------------------------
# Remote / degenerate inputs are unchanged.
# ---------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["Remote", "remote", "Fully Remote", "Remote - US"])
def test_remote_flag_still_detected(raw):
    assert parse_location(raw).remote is True


def test_foreign_city_with_remote_is_still_foreign():
    # "London or Remote" carries the remote flag but is anchored to London,
    # so it resolves foreign and is dropped (a UK-remote posting).
    res = parse_location("London or Remote")
    assert res.remote is True
    assert res.country == "United Kingdom"


@pytest.mark.parametrize("raw", ["", "   ", "Anywhere", "Multiple Locations"])
def test_unrecognized_strings_kept_unresolved(raw):
    assert _is_kept_unresolved(raw)


# ---------------------------------------------------------------------
# Namesake collisions. A country name that is also a US state or city, a full
# state name embedded in a city name, and a country name nested inside a city
# name all used to produce a second, phantom region signal — which collapsed
# the parse to empty and sent the row down the "nothing parsed -> keep" branch,
# silently bypassing the location allowlist.
# ---------------------------------------------------------------------

@pytest.mark.parametrize("raw,state,city", [
    ("Washington, DC", "DC", "Washington"),        # state name inside the city
    ("Kansas City, MO", "MO", "Kansas City"),
    ("Oklahoma City, OK", "OK", "Oklahoma City"),
    ("New York, NY", "NY", "New York City"),       # geonames spells it "New York City"
    ("Atlanta, Georgia", "GA", "Atlanta"),         # state name that is also a country
    ("Jamaica, NY", "NY", "Jamaica"),              # city name that is also a country
    ("Jersey City, NJ", "NJ", "Jersey City"),      # country name nested in the city
    ("Panama City, FL", "FL", "Panama City"),
])
def test_us_namesake_resolves_to_its_us_place(raw, state, city):
    res = parse_location(raw)
    assert res.country == "United States"
    assert res.state == state
    assert res.city == city


@pytest.mark.parametrize("raw,state,city", [
    ("Boston, MA", "MA", "Boston"),
    ("Boston, Massachusetts", "MA", "Boston"),
    ("Austin, TX", "TX", "Austin"),
    ("San Francisco Bay Area", "CA", "San Francisco"),
])
def test_unambiguous_us_locations_are_unchanged(raw, state, city):
    res = parse_location(raw)
    assert res.country == "United States"
    assert res.state == state
    assert res.city == city


@pytest.mark.parametrize("raw,country", [
    ("Toronto, Canada", "Canada"),
    ("Berlin, Germany", "Germany"),
    ("Hyderabad, India", "India"),
    ("London, United Kingdom", "United Kingdom"),
])
def test_foreign_locations_still_resolve_after_the_namesake_rule(raw, country):
    assert parse_location(raw).country == country
    assert _is_foreign(raw)


@pytest.mark.parametrize("raw", ["Beirut, Lebanon", "Tbilisi, Georgia"])
def test_genuine_us_foreign_namesake_stays_unresolved(raw):
    # "Lebanon"/"Georgia" name both a US place and a country, so the signals
    # genuinely conflict. Unresolved -> kept for manual review, never auto-dropped.
    assert _is_kept_unresolved(raw)
