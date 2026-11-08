"""The one country vocabulary: pycountry's names plus aliases, folded one way.
Addresses read `COUNTRY_NAMES` (`canonical_country`); form labels read
`label_names()`, the same table without native names and ordinary words."""

from __future__ import annotations

import gettext
import unicodedata

import pycountry


def fold_accents(text: str) -> str:
    """The text with its diacritics stripped ("Türkiye" -> "Turkiye")."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c)
    )


def _key(text: str) -> str:
    return fold_accents(text).lower().strip()


def _pycountry_names() -> dict[str, str]:
    """Every pycountry name, common name and official name -> its `name`."""
    names: dict[str, str] = {}
    for c in pycountry.countries:
        for attr in ("name", "common_name", "official_name"):
            value = getattr(c, attr, "")
            if value:
                names[_key(value)] = c.name
    return names


# Spellings both views read. A fuzzy fallback (pycountry search_fuzzy) is
# unsafe: it maps "North" to Cameroon and "Metro" to France.
_ALIASES = {
    "usa": "United States",
    "us": "United States",
    "u.s.": "United States",
    "u.s.a.": "United States",
    "united states of america": "United States",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "uae": "United Arab Emirates",
    "korea": "Korea, Republic of",
    "south korea": "Korea, Republic of",
    "holland": "Netherlands",
    "russia": "Russian Federation",
}
# Spellings only an address reads.
_ADDRESS_ALIASES = {
    "u.a.e.": "United Arab Emirates",
    "ksa": "Saudi Arabia",
    "drc": "Congo, The Democratic Republic of the",
    "dr congo": "Congo, The Democratic Republic of the",
    "democratic republic of congo": "Congo, The Democratic Republic of the",
    "ivory coast": "Côte d'Ivoire",
    "burma": "Myanmar",
    "macedonia": "North Macedonia",
}
# Spellings only a form label reads.
_LABEL_ALIASES = {
    "england": "United Kingdom",
    "scotland": "United Kingdom",
    "wales": "United Kingdom",
    "northern ireland": "United Kingdom",
    "great britain": "United Kingdom",
    "britain": "United Kingdom",
    "america": "United States",
    "the netherlands": "Netherlands",
    "turkiye": "Turkey",
    "czech republic": "Czechia",
    "vietnam": "Viet Nam",
    "taiwan": "Taiwan, Province of China",
    "iran": "Iran, Islamic Republic of",
    "bolivia": "Bolivia, Plurinational State of",
    "venezuela": "Venezuela, Bolivarian Republic of",
    "tanzania": "Tanzania, United Republic of",
    "moldova": "Moldova, Republic of",
    "syria": "Syrian Arab Republic",
    "laos": "Lao People's Democratic Republic",
    "republic of ireland": "Ireland",
}
# Country names that are also ordinary words or US places in a form label.
_NOT_A_COUNTRY_WORD = frozenset(
    {
        "jersey",
        "georgia",
        "chad",
        "jordan",
        "guinea",
        "niger",
        "turkey",
        "man",
        "norfolk island",
        "christmas island",
    }
)
# Native names ("Deutschland", "Espana") an address may use, from pycountry's
# own iso-codes catalogs, for the major job-market locales.
_MAJOR_LOCALES = (
    "de",
    "fr",
    "es",
    "nl",
    "it",
    "pt",
    "pl",
    "tr",
    "ru",
    "zh",
    "ja",
    "ko",
    "ar",
    "sv",
    "da",
    "fi",
    "cs",
    "ro",
    "hu",
    "el",
    "he",
    "th",
    "vi",
    "id",
    "uk",
)

COUNTRY_NAMES: dict[str, str] = _pycountry_names()
for _lang in _MAJOR_LOCALES:
    try:
        _cat = gettext.translation("iso3166-1", pycountry.LOCALES_DIR, languages=[_lang])
    except FileNotFoundError:
        continue
    for _c in pycountry.countries:
        _native = _cat.gettext(_c.name)
        if _native != _c.name:
            COUNTRY_NAMES.setdefault(_key(_native), _c.name)
COUNTRY_NAMES.update(_ALIASES)
COUNTRY_NAMES.update(_ADDRESS_ALIASES)

CC_TO_COUNTRY: dict[str, str] = {_c.alpha_2: _c.name for _c in pycountry.countries}
COUNTRY_TO_CC: dict[str, str] = {name: cc for cc, name in CC_TO_COUNTRY.items()}


def canonical_country(name: str) -> str | None:
    """The canonical name for a country as the parser returns it, or None."""
    return COUNTRY_NAMES.get(_key(name or ""))


def location_names() -> dict[str, str]:
    """Folded spelling -> country, for a job location: pycountry's names and
    every alias, no native names."""
    names = _pycountry_names()
    known = set(names.values())
    aliases = {**_ALIASES, **_ADDRESS_ALIASES, **_LABEL_ALIASES}
    return names | {k: v for k, v in aliases.items() if v in known}


def label_names() -> dict[str, str]:
    """Folded spelling -> country, for the countries a form label can name."""
    names = {k: v for k, v in _pycountry_names().items() if k not in _NOT_A_COUNTRY_WORD}
    names["turkey"] = "Turkey"  # the country name itself, once "Turkiye" is folded
    names.update(_ALIASES)
    names.update(_LABEL_ALIASES)
    return names
