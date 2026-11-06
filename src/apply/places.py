"""The places a form label can name: countries from discovery's vocabulary
(pycountry's names plus aliases) and regions, with US states and look-alikes
masked, each spelled one way across the pipeline."""

from __future__ import annotations

import functools
import re
import unicodedata
from itertools import pairwise

# Countries a label can name come from discovery's vocabulary (pycountry's
# names plus its aliases), so a place is spelled one way across the pipeline.
# Regions have no country of their own: they answer from `default` unless the
# bank gives them a row.
REGIONS: dict[str, str] = {
    "European Union": r"european\s+union|\beu\b|\beea\b|\befta\b|\beurope\b|\bemea\b",
    "Latin America": r"latin\s+america|\blatam\b|south\s+america|central\s+america",
    "Asia Pacific": r"\bapac\b|asia[\s-]pacific",
}
# Places whose names contain a country's: masked before any country is looked
# for, so "New Mexico" is never Mexico and "Bank of America" never the US.
_NOT_COUNTRIES = (
    "new south wales",
    "new england",
    "papua new guinea",
    "bank of america",
    "north america",
    "latin america",
    "south america",
    "central america",
)
_SHORT_NAMES = {
    "us": "United States",
    "usa": "United States",
    "u.s.": "United States",
    "u.s.a.": "United States",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "uae": "United Arab Emirates",
}
_ALIASES = {
    "england": "United Kingdom",
    "scotland": "United Kingdom",
    "wales": "United Kingdom",
    "northern ireland": "United Kingdom",
    "great britain": "United Kingdom",
    "britain": "United Kingdom",
    "united states of america": "United States",
    "america": "United States",
    "holland": "Netherlands",
    "the netherlands": "Netherlands",
    "turkiye": "Turkey",
    "czech republic": "Czechia",
    "south korea": "Korea, Republic of",
    "korea": "Korea, Republic of",
    "russia": "Russian Federation",
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
_OR_BETWEEN = re.compile(r"\bor\b|/", re.IGNORECASE)
#: The job's country when its location names a place nothing could read.
UNKNOWN_COUNTRY = "(unknown)"


def _fold_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c)
    )


@functools.lru_cache(maxsize=1)
def _vocabulary() -> tuple[dict[str, str], tuple, re.Pattern, tuple]:
    import pycountry

    names: dict[str, str] = {}
    for c in pycountry.countries:
        for attr in ("name", "common_name", "official_name"):
            value = getattr(c, attr, "")
            if value:
                names[_fold_accents(value).casefold()] = c.name
    names = {k: v for k, v in names.items() if k not in _NOT_A_COUNTRY_WORD}
    names["turkey"] = "Turkey"  # the country name itself, once "Turkiye" is folded
    names.update(_ALIASES)
    names.update(_SHORT_NAMES)
    us_states = [s.name.casefold() for s in pycountry.subdivisions.get(country_code="US")]
    masked = sorted({*_NOT_COUNTRIES, *us_states}, key=len, reverse=True)
    codes = "|".join(
        sub.code.split("-")[1] for sub in pycountry.subdivisions.get(country_code="US")
    )
    mask = (
        re.compile(
            r"(?<![\w.])(?:" + "|".join(re.escape(m) for m in masked) + r")(?![\w])", re.IGNORECASE
        ),
        # "Columbia, MD", "Norfolk, VA": a place and a US state code.
        re.compile(r"\b[A-Z][\w.' -]{1,30},\s*(?:" + codes + r")\b"),
        # "Jersey City", "Mexico City": a city, not the country.
        re.compile(r"\b\w+\s+City\b"),
    )
    ordered = sorted((k for k in names if k != "us"), key=len, reverse=True)
    country = re.compile(
        r"(?<![\w.])(?:" + "|".join(re.escape(k) for k in ordered) + r")(?![\w])", re.IGNORECASE
    )
    regions = tuple((name, re.compile(p, re.IGNORECASE)) for name, p in REGIONS.items())
    return names, mask, country, regions


def canonical_place(text: str) -> str:
    """The canonical name for a country or region, or "" when it names neither."""
    raw = " ".join((text or "").split())
    if raw.casefold() == "default":
        return "default"
    names, _, _, regions = _vocabulary()
    key = _fold_accents(raw).casefold()
    if key in names:
        return names[key]
    if key.startswith("the ") and key[4:] in names:
        return names[key[4:]]
    for name, pattern in regions:
        if raw.casefold() == name.casefold() or pattern.fullmatch(raw):
            return name
    if raw == "US":
        return "United States"
    named = places_named(raw)
    return named[0] if len(named) == 1 else ""


def _place_hits(label: str) -> list[tuple[int, int, str]]:
    """(start, end, place) for every place in the accent-folded label."""
    names, mask, country, regions = _vocabulary()
    folded = _fold_accents(label)
    lowered = folded
    for pattern in mask:
        lowered = pattern.sub(lambda m: " " * len(m.group(0)), lowered)
    hits = [(m.start(), m.end(), names[m.group(0).casefold()]) for m in country.finditer(lowered)]
    hits += [
        (m.start(), m.end(), name)
        for name, pattern in regions
        for m in [pattern.search(folded)]
        if m
    ]
    # "US" in capitals only; in a label written all in capitals, only after "THE".
    us = r"(?<=\bTHE )US(?![\w])" if folded.isupper() else r"(?<![\w.])US(?![\w])"
    hits += [(m.start(), m.end(), "United States") for m in re.finditer(us, lowered)]
    return sorted(hits)


def places_named(label: str) -> list[str]:
    """Every country or region a label names, in order, each once."""
    seen: list[str] = []
    for _, _, name in _place_hits(label):
        if name not in seen:
            seen.append(name)
    return seen


def _either_place(label: str) -> bool:
    """Whether the label offers its places as alternatives: an "or" or a "/"
    between two of them ("the US or Canada", "US/Canada"), not anywhere ("now
    or in the future")."""
    hits = _place_hits(label)
    folded = _fold_accents(label)
    return any(_OR_BETWEEN.search(folded[a[1] : b[0]]) for a, b in pairwise(hits) if a[2] != b[2])
