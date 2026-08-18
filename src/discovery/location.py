from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

import geonamescache
import pycountry
from postal.parser import parse_address


def _fold(s: str) -> str:
    """Lowercase, strip diacritics and surrounding whitespace so scraped
    ASCII forms match accented canonical spellings (e.g. "Iasi" ->
    "Iaşi", "Karnataka" -> "Karnātaka") regardless of stray whitespace a
    pre-split segment can leave on a libpostal component."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    ).lower().strip()


@dataclass
class LocationParse:
    country: str
    state: str
    city: str
    remote: bool
    # Populated only when multiple distinct countries were found and none
    # could be picked without guessing (a real multi-region listing). The
    # caller (cleaning.py) decides keep-vs-drop against its own allowlist —
    # no country is privileged here, unlike the old US-hardcoded design.
    candidate_countries: frozenset[str] = field(default_factory=frozenset)


gc = geonamescache.GeonamesCache()
_ALL_CITIES = gc.get_cities()
_GC_COUNTRIES = gc.get_countries()  # only used for continentcode, below

# ---------------------------------------------------------------------
# Countries — worldwide, from pycountry (ISO 3166-1).
# ---------------------------------------------------------------------

COUNTRY_NAMES: dict[str, str] = {}
for _c in pycountry.countries:
    COUNTRY_NAMES[_fold(_c.name)] = _c.name
    if hasattr(_c, "common_name"):
        COUNTRY_NAMES[_fold(_c.common_name)] = _c.name
    if hasattr(_c, "official_name"):
        COUNTRY_NAMES[_fold(_c.official_name)] = _c.name

# Colloquial aliases pycountry's own name strings don't cover.
COUNTRY_NAMES.update({
    "usa": "United States",
    "us": "United States",
    "u.s.": "United States",
    "u.s.a.": "United States",
    "united states of america": "United States",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
})

CC_TO_COUNTRY: dict[str, str] = {_c.alpha_2: _c.name for _c in pycountry.countries}
COUNTRY_TO_CC: dict[str, str] = {name: cc for cc, name in CC_TO_COUNTRY.items()}

# ---------------------------------------------------------------------
# Subdivisions (states/provinces/regions) — worldwide, from pycountry
# (ISO 3166-2). Codes are NOT globally unique ("KA" is both India's
# Karnataka and Georgia's Kakheti), so a bare code only resolves when it's
# unambiguous worldwide or corroborated by a country already found in the
# same segment.
# ---------------------------------------------------------------------

SUBDIVISIONS_BY_NAME: dict[str, object] = {}
SUBDIVISIONS_BY_CODE: dict[str, list] = {}
for _s in pycountry.subdivisions:
    SUBDIVISIONS_BY_NAME.setdefault(_fold(_s.name), _s)
    _bare = _s.code.split("-", 1)[-1].upper()
    SUBDIVISIONS_BY_CODE.setdefault(_bare, []).append(_s)

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
    "AF": "Africa", "AS": "Asia", "EU": "Europe",
    "NA": "North America", "OC": "Oceania", "SA": "South America",
    "AN": "Antarctica",
}

CONTINENT_TO_COUNTRIES: dict[str, list[str]] = {}
for _cc, _data in _GC_COUNTRIES.items():
    _cont = _CONTINENT_CODE_TO_NAME.get(_data.get("continentcode", ""))
    _country_name = CC_TO_COUNTRY.get(_cc)
    if _cont and _country_name:
        CONTINENT_TO_COUNTRIES.setdefault(_cont, []).append(_country_name)

# ---------------------------------------------------------------------
# Cities — worldwide (not filtered to any one country). Used only as a
# last-resort "lone place name -> what country/city is this" convenience;
# never used to inject a phantom country signal from a substring the way
# the old dictionary matcher did (that mechanism is what caused the
# Jakarta/Rome/Athens collisions and has been deleted, not ported).
# ---------------------------------------------------------------------

CITIES_BY_NAME: dict[str, list[dict]] = {}
for _city in _ALL_CITIES.values():
    CITIES_BY_NAME.setdefault(_fold(_city["name"]), []).append(_city)
for _lst in CITIES_BY_NAME.values():
    _lst.sort(key=lambda c: c.get("population", 0), reverse=True)

# geonames spells NYC "New York City"; postings write "New York".
_CITY_NAME_ALIASES = {"new york": "new york city"}

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
    "metropolitan area", "bay area", "metro area", "region", "area",
)

# Same idea, leading side: geonames lists the parent city ("Jakarta") but
# not every compass-point district name a job board writes ("Central
# Jakarta", "South Jakarta"). This is intentionally NOT a general
# leading-word-dropping fallback either: it only ever strips one of these
# specific, closed compass/scale words, never an arbitrary word, so it
# can't reintroduce the collision this rewrite fixes (an incidental common
# word like "Central" is only ever stripped here, never tested on its own).
_DISTRICT_PREFIXES = ("central ", "north ", "south ", "east ", "west ", "greater ")


def _validate_country(text: str) -> str:
    return COUNTRY_NAMES.get(_fold(text), "")


def _validate_state(
    text: str,
    hint_countries: set[str],
    allow_bare_code: bool = True,
    sibling_texts: tuple[str, ...] = (),
) -> tuple[str, str, frozenset[str]]:
    """Return (code, owning_country, ambiguous_pool).

    `code`/`owning_country` are empty when not confidently resolved.
    `ambiguous_pool` is non-empty only when the text really is a
    subdivision-code collision that couldn't be narrowed down (as opposed
    to not being a code at all) — the caller uses it to avoid letting an
    unrelated global city match silently override a plausible-but-
    unconfirmed state signal (see the "Warrington, PA" case in
    _mine_segment_signals: geonames has no small-town "Warrington, PA"
    entry, only the much bigger UK one, so if the ambiguous "PA" signal
    were simply dropped, an unconstrained city lookup on "Warrington"
    would confidently resolve to the UK instead of staying unresolved).

    `allow_bare_code` gates the short-code path only (e.g. "TX", "KA") — a
    full subdivision name is trusted standalone either way. A bare 2-3
    letter code with no other context in the segment is too weak a signal
    on its own (mirrors the old design's "a code counts only after a
    comma+token boundary" rule) — pass False when the code is the segment's
    only component.

    `sibling_texts` are the segment's other, not-yet-claimed component
    texts (typically the city). They're the last, strongest disambiguator
    for a code collision that survives every other check — including
    between two countries that both conventionally use bare codes ("WA" is
    both Washington and Western Australia; "MA" is both Massachusetts and
    Brazil's Maranhão) — by checking which candidate country the sibling
    text is actually a real city in.
    """
    sub = SUBDIVISIONS_BY_NAME.get(_fold(text))
    if sub is not None:
        return sub.code.split("-", 1)[-1], CC_TO_COUNTRY.get(sub.country_code, ""), frozenset()

    if not allow_bare_code:
        return "", "", frozenset()

    bare = text.strip().upper()
    if len(bare) > _SHORT_CODE_MAX_LEN:
        return "", "", frozenset()

    candidates = SUBDIVISIONS_BY_CODE.get(bare, [])
    if len(candidates) == 1:
        return bare, CC_TO_COUNTRY.get(candidates[0].country_code, ""), frozenset()
    if len(candidates) <= 1:
        return "", "", frozenset()

    if hint_countries:
        narrowed = [
            s for s in candidates
            if CC_TO_COUNTRY.get(s.country_code, "") in hint_countries
        ]
        if len(narrowed) == 1:
            return bare, CC_TO_COUNTRY.get(narrowed[0].country_code, ""), frozenset()

    conventional = [s for s in candidates if s.country_code in _COMMONLY_ABBREVIATED_CC]
    if len(conventional) == 1:
        return bare, CC_TO_COUNTRY.get(conventional[0].country_code, ""), frozenset()

    pool = conventional if conventional else candidates
    if sibling_texts:
        matched_cc: set[str] = set()
        for sib in sibling_texts:
            for cand in pool:
                if _city_lookup(sib, CC_TO_COUNTRY.get(cand.country_code, "")):
                    matched_cc.add(cand.country_code)
        if len(matched_cc) == 1:
            return bare, CC_TO_COUNTRY.get(next(iter(matched_cc)), ""), frozenset()

    ambiguous_pool = frozenset(CC_TO_COUNTRY.get(s.country_code, "") for s in pool) - {""}
    return "", "", ambiguous_pool


def _city_lookup(text: str, country_hint: str = "") -> dict | None:
    folded = _fold(text)
    candidates = CITIES_BY_NAME.get(folded) or CITIES_BY_NAME.get(
        _CITY_NAME_ALIASES.get(folded, "")
    )
    if not candidates:
        for suffix in _GENERIC_AREA_SUFFIXES:
            if folded.endswith(" " + suffix):
                return _city_lookup(folded[: -len(suffix) - 1], country_hint)
        for prefix in _DISTRICT_PREFIXES:
            if folded.startswith(prefix) and folded != prefix.strip():
                return _city_lookup(folded[len(prefix):], country_hint)
        return None

    if not country_hint:
        # No country established yet, so this match IS the country signal
        # -- require real prominence. Found by testing, not hypothetical:
        # a bare "PA"/"WA"/"Asia" (almost always meant as a US state
        # abbreviation or a continent, not a place name) each happen to
        # also be a real, tiny place (Pa, Burkina Faso pop. 15k; Wa, Ghana
        # pop. 78k; Asia, Philippines pop. 24k) that would otherwise hijack
        # the country resolution. Once a country is already known from a
        # state/country signal, this floor doesn't apply -- a small same-
        # country city match there only fills in display text, it can't
        # corrupt the country the row resolves to.
        candidates = [c for c in candidates if c.get("population", 0) >= _MIN_LONE_CITY_POPULATION]
        if not candidates:
            return None

    if country_hint:
        cc = COUNTRY_TO_CC.get(country_hint)
        for c in candidates:
            if c["countrycode"] == cc:
                return c
        # A country is already known from a state/country signal and this
        # name isn't a real city there -> don't guess a different country.
        return None
    return candidates[0]


_SEPARATOR_RE = re.compile(r";|\bor\b|/", re.IGNORECASE)


def _mine_segment_signals(segment: str):
    """Collect every (country, state, city) signal libpostal's own
    role-tagging surfaces in one segment. Only whole libpostal-emitted
    components are ever tested against the canonical tables — never raw
    substrings/n-grams of the segment — which is what stops an incidental
    word inside a real place name ("Central" in "Central Jakarta") from
    being tested on its own and colliding with an unrelated small town."""
    components = parse_address(segment)

    # Repair: libpostal occasionally folds a known city+region pair into a
    # single component that still contains a comma (observed: "Paris, TX",
    # likely because "Paris, Texas" appears as a fixed phrase in its
    # training corpus). Re-split and let it flow through the normal
    # validation path below rather than trusting the merge.
    if len(components) == 1 and "," in components[0][0]:
        head, _, tail = components[0][0].rpartition(",")
        head, tail = head.strip(), tail.strip()
        if head and tail:
            components = [(head, "city"), (tail, "state")]

    countries: set[str] = set()
    states: set[tuple[str, str]] = set()   # (code, owning_country)
    cities: set[tuple[str, str, str]] = set()  # (canonical_name, country, admin1)
    claimed: set[int] = set()

    for i, (text, typ) in enumerate(components):
        if typ == "country":
            c = _validate_country(text)
            if c:
                countries.add(c)
                claimed.add(i)

    restrict_city_countries: set[str] = set()
    for i, (text, typ) in enumerate(components):
        if i in claimed or typ != "state":
            continue
        # A word that is both a real subdivision's full name AND a real
        # country's full name ("Georgia") is genuinely ambiguous when it's
        # the segment's only component -- no anchoring city or other
        # context to pick a side, unlike "Atlanta, Georgia"/"Tbilisi,
        # Georgia", where libpostal's own role-tagging already resolved it
        # correctly (state vs. country) using the surrounding text. Surface
        # both as candidate countries instead of confidently picking one.
        if len(components) == 1:
            also_country = _validate_country(text)
            if also_country:
                code, owner, _pool = _validate_state(text, countries, allow_bare_code=False)
                if owner:
                    countries.add(owner)
                    countries.add(also_country)
                claimed.add(i)
                continue
        siblings = tuple(t for j, (t, _) in enumerate(components) if j != i and j not in claimed)
        code, owner, ambiguous_pool = _validate_state(
            text, countries, allow_bare_code=len(components) > 1, sibling_texts=siblings,
        )
        if code and owner:
            states.add((code, owner))
            countries.add(owner)
            claimed.add(i)
        elif ambiguous_pool:
            # A plausible-but-unconfirmed state-code collision ("PA" could
            # be Pennsylvania or Brazil's Pará) must not be silently
            # dropped and then overridden by an unconstrained global city
            # lookup on a sibling component -- see _validate_state's
            # docstring for the "Warrington, PA" case this guards against.
            restrict_city_countries |= set(ambiguous_pool)

    country_hint = next(iter(countries)) if len(countries) == 1 else ""
    for i, (text, _typ) in enumerate(components):
        if i in claimed:
            continue
        if country_hint:
            city = _city_lookup(text, country_hint)
        elif restrict_city_countries:
            city = next(
                (c for cc in restrict_city_countries if (c := _city_lookup(text, cc))),
                None,
            )
        else:
            city = _city_lookup(text)
        if city:
            country = CC_TO_COUNTRY.get(city["countrycode"], "")
            if country:
                cities.add((city["name"], country, city.get("admin1code", "")))

    return countries, states, cities


def parse_location(raw: str) -> LocationParse:
    if not raw:
        return LocationParse("", "", "", False)

    remote = "remote" in raw.lower()

    segments = [s.strip() for s in _SEPARATOR_RE.split(raw) if s.strip()]

    found_countries: set[str] = set()
    found_states: set[tuple[str, str]] = set()
    found_cities: set[tuple[str, str, str]] = set()

    for seg in segments:
        c, s, ci = _mine_segment_signals(seg)
        found_countries |= c
        found_states |= s
        found_cities |= ci

    # A bare city name (no separately-tagged state/country) still implies
    # its country, same convenience as the old design, generalized worldwide.
    for _name, country, _admin1 in found_cities:
        found_countries.add(country)

    if not found_countries:
        return LocationParse("", "", "", remote)

    if len(found_countries) > 1:
        # A genuine multi-region listing. No country is privileged here —
        # the caller decides keep-vs-drop against its own allowlist.
        return LocationParse("", "", "", remote, candidate_countries=frozenset(found_countries))

    country = next(iter(found_countries))

    states_for_country = {code for code, owner in found_states if owner == country}
    if len(states_for_country) > 1:
        return LocationParse("", "", "", remote)
    state = next(iter(states_for_country)) if states_for_country else ""

    cities_for_country = [(name, admin1) for name, c, admin1 in found_cities if c == country]
    city = ""
    if cities_for_country:
        if state:
            matching = [(n, a) for n, a in cities_for_country if a == state]
            if len(matching) == 1:
                city = matching[0][0]
            elif not matching and len(cities_for_country) == 1:
                # City didn't carry admin1 info but there's no ambiguity.
                city = cities_for_country[0][0]
        elif len(cities_for_country) == 1:
            name, admin1 = cities_for_country[0]
            city = name
            if country == "United States" and admin1:
                state = admin1
        else:
            # Multiple distinct cities, no state to disambiguate -> a
            # multi-site listing within one country, kept for review.
            return LocationParse("", "", "", remote)

    # City display text is only ever populated for the United States, same
    # convenience the old design offered (US-only geonames admin1 codes are
    # what make the "fill state from city" trick reliable; other countries'
    # admin1 codes aren't consistently shaped the same way).
    if country != "United States":
        city = ""

    return LocationParse(country, state, city, remote)
