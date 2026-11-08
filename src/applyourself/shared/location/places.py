"""Place tables: country names and codes, subdivisions, cities and region words, worldwide."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from typing import NamedTuple

import geonamescache
import pycountry

from applyourself.shared.countries import (
    CC_TO_COUNTRY,
    COUNTRY_TO_CC,
    NATIVE_NAMES,
    fold_accents,
    location_names,
)


def fold(s: str) -> str:
    """Lowercase, diacritics stripped ("Iaşi" -> "iasi")."""
    return fold_accents(s).lower().strip()


_ST_RE = re.compile(r"^st\.?\s+")


def saint(folded: str) -> str:
    """ "St. Petersburg" and "Saint Petersburg" share one key."""
    return _ST_RE.sub("saint ", folded)


CODE2: dict[str, str] = {c.alpha_2: c.alpha_2 for c in pycountry.countries} | {"UK": "GB"}

# Subdivisions: bare code ("TX") -> countries; folded name -> {country: code}.
SUB_CODE: dict[str, set[str]] = defaultdict(set)
SUB_NAME: dict[str, dict[str, str]] = defaultdict(dict)
for _s in pycountry.subdivisions:
    _code = _s.code.split("-", 1)[1].upper()
    SUB_CODE[_code].add(_s.country_code)
    SUB_NAME[fold(_s.name)].setdefault(_s.country_code, _code)


class City(NamedTuple):
    cc: str
    population: int
    name: str
    admin1: str


# Every geonames city of 15k people or more, by folded name, biggest first.
CITY: dict[str, list[City]] = defaultdict(list)
for _c in geonamescache.GeonamesCache().get_cities().values():
    CITY[saint(fold(_c["name"]))].append(
        City(_c["countrycode"], _c["population"], _c["name"], _c.get("admin1code", ""))
    )
for _lst in CITY.values():
    _lst.sort(key=lambda c: c.population, reverse=True)
for _alias, _name in {"new york": "new york city", "nyc": "new york city"}.items():
    CITY[_alias] = CITY[_name]
CITY["bangalore"] = CITY["bengaluru"]
CITY["geneva"] = sorted(CITY["geneva"] + CITY["geneve"], key=lambda c: c.population, reverse=True)

# A city whose every namesake is abroad reads foreign only from this size up.
MIN_FOREIGN_CITY = 100_000

# Folded country name, alias or native name ("Deutschland") -> ISO code. A
# native name that is also a state or a big city elsewhere ("Columbia",
# "Salvador") is left out.
COUNTRY: dict[str, str] = {
    k: COUNTRY_TO_CC[v] for k, v in location_names().items() if v in COUNTRY_TO_CC
}
for _name, _country in NATIVE_NAMES.items():
    _cc = COUNTRY_TO_CC.get(_country, "")
    _clash = _name in SUB_NAME or any(
        c.cc != _cc and c.population >= MIN_FOREIGN_CITY for c in CITY.get(_name, [])
    )
    if _cc and len(_name) > 3 and _name.isascii() and not _clash:
        COUNTRY.setdefault(_name, _cc)


# Region and global words: never a drop, the judge reads them.
DOUBT = frozenset(
    {
        "latam",
        "latin america",
        "south america",
        "anz",
        "oceania",
        "africa",
        "global",
        "globally",
        "worldwide",
        "world",
        "anywhere",
        "international",
        "americas",
        "north america",
        "emea",
        "emeia",
        "apac",
        "asia",
        "asia pacific",
        "europe",
        "eu",
        "european union",
        "middle east",
        "mena",
        "gcc",
        "dach",
        "na",
        "n.a.",
        "amer",
        "noram",
        "nam",
        "benelux",
        "nordics",
        "eea",
    }
)
# US work authorization covers these territories; American Samoa and the minor
# outlying islands never drop and never count as the US.
US_LIKE = frozenset({"PR", "GU", "VI", "MP"})
DOUBT_CC = frozenset({"AS", "UM"})

# ---------------------------------------------------------------------
# Continents and region acronyms, for the countries a title's residency
# qualifier names ("(Remote - EU only)").
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
for _cc, _data in geonamescache.GeonamesCache().get_countries().items():
    _cont = _CONTINENT_CODE_TO_NAME.get(_data.get("continentcode", ""))
    _country_name = CC_TO_COUNTRY.get(_cc)
    if _cont and _country_name:
        CONTINENT_TO_COUNTRIES.setdefault(_cont, []).append(_country_name)

_MIDDLE_EAST_CC = frozenset(
    {"AE", "BH", "IL", "IQ", "IR", "JO", "KW", "LB", "OM", "PS", "QA", "SA", "SY", "TR", "YE"}
)
# Latin America outside the South American continent code.
_LATAM_EXTRA_CC = frozenset(
    {"MX", "GT", "BZ", "SV", "HN", "NI", "CR", "PA", "CU", "DO", "PR", "HT"}
)


def _countries_from_cc(codes: Iterable[str]) -> set[str]:
    return {name for cc in codes if (name := CC_TO_COUNTRY.get(cc))}


def _continents(*names: str) -> set[str]:
    out: set[str] = set()
    for name in names:
        out |= set(CONTINENT_TO_COUNTRIES.get(name, []))
    return out


_EMEA = _continents("Europe", "Africa") | _countries_from_cc(_MIDDLE_EAST_CC)
_LATAM = _continents("South America") | _countries_from_cc(_LATAM_EXTRA_CC)

REGION_TOKEN_COUNTRIES: dict[str, frozenset[str]] = {
    "emea": frozenset(_EMEA),
    "emeia": frozenset(_EMEA | _countries_from_cc({"IN"})),
    "apac": frozenset(_continents("Asia", "Oceania")),
    "latam": frozenset(_LATAM),
    "anz": frozenset(_countries_from_cc({"AU", "NZ"})),
    "europe": frozenset(_continents("Europe")),
    "asia": frozenset(_continents("Asia")),
    "latin america": frozenset(_LATAM),
}
