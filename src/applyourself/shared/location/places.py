"""Place-name tables: countries, states, cities and regions, worldwide."""

from __future__ import annotations

import gettext
import re
from collections.abc import Iterable
from typing import Any

import geonamescache
import pycountry

from applyourself.shared.countries import CC_TO_COUNTRY, fold_accents


def _fold(s: str) -> str:
    """Lowercase, strip diacritics and surrounding whitespace so scraped
    ASCII forms match accented canonical spellings (e.g. "Iasi" ->
    "Iaşi", "Karnataka" -> "Karnātaka") regardless of stray whitespace a
    pre-split segment can leave on a libpostal component."""
    return fold_accents(s).lower().strip()


gc = geonamescache.GeonamesCache()
_ALL_CITIES = gc.get_cities()
_GC_COUNTRIES = gc.get_countries()  # only used for continentcode, below

# ---------------------------------------------------------------------
# Subdivisions (states/provinces/regions) — worldwide, from pycountry
# (ISO 3166-2). Codes are NOT globally unique ("KA" is both India's
# Karnataka and Georgia's Kakheti), so a bare code only resolves when it's
# unambiguous worldwide or corroborated by a country already found in the
# same segment.
# ---------------------------------------------------------------------

SUBDIVISIONS_BY_NAME: dict[str, list[Any]] = {}
SUBDIVISIONS_BY_CODE: dict[str, list[Any]] = {}
for _s in pycountry.subdivisions:
    SUBDIVISIONS_BY_NAME.setdefault(_fold(_s.name), []).append(_s)
    _bare = _s.code.split("-", 1)[-1].upper()
    SUBDIVISIONS_BY_CODE.setdefault(_bare, []).append(_s)

# pycountry's own subdivision.name is often the local-language ISO name
# ("Noord-Brabant", "Bayern") rather than the English form job postings
# use. Same iso-codes gettext catalog mechanism as countries.COUNTRY_NAMES,
# translated into English, added as an extra lookup key when it differs.
# Coverage is genuinely partial -- iso-codes' own English catalog doesn't
# translate every subdivision (confirmed: "Bayern" -> "Bavaria" exists,
# "Noord-Brabant" -> "North Brabant" does not) -- this narrows the gap,
# it doesn't close it.
try:
    _en_subdivision_cat = gettext.translation("iso3166-2", pycountry.LOCALES_DIR, languages=["en"])
    for _s in pycountry.subdivisions:
        _native_name = _en_subdivision_cat.gettext(_s.name)
        if _native_name != _s.name:
            SUBDIVISIONS_BY_NAME.setdefault(_fold(_native_name), []).append(_s)
except FileNotFoundError:
    pass

_SHORT_CODE_MAX_LEN = 3

# Countries whose subdivisions are conventionally written as a bare 2-letter
# code in English prose ("Austin, TX", "Bangalore, KA", "Toronto, ON"). Used
# only to break ties when a bare code collides with an obscure ISO 3166-2
# code from a country that is essentially never abbreviated this way in
# English text (e.g. "MA" is also Brazil's Maranhão and Burundi's Makamba,
# but nobody writes a job posting location as "Somewhere, MA" meaning
# either of those). This is a tie-break grounded in observed writing
# convention, not a privileged "home country" — it applies identically
# regardless of which country the user's own allowlist targets.
_COMMONLY_ABBREVIATED_CC = {"US", "CA", "AU", "IN", "MX", "BR"}

# ---------------------------------------------------------------------
# Continents — for the optional `location_allowlist.continents` shorthand.
# Not used by the parser itself.
# ---------------------------------------------------------------------

_CONTINENT_CODE_TO_NAME = {
    "AF": "Africa",
    "AS": "Asia",
    "EU": "Europe",
    "NA": "North America",
    "OC": "Oceania",
    "SA": "South America",
    "AN": "Antarctica",
}

CONTINENT_TO_COUNTRIES: dict[str, list[str]] = {}
for _cc, _data in _GC_COUNTRIES.items():
    _cont = _CONTINENT_CODE_TO_NAME.get(_data.get("continentcode", ""))
    _country_name = CC_TO_COUNTRY.get(_cc)
    if _cont and _country_name:
        CONTINENT_TO_COUNTRIES.setdefault(_cont, []).append(_country_name)

# ---------------------------------------------------------------------
# Region tokens — acronyms that name a multi-country region rather than a
# place. A segment that is exactly one of these expands to its constituent
# countries, so a token-only string resolves to candidate_countries and the
# caller's allowlist decides keep-vs-drop. Closed list: an acronym not
# listed here is parsed as ordinary text.
# ---------------------------------------------------------------------

_MIDDLE_EAST_CC = frozenset(
    {
        "AE",
        "BH",
        "IL",
        "IQ",
        "IR",
        "JO",
        "KW",
        "LB",
        "OM",
        "PS",
        "QA",
        "SA",
        "SY",
        "TR",
        "YE",
    }
)
# Latin America outside the South American continent code.
_LATAM_EXTRA_CC = frozenset(
    {
        "MX",
        "GT",
        "BZ",
        "SV",
        "HN",
        "NI",
        "CR",
        "PA",
        "CU",
        "DO",
        "PR",
        "HT",
    }
)


def _countries_from_cc(codes: Iterable[str]) -> set[str]:
    return {name for cc in codes if (name := CC_TO_COUNTRY.get(cc))}


def _continents(*names: str) -> set[str]:
    out: set[str] = set()
    for name in names:
        out |= set(CONTINENT_TO_COUNTRIES.get(name, []))
    return out


_EMEA = _continents("Europe", "Africa") | _countries_from_cc(_MIDDLE_EAST_CC)

REGION_TOKEN_COUNTRIES: dict[str, frozenset[str]] = {
    "emea": frozenset(_EMEA),
    "emeia": frozenset(_EMEA | _countries_from_cc({"IN"})),
    "apac": frozenset(_continents("Asia", "Oceania")),
    "latam": frozenset(_continents("South America") | _countries_from_cc(_LATAM_EXTRA_CC)),
    "anz": frozenset(_countries_from_cc({"AU", "NZ"})),
    "europe": frozenset(_continents("Europe")),
    "asia": frozenset(_continents("Asia")),
    "latin america": frozenset(_continents("South America") | _countries_from_cc(_LATAM_EXTRA_CC)),
}

# ---------------------------------------------------------------------
# Cities — worldwide (not filtered to any one country). Used only as a
# last-resort "lone place name -> what country/city is this" convenience;
# never used to inject a phantom country signal from a substring the way
# the old dictionary matcher did (that mechanism is what caused the
# Jakarta/Rome/Athens collisions and has been deleted, not ported).
# ---------------------------------------------------------------------

# "St." and "Saint" are the same word to a job poster but can each be a
# real, independently-canonical geonames entry for a DIFFERENT city
# ("St. Petersburg" FL vs "Saint Petersburg" Russia) -- folding both forms
# to one lookup key merges them into a shared bucket instead of the two
# ever shadowing each other, so country-hint filtering (below) can pick
# the right one the same way it already does for any other same-name
# collision. Applied at both index-build time and query time. Not a
# one-off alias: covers every St./Saint pair in the dataset (St. Louis,
# St. Paul, St. John's, ...), not just the one that was observed.
_ST_RE = re.compile(r"^st\.?\s+")


def _normalize_saint(folded: str) -> str:
    return _ST_RE.sub("saint ", folded)


CITIES_BY_NAME: dict[str, list[dict[str, Any]]] = {}
for _city in _ALL_CITIES.values():
    CITIES_BY_NAME.setdefault(_normalize_saint(_fold(_city["name"])), []).append(_city)
for _lst in CITIES_BY_NAME.values():
    _lst.sort(key=lambda c: c.get("population", 0), reverse=True)

# geonames spells NYC "New York City"; postings write "New York" or "NYC".
# Curated, closed list for names that are different words in different
# languages/registers rather than a lexical pattern (no general mechanism
# can derive these) -- same documented pattern as _PRE_PARSE_EXPANSIONS
# below, not a growing pile of one-offs: add here only when a name is
# confirmed missing and isn't covered by _normalize_saint or the
# COUNTRY_NAMES/SUBDIVISIONS_BY_NAME locale-translation mechanisms.
_CITY_NAME_ALIASES = {
    "new york": "new york city",
    "nyc": "new york city",
    "bangalore": "bengaluru",
    "bruxelles": "brussels",
    "ciudad de mexico": "mexico city",
}

# A city match with no country context yet becomes the country signal
# itself, so it needs real prominence -- see _city_lookup's docstring.
_MIN_LONE_CITY_POPULATION = 100_000

# Explicit, narrow suffix strip for generic geographic descriptors libpostal
# folds into one chunk with a real city name ("San Francisco Bay Area").
# Deliberately NOT a general trailing-word-dropping fallback: dropping the
# last word from "Central Jakarta" would land on "Jakarta" (fine) but from
# some other phrase could land on an unrelated common word -- this list is
# closed and geography-descriptor-specific, not a blind heuristic. Longest
# first so "bay area" is stripped before the shorter "area" would match.
_GENERIC_AREA_SUFFIXES = (
    "metropolitan area",
    "bay area",
    "metro area",
    "region",
    "area",
)

# Same idea, leading side: geonames lists the parent city ("Jakarta") but
# not every compass-point district name a job board writes ("Central
# Jakarta", "South Jakarta"). This is intentionally NOT a general
# leading-word-dropping fallback either: it only ever strips one of these
# specific, closed compass/scale words, never an arbitrary word, so it
# can't reintroduce the collision this rewrite fixes (an incidental common
# word like "Central" is only ever stripped here, never tested on its own).
_DISTRICT_PREFIXES = ("central ", "north ", "south ", "east ", "west ", "greater ")
