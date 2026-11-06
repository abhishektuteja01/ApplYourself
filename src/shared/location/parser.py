"""Reads a free-text job location into country / state / city."""

from __future__ import annotations

import functools
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

import pycountry

from src.shared.countries import CC_TO_COUNTRY, COUNTRY_NAMES, COUNTRY_TO_CC
from src.shared.location.places import (
    _CITY_NAME_ALIASES,
    _COMMONLY_ABBREVIATED_CC,
    _DISTRICT_PREFIXES,
    _GENERIC_AREA_SUFFIXES,
    _MIN_LONE_CITY_POPULATION,
    _SHORT_CODE_MAX_LEN,
    CITIES_BY_NAME,
    REGION_TOKEN_COUNTRIES,
    SUBDIVISIONS_BY_CODE,
    SUBDIVISIONS_BY_NAME,
    _fold,
    _normalize_saint,
)

# libpostal is an optional install (`uv sync --group discovery`) because the
# `postal` binding is sdist-only and compiles against a C library no distro
# packages. Only address parsing needs it, so the import is deferred to the
# one call site: importing this module -- and therefore the store, run.py
# and every board lane -- stays possible without it, and /score
# and /tailor keep working on a clone that never installs it.
_POSTAL_MISSING = (
    "libpostal is required to parse job locations during discovery, and it "
    "is not installed.\n"
    "  1. Install the C library: `brew install libpostal` (macOS), or build "
    "from source on Linux (github.com/openvenues/libpostal) -- no distro "
    "packages it.\n"
    "  2. Install the Python binding: `uv sync --group discovery`.\n"
    "Only the discovery/scraping path needs this; /score, /tailor and the "
    "rest of the pipeline run without it."
)

_parse_address: Callable[[str], list[tuple[str, str]]] | None = None


def _get_parse_address() -> Callable[[str], list[tuple[str, str]]]:
    """Resolve `postal.parser.parse_address` on first use, converting the
    ModuleNotFoundError a clone without libpostal would otherwise hit into an
    actionable message. No fallback parser: a degraded parse would silently
    change which rows survive the location allowlist."""
    global _parse_address
    if _parse_address is None:
        try:
            from postal.parser import parse_address
        except ImportError as exc:  # pragma: no cover - needs postal absent
            raise RuntimeError(_POSTAL_MISSING) from exc
        _parse_address = parse_address
    return _parse_address


# frozen: instances are returned by reference out of a cached parse_location.
@dataclass(frozen=True)
class LocationParse:
    country: str
    state: str
    city: str
    remote: bool
    # Populated only when multiple distinct countries were found and none
    # could be picked without guessing (a real multi-region listing). The
    # caller (Allowlist.check) decides keep-vs-drop against its own allowlist —
    # no country is privileged here.
    candidate_countries: frozenset[str] = field(default_factory=frozenset)


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
    name_candidates = SUBDIVISIONS_BY_NAME.get(_fold(text), [])
    if len(name_candidates) == 1:
        sub = name_candidates[0]
        owner = CC_TO_COUNTRY.get(sub.country_code, "")
        if hint_countries and owner not in hint_countries:
            # A single name match that flatly contradicts an already-
            # established country isn't confidently this subdivision --
            # observed with libpostal splitting "New York, USA" into
            # components ("new", city) + ("york", state), where "York" is
            # a real but unrelated UK subdivision. Don't let a name
            # collision silently override a corroborated country.
            return "", "", frozenset({owner})
        return sub.code.split("-", 1)[-1], owner, frozenset()
    if len(name_candidates) > 1:
        sub, ambiguous_pool = _disambiguate_subdivisions(
            name_candidates,
            hint_countries,
            sibling_texts,
            use_conventional_tiebreak=False,
        )
        if sub is not None:
            return sub.code.split("-", 1)[-1], CC_TO_COUNTRY.get(sub.country_code, ""), frozenset()
        return "", "", ambiguous_pool

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

    sub, ambiguous_pool = _disambiguate_subdivisions(
        candidates,
        hint_countries,
        sibling_texts,
        use_conventional_tiebreak=True,
    )
    if sub is not None:
        return bare, CC_TO_COUNTRY.get(sub.country_code, ""), frozenset()
    return "", "", ambiguous_pool


def _disambiguate_subdivisions(
    candidates: list[Any],
    hint_countries: set[str],
    sibling_texts: tuple[str, ...],
    use_conventional_tiebreak: bool,
) -> tuple[Any, frozenset[str]]:
    """Shared narrowing logic for a collision pool of pycountry subdivisions
    that share the same name or the same bare code: known-country hint,
    then (codes only) the "conventionally abbreviated this way" tie-break,
    then sibling-city corroboration, else surface the pool as ambiguous.
    Returns (matched_subdivision_or_None, ambiguous_pool)."""
    if hint_countries:
        narrowed = [
            s for s in candidates if CC_TO_COUNTRY.get(s.country_code, "") in hint_countries
        ]
        if len(narrowed) == 1:
            return narrowed[0], frozenset()

    pool = candidates
    if use_conventional_tiebreak:
        conventional = [s for s in candidates if s.country_code in _COMMONLY_ABBREVIATED_CC]
        if len(conventional) == 1:
            return conventional[0], frozenset()
        pool = conventional if conventional else candidates

    if sibling_texts:
        matched: dict[str, object] = {}
        for sib in sibling_texts:
            for cand in pool:
                if _city_lookup(sib, CC_TO_COUNTRY.get(cand.country_code, "")):
                    matched[cand.country_code] = cand
        if len(matched) == 1:
            return next(iter(matched.values())), frozenset()

    ambiguous_pool = frozenset(CC_TO_COUNTRY.get(s.country_code, "") for s in pool) - {""}
    return None, ambiguous_pool


def _city_lookup(text: str, country_hint: str = "") -> dict[str, Any] | None:
    folded = _normalize_saint(_fold(text))
    candidates = list(CITIES_BY_NAME.get(folded, []))
    alias_target = _CITY_NAME_ALIASES.get(folded, "")
    if alias_target:
        # Merge rather than "or"-shortcircuit: an alias target and a
        # direct match can both be real (e.g. a future alias could point
        # at a name that also has its own small unrelated namesake) -- the
        # country-hint filter below is what actually picks the right one,
        # so both pools should be visible to it, not just whichever was
        # checked first.
        seen_ids = {c["geonameid"] for c in candidates}
        for c in CITIES_BY_NAME.get(alias_target, []):
            if c["geonameid"] not in seen_ids:
                candidates.append(c)
        candidates.sort(key=lambda c: c.get("population", 0), reverse=True)
    if not candidates:
        for suffix in _GENERIC_AREA_SUFFIXES:
            if folded.endswith(" " + suffix):
                return _city_lookup(folded[: -len(suffix) - 1], country_hint)
        for prefix in _DISTRICT_PREFIXES:
            if folded.startswith(prefix) and folded != prefix.strip():
                return _city_lookup(folded[len(prefix) :], country_hint)
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


# `or` must stay case-sensitive even though `;`/`/`/`|` don't need to be: a
# handful of US state postal codes are themselves the English word "or"
# spelled in caps ("Portland, OR"). Matching "OR" here as a separator
# destroys the state before libpostal ever sees it. `(?-i:...)` scopes
# case-sensitivity to just that branch inside an otherwise case-insensitive
# pattern.
#
# `\s-\s` (a hyphen with a space on both sides) is deliberately narrower
# than a bare `-`: real hyphenated place names ("Winston-Salem",
# "Sophia-Antipolis") never have spaces around the hyphen, so they're
# untouched, while a board's own "Country - City" formatting (which
# libpostal tokenizes inconsistently -- confirmed it happens to split
# "France - Paris" into two components but folds "Netherlands - Alkmaar"
# into one unparseable blob) is now split deterministically before
# libpostal ever sees it, rather than relying on tokenizer luck.
_SEPARATOR_RE = re.compile(r";|(?-i:\bor\b)|/|\||\s-\s", re.IGNORECASE)

# `and`/`&` are deliberately NOT in `_SEPARATOR_RE` above: multi-word
# country names legitimately contain "and" ("Trinidad and Tobago", "Bosnia
# and Herzegovina") and splitting on it there would destroy them before
# libpostal ever sees the whole name. Instead this is a narrow fallback
# tried only when a segment's ordinary mining below finds *nothing at
# all* -- real country names always resolve on the first pass and never
# reach this path, so they can't regress. A segment that failed outright
# ("US and Canada", "United States & Canada" -- confirmed via testing that
# libpostal folds the whole phrase into one unparseable "house" blob) gets
# one retry, split on " and "/" & ", mining each half separately.
_AND_SPLIT_RE = re.compile(r"\s+and\s+|\s*&\s*", re.IGNORECASE)

# A real hierarchical address (city, state, country[, zip]) essentially
# never needs more than this many commas. A segment with more is a
# place-list dump run together without recognized separators (observed: a
# real 10-state posting listing every office it has) -- libpostal will
# still tag pieces of it ("state", "country", even "house"/"road" for
# fragments it can't place), but only a couple of them, arbitrarily, so
# trusting those tags would silently pick one state out of ten. Refuse to
# mine a segment shaped like that at all rather than guess.
_MAX_SEGMENT_COMMAS = 4

# A few country acronyms/colloquial names libpostal's own tokenizer fails to
# recognize as a geographic token at all -- confirmed by testing, not
# guessed: "Dubai, UAE" folds into a single unparseable "house"-type blob
# instead of tagging a country component, so no amount of downstream
# alias-matching in COUNTRY_NAMES can help (the token is gone from the
# component list by the time our code sees it). These must be expanded to
# a name libpostal itself tags correctly *before* parsing. Acronyms are
# matched case-sensitively (real postings write them in caps; matching
# lowercase would risk hitting unrelated text) using a curated, closed
# list -- not a general acronym-expansion mechanism.
_PRE_PARSE_EXPANSIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bUAE\b"), "United Arab Emirates"),
    (re.compile(r"\bKSA\b"), "Saudi Arabia"),
    (re.compile(r"\bDRC\b"), "Democratic Republic of the Congo"),
    (re.compile(r"\bBurma\b", re.IGNORECASE), "Myanmar"),
)


def _mine_segment_signals(
    segment: str,
) -> tuple[set[str], set[tuple[str, str]], set[tuple[str, str, str]]]:
    """Collect every (country, state, city) signal libpostal's own
    role-tagging surfaces in one segment. Only whole libpostal-emitted
    components are ever tested against the canonical tables — never raw
    substrings/n-grams of the segment — which is what stops an incidental
    word inside a real place name ("Central" in "Central Jakarta") from
    being tested on its own and colliding with an unrelated small town."""
    if segment.count(",") > _MAX_SEGMENT_COMMAS:
        return set(), set(), set()

    for pattern, full_name in _PRE_PARSE_EXPANSIONS:
        segment = pattern.sub(full_name, segment)

    components = _get_parse_address()(segment)

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
    states: set[tuple[str, str]] = set()  # (code, owning_country)
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
        # context to pick a side. Surface both as candidate countries
        # instead of confidently picking one.
        also_country = _validate_country(text)
        if also_country:
            siblings = tuple(
                t for j, (t, _) in enumerate(components) if j != i and j not in claimed
            )
            code, owner, _pool = _validate_state(
                text,
                countries,
                allow_bare_code=False,
                sibling_texts=siblings,
            )
            if len(components) == 1:
                if owner:
                    countries.add(owner)
                    countries.add(also_country)
                else:
                    countries.add(also_country)
            elif owner and any(_city_lookup(sib, owner) for sib in siblings):
                # A sibling really is a city in the subdivision's own
                # country ("Atlanta" is a real US city) -- libpostal's
                # "state" tag is corroborated, so trust the state reading.
                states.add((code, owner))
                countries.add(owner)
            else:
                # Not corroborated: libpostal's "state" tag on this word is
                # unreliable when the rest of the segment is a foreign city
                # it doesn't recognize ("Batumi, Adjara, Georgia" -- Batumi
                # is a real city in the country Georgia, not the US). Prefer
                # the unambiguous country reading rather than guessing state.
                countries.add(also_country)
            claimed.add(i)
            continue
        siblings = tuple(t for j, (t, _) in enumerate(components) if j != i and j not in claimed)
        # Symmetric to the also_country check above: a word that's both a
        # real subdivision's full name AND a real, prominent city elsewhere
        # ("New York" the state vs. New York City) is the same kind of
        # ambiguity, just city-flavored instead of country-flavored. With
        # no sibling text in this segment to corroborate "this really is
        # the state" (e.g. no comma-attached country/city), a lone place
        # name is almost always meant as the city -- this is exactly the
        # shape of a semicolon-separated city list ("Chicago; Dallas; New
        # York; Seattle"). Leave it unclaimed so the ordinary city lookup
        # below picks it up instead of forcing the state reading. Only
        # applies with no countries established yet either: once a country
        # is already anchored (e.g. "New York, USA"), the state reading is
        # the correct, corroborated one and must not be overridden.
        if not siblings and not countries and _city_lookup(text):
            continue
        code, owner, ambiguous_pool = _validate_state(
            text,
            countries,
            allow_bare_code=len(components) > 1,
            sibling_texts=siblings,
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
            # Sorted so the pick is stable across process runs — plain
            # `set` iteration order for strings is randomized per process
            # in CPython (PYTHONHASHSEED), which would otherwise let the
            # same input resolve to a different country on different runs.
            city = next(
                (hit for cc in sorted(restrict_city_countries) if (hit := _city_lookup(text, cc))),
                None,
            )
        else:
            city = _city_lookup(text)
        if city:
            country = CC_TO_COUNTRY.get(city["countrycode"], "")
            if country:
                cities.add((city["name"], country, city.get("admin1code", "")))

    return countries, states, cities


# Words that say how or where in a building, not where on a map:
# "Tel Aviv (Onsite)", "London Office", "New York, NY (HQ)".
_PAREN_RE = re.compile(r"\([^)]*\)")
_WORKPLACE_WORD_RE = re.compile(
    r"\b(?:office|offices|hq|headquarters|on-?site|hybrid|in-office)\b", re.IGNORECASE
)


_SPACES_RE = re.compile(r"\s+")


def _strip_workplace_words(raw: str) -> str:
    """Brackets become a separator, so "Remote (Canada)" still names Canada;
    then the workplace words go."""
    text = _PAREN_RE.sub(lambda m: f" - {m.group(0)[1:-1]} ", raw)
    text = _WORKPLACE_WORD_RE.sub(" ", text)
    return _SPACES_RE.sub(" ", text).strip(" ,-")


# ---------------------------------------------------------------------
# Country codes. A code closing an address ("Amsterdam, NH, NL") names the
# country, so it is spelled out before mining; otherwise "NH" reads as New
# Hampshire. A code that is also a US state ("IN", "DE", "CA") counts only
# when the address shows it is not one: a non-US region code before it
# ("Pune, MH, IN"), or a city that is in that country and not in the US
# ("Hamburg, DE").
# ---------------------------------------------------------------------

_ALPHA3_TO_COUNTRY: dict[str, str] = {_c.alpha_3: _c.name for _c in pycountry.countries}
_CODE_ALIASES_ALPHA2 = {"UK": "GB"}
US_STATE_CODES = frozenset(
    s.code.split("-", 1)[1]
    for s in pycountry.subdivisions.get(country_code="US")  # type: ignore[no-untyped-call]
)
_POSTAL_PART_RE = re.compile(r"^\d{4,6}$")
_LEADING_ALPHA3_RE = re.compile(r"^([A-Z]{3})\s")
# "TUN - ARIANA", "IND-Pune-Smartworks", "USA-CA San Jose": a leading code.
_LEADING_CODE_RE = re.compile(r"^([A-Z]{2,3})(?:\s+-\s+|-)(\S.*)$")


def _comma_free(name: str) -> str:
    """A spelling of a country COUNTRY_NAMES knows, without a comma
    ("Taiwan" for "Taiwan, Province of China"), so a spelled-out code does
    not split the address it closes."""
    if "," not in name:
        return name
    c = pycountry.countries.get(name=name)
    for alt in (getattr(c, "common_name", ""), getattr(c, "official_name", "")):
        if alt and "," not in alt and COUNTRY_NAMES.get(_fold(alt)) == name:
            return alt
    return c.alpha_3 if c else name


def _code_name(code: str) -> str:
    """The country a 2- or 3-letter ISO code names, any case, or ""."""
    up = code.strip().upper()
    if len(up) == 2 and up.isalpha():
        return CC_TO_COUNTRY.get(_CODE_ALIASES_ALPHA2.get(up, up), "")
    if len(up) == 3 and up.isalpha():
        return _ALPHA3_TO_COUNTRY.get(up, "")
    return ""


def _is_subdivision_of(text: str, country: str) -> bool:
    """ "MH" before "IN": a region code of that country."""
    cc = COUNTRY_TO_CC.get(country, "")
    return any(sub.country_code == cc for sub in SUBDIVISIONS_BY_CODE.get(text.strip().upper(), []))


def _city_country(text: str) -> str:
    hit = _city_lookup(text)
    return CC_TO_COUNTRY.get(hit["countrycode"], "") if hit else ""


def _spell_out_codes(seg: str) -> str:
    """`seg` with a country code that closes it spelled out, or unchanged."""
    parts = [p.strip() for p in seg.split(",") if p.strip()]
    # A trailing postal code hides the code before it ("Ludwigsburg, DE,
    # 71636"); it is dropped only then, since libpostal reads it otherwise.
    trimmed = parts
    while len(trimmed) > 1 and _POSTAL_PART_RE.match(trimmed[-1]):
        trimmed = trimmed[:-1]
    if len(trimmed) == 1 and (m := _LEADING_ALPHA3_RE.match(trimmed[0])):
        # "SGP Work-at-Home", "CHE ZURI 2".
        name = _ALPHA3_TO_COUNTRY.get(m.group(1), "")
        return f"{trimmed[0][m.end() :].strip()}, {_comma_free(name)}" if name else seg
    if len(trimmed) == 1 and len(trimmed[0]) == 3 and trimmed[0].isupper():
        name = _ALPHA3_TO_COUNTRY.get(trimmed[0], "")
        return _comma_free(name) if name else seg
    if len(trimmed) < 2:
        return seg
    head, code = trimmed[:-1], trimmed[-1]
    name = _code_name(code)
    if not name:
        return seg
    in_country = any(_city_lookup(p, name) for p in head)
    if len(code) == 2 and code.upper() in US_STATE_CODES:
        # "Pune, MH, IN" and "Hamburg, DE" are not Indiana or Delaware;
        # "Indianapolis, IN" and "Wilmington, DE" are.
        city_there = in_country and _city_lookup(head[0], "United States") is None
        if not (_is_subdivision_of(head[-1], name) or city_there):
            return seg
    elif not in_country and (
        len(head) == 1 or any((c := _city_country(p)) and c != name for p in head)
    ):
        # "Mumbai, MH" and "Gurugram, HR": a city elsewhere, so the code is
        # a region of that country, not Marshall Islands or Croatia.
        return seg
    return ", ".join([*head, _comma_free(name)])


# A lone name that is a region in one country and a city this big in
# another reads as the city.
_MIN_DOMINANT_CITY_POPULATION = 500_000
_COMPASS_RE = re.compile(r"^(?:north|south|east|west|central|greater)(?:ern)?\s+")


def _whole_subdivision(seg: str) -> tuple[str, str] | None:
    """(code, country) when the segment is exactly one region's name, with
    an optional compass word in front, and names no country."""
    folded = _fold(seg)
    if _validate_country(seg) or not _COMPASS_RE.sub("", folded + " ").strip():
        # "North", "Central": a compass word alone names no place.
        return None
    for name in (folded, _COMPASS_RE.sub("", folded)):
        subs = SUBDIVISIONS_BY_NAME.get(name, [])
        owners = {s.country_code for s in subs}
        if len(owners) == 1:
            owner = CC_TO_COUNTRY.get(subs[0].country_code, "")
            city = _city_lookup(seg)
            if city and (
                CC_TO_COUNTRY.get(city["countrycode"]) == owner
                or city.get("population", 0) >= _MIN_DOMINANT_CITY_POPULATION
            ):
                # "New York", "Iasi": a city of the same country reads as the
                # city, as before; so does a major city ("Santiago").
                return None
            return subs[0].code.split("-", 1)[-1], owner
        if subs:
            return None
    return None


def _leading_code(raw: str) -> tuple[str, str]:
    """(country, rest) for a location led by a country code, or ("", raw)."""
    m = _LEADING_CODE_RE.match(raw)
    if not m:
        return "", raw
    code = m.group(1)
    if len(code) == 2 and code in US_STATE_CODES:
        return "", raw
    return _code_name(code), m.group(2)


@functools.lru_cache(maxsize=50_000)
def parse_location(raw: str) -> LocationParse:
    if not raw:
        return LocationParse("", "", "", False)

    remote = "remote" in raw.lower()
    raw = _strip_workplace_words(raw)

    lead, rest = _leading_code(raw)
    if lead:
        # The leading code is the country unless the rest names another
        # one outright ("PER - Karachi, PK" is Pakistan).
        r = parse_location(rest)
        tail = rest.rpartition(",")[2].strip()
        named = _validate_country(tail) or (len(tail) <= 3 and _code_name(tail))
        if r.country == lead or (r.country and named):
            return replace(r, remote=remote or r.remote)
        return LocationParse(lead, "", "", remote)

    segments = [s.strip(" ,-") for s in _SEPARATOR_RE.split(raw) if s.strip(" ,-")]
    segments = [_spell_out_codes(s) for s in segments]

    found_countries: set[str] = set()
    found_states: set[tuple[str, str]] = set()
    found_cities: set[tuple[str, str, str]] = set()

    for seg in segments:
        region = REGION_TOKEN_COUNTRIES.get(_fold(seg))
        if region:
            # Checked before mining: some of these acronyms are also a real
            # small place name ("Apac", Uganda).
            found_countries |= region
            continue
        sub = _whole_subdivision(seg)
        if sub:
            # "Virginia", "Northern California": a region's own name, which
            # libpostal does not tag, beats a small namesake city abroad.
            found_states.add(sub)
            found_countries.add(sub[1])
            continue
        c, s, ci = _mine_segment_signals(seg)
        whole = _validate_country(seg)
        if whole and not (c or s or ci):
            # A segment that is exactly a country name ("Malta") and nothing
            # else libpostal recognised needs no tagging to be believed.
            c = {whole}
        tail_text = seg.rpartition(",")[2]
        tail = _validate_country(tail_text)
        if "," in seg and tail and c - {tail} and not SUBDIVISIONS_BY_NAME.get(_fold(tail_text)):
            # "New York, United States": the closing country wins over a
            # name libpostal misread inside the address ("York", England).
            c = set()
            s = {x for x in s if x[1] == tail}
            ci = {x for x in ci if x[1] == tail}
        if not c and tail:
            # "Seoul, Korea": the last comma part names the country even when
            # libpostal does not tag it as one.
            c = {tail}
            ci = ci or (
                {(hit["name"], tail, hit.get("admin1code", ""))}
                if (hit := _city_lookup(seg.rpartition(",")[0].strip(), tail))
                else set()
            )
        if not (c or s or ci) and _AND_SPLIT_RE.search(seg):
            # Ordinary mining found nothing at all -- see _AND_SPLIT_RE's
            # comment for why "and"/"&" aren't in the main separator regex.
            for sub_seg in _AND_SPLIT_RE.split(seg):
                sub_seg = sub_seg.strip()
                if not sub_seg:
                    continue
                sc, ss, sci = _mine_segment_signals(sub_seg)
                c |= sc
                s |= ss
                ci |= sci
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
        # Several states of one country: the country is certain.
        return LocationParse(country, "", "", remote)
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
            # State is inferred from a city's admin1 code for the United
            # States only; other countries' admin1 codes are not consistently
            # shaped. City text itself is returned for every country.
            if country == "United States" and admin1:
                state = admin1
        else:
            # Several cities of one country: the country is certain.
            return LocationParse(country, "", "", remote)

    return LocationParse(country, state, city, remote)


def require_libpostal() -> None:
    """Raise the install hint now rather than hours into a run."""
    _get_parse_address()
