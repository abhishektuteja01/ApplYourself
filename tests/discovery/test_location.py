import pytest

from src.discovery.location import parse_location


# A place is "positively non-US" (and thus dropped by the United-States
# allowlist) exactly when it resolves to a non-empty, non-US country, OR
# when every candidate in a genuine multi-region ambiguity is foreign (the
# candidate_countries path — location.py itself no longer picks a winner
# among multiple countries, so this helper mirrors what cleaning.py's
# allowlist-intersection check would do for a US-only allowlist).
def _is_foreign(raw):
    r = parse_location(raw)
    if r.country:
        return r.country != "United States"
    if r.candidate_countries:
        return "United States" not in r.candidate_countries
    return False


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

def test_location_oregon_not_eaten_by_or_separator():
    # "OR" the Oregon postal code must not be treated as the "or" separator.
    res = parse_location("Portland, OR")
    assert res.country == "United States"
    assert res.state == "OR"
    assert res.city == "Portland"

def test_location_oregon_with_country():
    res = parse_location("Bend, OR, USA")
    assert res.country == "United States"
    assert res.state == "OR"

def test_location_city_list_not_hijacked_by_state_namesake():
    # "New York" alone would libpostal-tag as a state; in a list of plain
    # city names it must not override the other, unambiguous cities.
    res = parse_location(
        "Chicago; Dallas; Los Angeles; Minneapolis; New York; "
        "San Francisco; Seattle; Washington, D.C."
    )
    assert res.country == ""
    assert res.state == ""

def test_location_many_state_dump_stays_unresolved():
    res = parse_location(
        "Raleigh-Cary, NC, Austin/Dallas, TX, Tampa Bay, FL, "
        "Greater Boston Area, MA, Denver, CO, Atlanta, GA, CA, NJ, TN, PA"
    )
    assert res.state != "NJ" or res.city != "Austin"

def test_location_state_name_collision_does_not_override_country():
    # libpostal splits "New York, USA" into ("new", city) + ("york", state);
    # "York" is a real UK subdivision, but the established country ("USA")
    # must win over the unrelated name collision.
    res = parse_location("New York, USA")
    assert res.country == "United States"

def test_location_uae_multiregion_flagged_not_dropped():
    # libpostal can't tokenize "Dubai, UAE" at all without the pre-parse
    # expansion; before that fix this silently resolved to Qatar only.
    res = parse_location("Doha, Qatar ; Dubai, UAE")
    assert res.country == ""
    assert res.candidate_countries == {"Qatar", "United Arab Emirates"}

def test_location_ksa_resolves():
    res = parse_location("Riyadh, KSA")
    assert res.country == "Saudi Arabia"

def test_location_drc_resolves():
    res = parse_location("Kinshasa, DRC")
    assert res.country == "Congo, The Democratic Republic of the"

def test_location_burma_resolves():
    res = parse_location("Yangon, Burma")
    assert res.country == "Myanmar"

def test_location_ivory_coast_resolves():
    res = parse_location("Abidjan, Ivory Coast")
    assert res.country == "Côte d'Ivoire"


# ---------------------------------------------------------------------
# Foreign-city recognition (the shortlist-leak fix). A bare foreign city
# name — no country suffix — must resolve to its country so the US-only
# allowlist drops it, instead of parsing to nothing and being kept.
# ---------------------------------------------------------------------

# The exact strings that leaked onto a real shortlist.
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
# Namesake resolution, worldwide design: a bare city name shared by both a
# US place and a larger foreign place now resolves to whichever is the
# real-world dominant place (population-ranked, same tie-break used
# throughout this module) -- it is NOT hardcoded to prefer the US namesake.
# This is a deliberate behavior change from the old dictionary-matcher
# design, which always preferred a US namesake to be "conservative" -- that
# was itself a hardcoded home-country privilege (the exact kind this
# rewrite removes; see CLAUDE.md's no-hardcoding rule and the location.py
# module docstring). An India-based user's search deserves the same quality
# of resolution a US-based user's does, and "assume US" doesn't generalize.
# ---------------------------------------------------------------------

@pytest.mark.parametrize("raw,country", [
    ("Paris", "France"),
    ("Vancouver", "Canada"),
    ("Manchester", "United Kingdom"),
    ("Cambridge", "United Kingdom"),
    ("Amsterdam", "Netherlands"),
])
def test_bare_namesake_resolves_to_globally_dominant_place(raw, country):
    res = parse_location(raw)
    assert res.country == country
    assert _is_foreign(raw)


# An EXPLICIT state code or name is real disambiguating context, not a
# population guess, so it still resolves confidently to its own US place
# regardless of the bare-name namesake's global population.
@pytest.mark.parametrize("raw,state", [
    ("Paris, TX", "TX"),
    ("Cambridge, MA", "MA"),
    ("Vancouver, WA", "WA"),
    ("Manchester, NH", "NH"),
])
def test_us_namesake_with_explicit_state_stays_us(raw, state):
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


def test_bare_comma_list_no_longer_fabricates_a_us_namesake():
    # A 5-city list joined only by commas (no "or"/";"/"/") isn't split into
    # segments by design (comma is ambiguous between "city, state" and a
    # list -- see location.py's module docstring), so libpostal parses it as
    # one ungrammatical blob. It reliably recognizes at most one real place
    # inside the noise; here that's "Paris", which -- under the new
    # worldwide, unbiased design -- resolves to the globally dominant real
    # Paris (France), not a fabricated "Paris, TX" the way the old
    # dictionary matcher's blind namesake bias used to risk. The key
    # invariant this test protects is the second part: never confidently
    # mislabel this as a US place.
    res = parse_location("London, Stockholm, Lisbon, Berlin, Paris")
    assert res.country != "United States"


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


# "Lebanon"/"Georgia" name both a US place and a country, but a real city
# anchors the context here, so libpostal's own role-tagging resolves both
# confidently as countries -- unlike the old dictionary matcher, which
# blindly tested "Lebanon"/"Georgia" against a US-namesake table regardless
# of context and collapsed the parse to ambiguous. Bare "Georgia" alone
# (no anchoring city) stays genuinely ambiguous -- see
# test_bare_state_country_namesake_stays_unresolved below.
@pytest.mark.parametrize("raw,country", [
    ("Beirut, Lebanon", "Lebanon"),
    ("Tbilisi, Georgia", "Georgia"),
])
def test_us_foreign_namesake_resolves_confidently_in_context(raw, country):
    res = parse_location(raw)
    assert res.country == country
    assert _is_foreign(raw)


def test_bare_state_country_namesake_stays_unresolved():
    # No anchoring city -> genuinely ambiguous between the US state and the
    # country -> unresolved, kept for manual review, never auto-dropped.
    assert _is_kept_unresolved("Georgia")


# ---------------------------------------------------------------------
# The regression that started this rewrite: job_id 35a899e2's "Central
# Jakarta" parsed to nothing (a small US town, Central LA, collided with
# the unrelated adjective "Central") and silently leaked past a US-only
# allowlist. libpostal recognizes "Central Jakarta" as one place unit, so
# the collision-inducing sub-token match never happens.
# ---------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "Central Jakarta", "South Jakarta", "North Jakarta",
    "East Jakarta", "West Jakarta",
])
def test_jakarta_district_no_longer_leaks_past_the_allowlist(raw):
    res = parse_location(raw)
    assert res.country == "Indonesia"
    assert _is_foreign(raw)


# ---------------------------------------------------------------------
# Same-string US-city/foreign-country collisions that used to collapse to
# unresolved (silently kept, bypassing the allowlist) because a small US
# town shares a name with a major foreign capital. Not hypothetical --
# found by direct testing this session, same bug class as Jakarta.
# ---------------------------------------------------------------------

@pytest.mark.parametrize("raw,country", [
    ("Rome, Italy", "Italy"),
    ("Athens, Greece", "Greece"),
    ("Cairo, Egypt", "Egypt"),
    ("Delhi, India", "India"),
])
def test_us_city_foreign_country_collisions_resolve_foreign(raw, country):
    res = parse_location(raw)
    assert res.country == country
    assert _is_foreign(raw)


# ---------------------------------------------------------------------
# Namesake city+state vs. city+country pairs, both sides in one place so a
# future change can't silently break one side without the test file
# showing it right next to its counterpart.
# ---------------------------------------------------------------------

@pytest.mark.parametrize("raw,state", [
    ("Athens, OH", "OH"),
    ("Rome, GA", "GA"),
    ("Birmingham, AL", "AL"),
    ("Dublin, OH", "OH"),
    ("Valencia, CA", "CA"),
    ("York, PA", "PA"),
])
def test_namesake_pair_us_side(raw, state):
    res = parse_location(raw)
    assert res.country == "United States"
    assert res.state == state


@pytest.mark.parametrize("raw,country", [
    ("Athens, Greece", "Greece"),
    ("Rome, Italy", "Italy"),
    ("Birmingham, UK", "United Kingdom"),
    ("Dublin, Ireland", "Ireland"),
    ("Valencia, Spain", "Spain"),
    ("York, UK", "United Kingdom"),
])
def test_namesake_pair_foreign_side(raw, country):
    res = parse_location(raw)
    assert res.country == country
    assert _is_foreign(raw)


# ---------------------------------------------------------------------
# The libpostal single-merged-token quirk and its repair (see location.py's
# module-level comment on the "Paris, TX" case).
# ---------------------------------------------------------------------

def test_paris_tx_merge_quirk_is_repaired():
    res = parse_location("Paris, TX")
    assert res.country == "United States"
    assert res.state == "TX"
    assert res.city == "Paris"


def test_paris_tx_usa_agrees_with_the_repaired_case():
    # libpostal already splits this one cleanly without the repair path --
    # confirms both paths land on the same answer.
    res = parse_location("Paris, TX, USA")
    assert res.country == "United States"
    assert res.state == "TX"


# ---------------------------------------------------------------------
# Worldwide subdivision correctness -- new coverage class, not present in
# the old US-only design at all.
# ---------------------------------------------------------------------

def test_vancouver_bc_resolves_to_canada_without_a_country_suffix():
    # Under the old US-only state table this wrongly resolved to
    # Washington state (see location.py's module docstring) -- fixed as a
    # natural consequence of validating subdivisions worldwide.
    res = parse_location("Vancouver, BC")
    assert res.country == "Canada"
    assert res.state == "BC"


def test_bengaluru_karnataka_resolves_via_diacritic_folded_subdivision():
    # pycountry's real subdivision name is "Karnātaka" (diacritics) --
    # confirms _fold() is applied to subdivision names, not just cities.
    res = parse_location("Bengaluru, Karnataka")
    assert res.country == "India"
    assert res.state == "KA"


def test_bangalore_ka_code_collision_does_not_silently_pick_the_wrong_country():
    # "KA" is both India's Karnataka and Georgia's Kakheti region -- a bare
    # code alone can't tell them apart. The city itself ("Bangalore") is
    # only a real place in India, which is what the overall parse should
    # land on -- via the city signal, not a guessed subdivision code.
    res = parse_location("Bangalore, KA")
    assert res.country == "India"


@pytest.mark.parametrize("raw,country,state", [
    ("Toronto, Ontario, Canada", "Canada", "ON"),
    ("Recife, PE, Brazil", "Brazil", "PE"),
])
def test_full_hierarchical_strings_outside_north_america_and_europe(raw, country, state):
    res = parse_location(raw)
    assert res.country == country
    assert res.state == state


# ---------------------------------------------------------------------
# New Mexico vs. Mexico: a country name that is a strict substring of a US
# state's name must not bleed a phantom country signal into the state.
# ---------------------------------------------------------------------

def test_albuquerque_new_mexico_is_us():
    res = parse_location("Albuquerque, New Mexico")
    assert res.country == "United States"
    assert res.state == "NM"


def test_bare_new_mexico_is_unambiguous_unlike_georgia_or_washington():
    # Unlike "Georgia"/"Washington", "New Mexico" isn't also a country name
    # or a same-named US city, so it resolves confidently on its own.
    res = parse_location("New Mexico")
    assert res.country == "United States"
    assert res.state == "NM"


@pytest.mark.parametrize("raw", ["Mexico", "Mexico City, Mexico"])
def test_mexico_the_country_is_unaffected_by_the_new_mexico_namesake(raw):
    assert parse_location(raw).country == "Mexico"


# ---------------------------------------------------------------------
# Multi-word countries containing "and" must survive the list-separator
# pre-split intact -- "and" is deliberately excluded from the separator
# regex for exactly this reason (see location.py's module docstring).
# ---------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "Trinidad and Tobago", "Antigua and Barbuda", "Bosnia and Herzegovina",
])
def test_and_countries_survive_the_list_separator_presplit(raw):
    assert parse_location(raw).country == raw


# ---------------------------------------------------------------------
# Multi-region reconciliation across every separator style. location.py no
# longer decides keep-vs-drop for a multi-country ambiguity itself (that
# moved to cleaning.py's allowlist-aware logic) -- it surfaces every
# candidate via `candidate_countries` instead, asserted here directly.
# ---------------------------------------------------------------------
# Found by scanning real historical data after this rewrite landed, not
# hypothetical: bare short codes/words that are also real (tiny) places
# elsewhere in the world must not silently hijack the country resolution.
# ---------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["PA", "WA", "Asia"])
def test_bare_short_tokens_dont_resolve_via_a_tiny_unrelated_namesake(raw):
    # "Pa" (Burkina Faso, pop. 15k), "Wa" (Ghana, pop. 78k) and "Asia"
    # (a town in the Philippines, pop. 24k) are all real geonames entries
    # that would otherwise hijack a bare US-state-abbreviation-shaped
    # string with no other context.
    assert _is_kept_unresolved(raw)


def test_ambiguous_state_code_does_not_get_overridden_by_an_unrelated_city():
    # "PA" collides between Brazil's Pará and Pennsylvania, and the real
    # Warrington, PA (a small Bucks County community) isn't in the geonames
    # dataset at all -- only the much bigger Warrington, England is. The
    # ambiguous state signal must suppress the unconstrained global city
    # fallback rather than let it confidently resolve to the UK.
    assert _is_kept_unresolved("Warrington, PA")


# ---------------------------------------------------------------------

def test_slash_separator_resolves_the_real_segment_and_ignores_remote():
    res = parse_location("Austin, TX / Remote")
    assert res.country == "United States"
    assert res.state == "TX"
    assert res.remote is True


def test_or_list_of_two_full_us_pairs_is_a_multi_site_listing_kept():
    # Both segments are the same country but different states -> a
    # multi-site listing, not a single answer -> kept for review.
    assert _is_kept_unresolved("Chicago, Illinois or Austin, Texas")


def test_semicolon_all_foreign_list_has_no_us_candidate():
    res = parse_location("London, United Kingdom; Stockholm, Sweden")
    assert res.country == ""
    assert res.candidate_countries == frozenset({"United Kingdom", "Sweden"})
    assert "United States" not in res.candidate_countries


# ---------------------------------------------------------------------
# Found scanning ~350 random real jobs/clean.parquet location strings
# through the parser end to end (not hypothetical). Each of these traces
# to one of: the separator regex not covering pipe/dash, "and"/"&"
# country lists not being surfaced as ambiguous, native-language country/
# subdivision names, or geonames' canonical spelling differing from what
# a job poster writes.
# ---------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["US and Canada", "United States and Canada", "United States & Canada"])
def test_and_ampersand_country_lists_surface_as_candidates(raw):
    # libpostal folds the whole phrase into one unparseable "house" blob
    # (confirmed via parse_address), so ordinary segment mining finds
    # nothing -- the "and"/"&" fallback split only fires on that total
    # failure, which is what lets "Trinidad and Tobago" (tested above)
    # keep resolving as one country without ever reaching this path.
    res = parse_location(raw)
    assert res.country == ""
    assert res.candidate_countries == frozenset({"United States", "Canada"})


def test_pipe_separated_multi_office_listing_is_ambiguous_not_a_wrong_single_answer():
    # Before `|` was a recognized separator, libpostal mis-tokenized the
    # whole string and confidently returned just "Seattle, WA", silently
    # dropping the other two offices. It must resolve the same way the
    # semicolon-separated equivalent already does: ambiguous, kept empty.
    res = parse_location("San Francisco, CA | New York City, NY | Seattle, WA")
    assert res.country == ""
    assert res.state == ""
    assert res.city == ""


def test_country_dash_city_resolves_regardless_of_libpostal_tokenizer_luck():
    # libpostal happens to split "France - Paris" into two components on
    # its own but folds "Netherlands - Alkmaar" into one unparseable
    # blob -- an explicit " - " separator makes this deterministic
    # instead of depending on which one libpostal's model gets right.
    assert parse_location("Netherlands - Alkmaar").country == "Netherlands"
    assert parse_location("France - Paris").country == "France"


@pytest.mark.parametrize("raw", ["Winston-Salem, NC", "Sophia-Antipolis, France"])
def test_bare_hyphen_in_a_real_place_name_is_not_treated_as_a_separator(raw):
    # The " - " separator only matches a hyphen with a space on both
    # sides -- a real hyphenated place name has no such spaces, so it
    # must still parse as a single segment.
    res = parse_location(raw)
    assert res.country != ""


def test_nyc_abbreviation_resolves_like_new_york():
    res = parse_location("Sapien HQ — NYC")
    assert res.country == "United States"
    assert res.state == "NY"
    assert res.city == "New York City"


def test_saint_petersburg_florida_disambiguates_from_the_russian_city():
    # geonames' canonical spelling for the Russian city is "Saint
    # Petersburg" and for the Florida one is "St. Petersburg" -- both
    # spellings must resolve to whichever is correct for the given
    # country/state context, not just whichever canonical entry happens
    # to share the raw string exactly.
    res = parse_location("Saint Petersburg, FL, US")
    assert res.country == "United States"
    assert res.state == "FL"


def test_saint_petersburg_alone_still_means_russia():
    # With no US state/country context, the bare name must still resolve
    # to the globally dominant place, same as any other bare-namesake
    # case tested elsewhere in this file.
    assert parse_location("Saint Petersburg").country == "Russian Federation"


@pytest.mark.parametrize("raw,country", [
    ("Bruxelles", "Belgium"),
    ("Bangalore", "India"),
    ("Ciudad de México", "Mexico"),
])
def test_curated_city_name_aliases_resolve_the_local_or_common_spelling(raw, country):
    assert parse_location(raw).country == country


@pytest.mark.parametrize("raw,country", [
    ("Deutschland", "Germany"),
    ("España", "Spain"),
    ("Brasil", "Brazil"),
    ("Nederland", "Netherlands"),
])
def test_native_language_country_names_resolve_via_pycountrys_own_locale_data(raw, country):
    assert parse_location(raw).country == country
