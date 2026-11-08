"""Reads a free-text job location into the countries it can mean.

Each part of the text keeps every reading it has (a country or alias, an
uppercase code, a state, every city namesake); neighbours and a named country
narrow them. Unsure means keep: a location drops only when something reads
clearly foreign and nothing reads allowed or doubtful.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from applyourself.shared.countries import COUNTRY_TO_CC, canonical_country, fold_accents
from applyourself.shared.location.places import (
    CITY,
    CODE2,
    COUNTRY,
    DOUBT,
    DOUBT_CC,
    MIN_FOREIGN_CITY,
    SUB_CODE,
    SUB_NAME,
    US_LIKE,
    City,
    fold,
    saint,
)

_NOISE_RE = re.compile(
    r"(?i)\b(remote(ly)?|hybrid|on-?site|in[- ]office|offices?|hq|headquarters|based|only|"
    r"within|work(ing)?|from|home|wfh|flexible|in|the|of|pan|any|anywhere|nationwide|"
    r"country|location|locations|area|metro|metropolitan|region|greater|incl\.?|including|"
    r"across|available|multiple|various|or|and|miles|\d+)\b"
)
_DOUBT_WORD_RE = re.compile(r"(?i)\b(anywhere|global(ly)?|worldwide|international)\b")
_SEP_RE = re.compile(r"[,;/|()\[\]{}+•·\n]|\s[-–—]\s|[–—]|\s(?:or|OR)\s|:")
_SUB_SEP_RE = re.compile(r"(?i)\s+and\s+|\s*&\s*|-")
_STRIP = " .-'\"*"
# A multi-site listing's own separators ("Austin, TX; Seattle, WA"). `OR` stays a state.
LIST_SEPARATOR_RE = re.compile(r";|(?-i:\bor\b)|/|\||\s-\s", re.IGNORECASE)


@dataclass
class Part:
    """One piece of a location. `R`: the readings the verdict uses ("?" is
    doubt); `named`: the country it names; `place`: countries it is a state or
    city in; `subs`: its state code per country; `cities`: its city namesakes."""

    text: str
    R: set[str] = field(default_factory=set)
    kind: str = ""
    named: set[str] = field(default_factory=set)
    place: set[str] = field(default_factory=set)
    subs: dict[str, str] = field(default_factory=dict)
    cities: list[City] = field(default_factory=list)
    full: bool = False  # names its country in full ("Germany", "Deutschland")

    @property
    def city(self) -> set[str]:
        return {c.cc for c in self.cities}

    @property
    def big(self) -> set[str]:
        return {c.cc for c in self.cities if c.population >= MIN_FOREIGN_CITY}

    def read(self) -> bool:
        return bool(self.kind or self.place or self.named)


@dataclass(frozen=True)
class Reading:
    """keep: the verdict. countries: the countries the verdict read (an allowed
    namesake hides the foreign ones). certain: the one country among them, with
    no region word. sure: the one country every reading can mean, so the text
    is one place. place: the country to show the location in. alone: the
    countries some part can only mean. spelled: the countries named in full.
    name_or_town: a foreign country's name is also an allowed town ("Lebanon").
    named_allowed: a part names an allowed country outright. loose: a part
    reads nothing. city/state: the place in `place`."""

    keep: bool
    countries: frozenset[str] = frozenset()
    certain: str = ""
    sure: str = ""
    place: str = ""
    alone: frozenset[str] = frozenset()
    spelled: frozenset[str] = frozenset()
    name_or_town: bool = False
    doubt: bool = False
    named_allowed: bool = False
    remote: bool = False
    loose: bool = False
    city: str = ""
    state: str = ""


def allowed_codes(names: Iterable[str]) -> frozenset[str]:
    """ISO codes of the allowed countries; the US brings its territories."""
    codes = {COUNTRY_TO_CC.get(canonical_country(n) or n, "") for n in names} - {""}
    if "US" in codes:
        codes |= US_LIKE
    return frozenset(codes)


def _is_code(p: str, n: int) -> bool:
    return len(p) == n and p.isalpha() and p.isupper()


def _classify(p: str, allowed: frozenset[str], closing: bool = False, first: bool = True) -> Part:
    f = fold(p)
    if closing and len(p) == 2 and p.islower() and p.upper() in CODE2:
        # Indeed's "Lyon, Auvergne-Rhone-Alpes, fr": a closing lowercase code is the country.
        cc = CODE2[p.upper()]
        return Part(p, {cc}, "code", {cc})
    if not f:
        return Part(p)
    if f in DOUBT:
        return Part(p, {"?"}, "doubt")
    named: set[str] = set()
    kind = ""
    if f in COUNTRY:
        named, kind = {COUNTRY[f]}, "country"
    elif _is_code(p, 2) and p in CODE2:
        named, kind = {CODE2[p]}, "code"
    subs = dict(SUB_NAME.get(f, {}))
    cities = CITY.get(saint(f), [])
    if kind == "country":
        # A country's name is that country, unless it is also a town in an allowed
        # country and the country is not: "Lebanon" (Lebanon, PA) is unsure, so it keeps.
        cities = [c for c in cities if c.cc in allowed and not named & allowed]
    if (_is_code(p, 2) or _is_code(p, 3)) and kind != "country":
        # A leading code ("BE (BE100)", "CH") is a state only when a US state;
        # after a place, any state.
        sc = SUB_CODE.get(p, set())
        for cc in sc if not first or not kind else sc & {"US"}:
            subs.setdefault(cc, p)
    place = set(subs) | {c.cc for c in cities}
    R = set(named)
    if place & allowed:
        R |= place & allowed
        kind = "amb" if kind else "place"
    elif not kind and cities and cities[0].population >= MIN_FOREIGN_CITY:
        R, kind = set(place), "city"  # every namesake abroad and one is big: "Toronto"
    return Part(p, R, kind, named, place, subs, list(cities), f in COUNTRY)


def _parts(raw: str, allowed: frozenset[str]) -> list[Part]:
    out: list[Part] = []
    chunks = [c.strip(_STRIP) for c in _SEP_RE.split(raw)]
    chunks = [c for c in chunks if c]
    for i, p in enumerate(chunks):
        d = _classify(p, allowed, closing=(i == len(chunks) - 1 and i >= 2), first=(i == 0))
        if not d.read():
            q = re.sub(r"\s+", " ", _NOISE_RE.sub(" ", p)).strip(" .-")
            if not q and _DOUBT_WORD_RE.search(p):
                d = Part(p, {"?"}, "doubt")
            elif q and q != p:
                d = replace(_classify(q, allowed), text=p)
            if not d.read() and _SUB_SEP_RE.search(q or p):
                # "Canada & Mexico", "Dallas-Fort Worth": only when the whole reads nothing.
                for h in _SUB_SEP_RE.split(q or p):
                    if h.strip():
                        out += _parts(h, allowed)
                continue
            if not d.read() and (inside := _named_inside(p, allowed)):
                out += inside  # "Remote US eligible", "US also"
                continue
        out.append(d)
    return out


# Words that are a country's name but mean something else in a location.
_NOT_COUNTRY_INSIDE = frozenset(
    {"america", "jersey", "georgia", "chad", "jordan", "guinea", "niger", "turkey", "man"}
)


@functools.cache
def _country_word_re() -> re.Pattern[str]:
    names = sorted(
        (k for k in COUNTRY if len(k) > 3 and k not in _NOT_COUNTRY_INSIDE), key=len, reverse=True
    )
    return re.compile(
        r"\b(?:US|USA|UK|UAE)\b|(?i:\b(?:" + "|".join(re.escape(n) for n in names) + r")\b)"
    )


def _named_inside(p: str, allowed: frozenset[str]) -> list[Part]:
    """The allowed countries a chunk that reads nothing names in passing, whole
    words only. Only ever a keep: "Beth Israel Lahey Health" stays unread."""
    words = (_classify(m.group(0), allowed) for m in _country_word_re().finditer(fold_accents(p)))
    return [replace(d, text=p) for d in words if d.named & allowed]


def _narrow(ps: list[Part], allowed: frozenset[str]) -> list[set[str] | None]:
    """Per part, the readings its neighbours or a named country settle, else None."""
    guard = {"US"} & allowed
    out: list[set[str] | None] = [None] * len(ps)
    for i in range(len(ps) - 1):
        # "Toronto, ON", "Bangkok, TH", "Birmingham, UK": two neighbours that fit one country.
        a, b = ps[i], ps[i + 1]
        sa, sb = a.place - a.city, b.place - b.city
        # A named country binds any city; a state binds only a big city.
        x = (a.city & b.named) | (a.named & b.city)
        x |= (a.big & sb) | (sa & b.big) | (a.named & sb) | (sa & b.named)
        x |= ((a.city & sb) | (sa & b.city)) & allowed  # "Windsor, CA": an allowed namesake fits
        if x and not x & allowed:
            for side in (a, b):
                # An allowed state ("MI", "Georgia") yields only to the country it names.
                if (side.place - side.city) & guard:
                    x &= side.named
        if x:
            out[i] = set(x)
            out[i + 1] = set(x)
    explicit: set[str] = set()
    for d in ps:
        if d.kind in ("country", "code"):
            explicit |= d.named
    closing = bool(ps) and ps[-1].kind in ("country", "code") and bool(ps[-1].named & explicit)
    for i, d in enumerate(ps):
        # A named country binds the places in it; a US state yields only to a closing country.
        guarded = (d.place - d.city) & guard and not closing
        if d.kind not in ("country", "code") and (d.place | d.named) & explicit and not guarded:
            out[i] = (d.place | d.named) & explicit
    return out


def _where(ps: list[Part], cc: str, text: str) -> tuple[str, str]:
    """(city, state) of a location in country `cc`, each "" when not one place."""
    city_parts = [i for i, d in enumerate(ps) if cc in d.city]
    subs = {i: d.subs[cc] for i, d in enumerate(ps) if cc in d.subs}
    city_i = city_parts[0] if city_parts else None
    # A name that is a city and a state ("New York") is the state only right after the city.
    after = f"{ps[city_i].text}, " if city_i is not None else "\0"
    as_state = {
        i for i in subs if i != city_i and (i not in city_parts or after + ps[i].text in text)
    }
    states = {subs[i] for i in as_state}
    others = {fold(ps[i].text) for i in city_parts if i != city_i and i not in as_state}
    if len(states) > 1 or (city_i is not None and others - {fold(ps[city_i].text)}):
        return "", ""  # several states or cities: the country is certain, no one place
    state = next(iter(states), "")
    if city_i is None:
        return "", state
    there = [c for c in ps[city_i].cities if c.cc == cc]
    if state:
        # The named state settles the namesake; none there is a town the tables
        # lack ("Knoxville, IA"), shown as written.
        pick = next((c for c in there if c.admin1 == state), None) if cc == "US" else there[0]
        return (pick.name if pick else ps[city_i].text), state
    pick = there[0]
    return pick.name, pick.admin1 if cc == "US" else ""


@functools.lru_cache(maxsize=50_000)
def read_location(raw: str, allowed: frozenset[str] = frozenset()) -> Reading:
    """What `raw` says about its country, against the allowed ISO codes."""
    if not isinstance(raw, str) or not raw.strip():
        return Reading(True)
    remote = "remote" in raw.lower()
    text = "".join(  # emoji and flags
        c for c in unicodedata.normalize("NFKC", raw) if unicodedata.category(c) != "So"
    )
    ps = _parts(text, allowed)
    narrowed = _narrow(ps, allowed)
    R = [n if n is not None else set(d.R) for d, n in zip(ps, narrowed, strict=True)]
    found = set().union(*R) if R else set()
    keep = not found or "?" in found or bool(found & allowed) or bool(found & DOUBT_CC)
    doubt = "?" in found
    found.discard("?")
    certain = next(iter(found)) if len(found) == 1 and not doubt else ""
    # What each read part can still mean, beyond the allowlist: "Paris" alone is
    # certain to an allowlist of one, but not one place.
    means: set[str] = set()
    alone: set[str] = set()
    for d, n, r in zip(ps, narrowed, R, strict=True):
        if r:
            part = (n if n is not None else (d.named | d.place or r)) - {"?"}
            means |= part
            if len(part) == 1:
                alone |= part
    sure = next(iter(means)) if len(means) == 1 and not doubt else ""
    # Shown as a place: the sure country, else the one read country where each
    # lone city is biggest ("San Francisco", "Amsterdam"; not "Paris" to a US allowlist).
    rows = [(d, n, r) for d, n, r in zip(ps, narrowed, R, strict=True) if r]
    bigs = {d.cities[0].cc for d, n, _ in rows if n is None and d.cities}
    cand = certain or (next(iter(bigs)) if len(bigs) == 1 else "")
    fits = cand in found and not doubt
    for d, n, r in rows:
        meaning = n if n is not None else (d.named | d.place or r)
        fits = (
            fits and cand in meaning and (n is not None or not d.cities or d.cities[0].cc == cand)
        )
        fits = fits and (n is not None or not d.named or cand in d.named)  # "Lebanon" alone
    place = sure or (cand if fits else "")
    city, state = _where(ps, place, text) if place else ("", "")
    named_allowed = any(d.kind in ("country", "code", "amb") and d.named & allowed for d in ps)
    loose = any(not d.R and _NOISE_RE.sub(" ", d.text).strip(" .-") for d in ps)
    spelled = set().union(*(d.named for d in ps if d.kind == "country")) if ps else set()
    # A foreign country's name that is also an allowed town ("Lebanon").
    name_or_town = any(d.full and d.kind == "amb" and not d.named & allowed for d in ps)
    return Reading(
        keep,
        frozenset(found),
        certain,
        sure,
        place,
        frozenset(alone),
        frozenset(spelled),
        name_or_town,
        doubt,
        named_allowed,
        remote,
        loose,
        city,
        state,
    )
