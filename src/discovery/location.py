from __future__ import annotations

import gettext
import re
import unicodedata
from dataclasses import dataclass, field

import geonamescache
import pycountry

# libpostal is an optional install (`uv sync --group discovery`) because the
# `postal` binding is sdist-only and compiles against a C library no distro
# packages. Only address parsing needs it, so the import is deferred to the
# one call site: importing this module -- and therefore cleaning, the
# orchestrator and every ATS source -- stays possible without it, and /score
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

_parse_address = None


def _get_parse_address():
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

# Native-language country names ("Deutschland", "Espana", "Brasil") come
# from pycountry's own bundled iso-codes gettext catalogs -- the same
# upstream project pycountry's English names are sourced from -- rather
# than a hand-typed alias per language. Limited to major job-market
# locales (not all ~160 iso-codes ships) to keep import cost bounded;
# missing a rare locale just means that one native name stays unresolved,
# same as any other unmapped input, not a crash.
_MAJOR_LOCALES = (
    "de", "fr", "es", "nl", "it", "pt", "pl", "tr", "ru", "zh", "ja", "ko",
    "ar", "sv", "da", "fi", "cs", "ro", "hu", "el", "he", "th", "vi", "id",
    "uk",
)
for _lang in _MAJOR_LOCALES:
    try:
        _cat = gettext.translation("iso3166-1", pycountry.LOCALES_DIR, languages=[_lang])
    except FileNotFoundError:
        continue
    for _c in pycountry.countries:
        _native = _cat.gettext(_c.name)
        if _native != _c.name:
            COUNTRY_NAMES.setdefault(_fold(_native), _c.name)

# Colloquial aliases pycountry's own name/common_name/official_name strings
# don't cover. A fuzzy-matching fallback (pycountry.countries.search_fuzzy)
# was tried and rejected: it also matches unrelated common words to a
# country by edit-distance coincidence ("North"/"South"/"East"/"West" ->
# Cameroon, "Central" -> Botswana, "Metro" -> France) -- exactly the kind
# of false-positive collision this rewrite replaced the old keyword matcher
# to avoid. Add names here only when confirmed missing from pycountry's own
# fields (see COUNTRY_NAMES construction above).
COUNTRY_NAMES.update({
    "usa": "United States",
    "us": "United States",
    "u.s.": "United States",
    "u.s.a.": "United States",
    "united states of america": "United States",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "uae": "United Arab Emirates",
    "u.a.e.": "United Arab Emirates",
    "ksa": "Saudi Arabia",
    "drc": "Congo, The Democratic Republic of the",
    "dr congo": "Congo, The Democratic Republic of the",
    "democratic republic of congo": "Congo, The Democratic Republic of the",
    "ivory coast": "Côte d'Ivoire",
    "burma": "Myanmar",
    "holland": "Netherlands",
    "russia": "Russian Federation",
    "macedonia": "North Macedonia",
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

SUBDIVISIONS_BY_NAME: dict[str, list] = {}
SUBDIVISIONS_BY_CODE: dict[str, list] = {}
for _s in pycountry.subdivisions:
    SUBDIVISIONS_BY_NAME.setdefault(_fold(_s.name), []).append(_s)
    _bare = _s.code.split("-", 1)[-1].upper()
    SUBDIVISIONS_BY_CODE.setdefault(_bare, []).append(_s)

# pycountry's own subdivision.name is often the local-language ISO name
# ("Noord-Brabant", "Bayern") rather than the English form job postings
# use. Same iso-codes gettext catalog mechanism as COUNTRY_NAMES above,
# translated into English, added as an extra lookup key when it differs.
# Coverage is genuinely partial -- iso-codes' own English catalog doesn't
# translate every subdivision (confirmed: "Bayern" -> "Bavaria" exists,
# "Noord-Brabant" -> "North Brabant" does not) -- this narrows the gap,
# it doesn't close it.
try:
    _en_subdivision_cat = gettext.translation(
        "iso3166-2", pycountry.LOCALES_DIR, languages=["en"]
    )
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


CITIES_BY_NAME: dict[str, list[dict]] = {}
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
# COUNTRY_NAMES/SUBDIVISIONS_BY_NAME locale-translation mechanisms above.
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
            name_candidates, hint_countries, sibling_texts, use_conventional_tiebreak=False,
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
        candidates, hint_countries, sibling_texts, use_conventional_tiebreak=True,
    )
    if sub is not None:
        return bare, CC_TO_COUNTRY.get(sub.country_code, ""), frozenset()
    return "", "", ambiguous_pool


def _disambiguate_subdivisions(
    candidates: list,
    hint_countries: set[str],
    sibling_texts: tuple[str, ...],
    use_conventional_tiebreak: bool,
):
    """Shared narrowing logic for a collision pool of pycountry subdivisions
    that share the same name or the same bare code: known-country hint,
    then (codes only) the "conventionally abbreviated this way" tie-break,
    then sibling-city corroboration, else surface the pool as ambiguous.
    Returns (matched_subdivision_or_None, ambiguous_pool)."""
    if hint_countries:
        narrowed = [
            s for s in candidates
            if CC_TO_COUNTRY.get(s.country_code, "") in hint_countries
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


def _city_lookup(text: str, country_hint: str = "") -> dict | None:
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
_PRE_PARSE_EXPANSIONS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bUAE\b"), "United Arab Emirates"),
    (re.compile(r"\bKSA\b"), "Saudi Arabia"),
    (re.compile(r"\bDRC\b"), "Democratic Republic of the Congo"),
    (re.compile(r"\bBurma\b", re.IGNORECASE), "Myanmar"),
)


def _mine_segment_signals(segment: str):
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
        # context to pick a side. Surface both as candidate countries
        # instead of confidently picking one.
        also_country = _validate_country(text)
        if also_country:
            siblings = tuple(t for j, (t, _) in enumerate(components) if j != i and j not in claimed)
            code, owner, _pool = _validate_state(
                text, countries, allow_bare_code=False, sibling_texts=siblings,
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
            # Sorted so the pick is stable across process runs — plain
            # `set` iteration order for strings is randomized per process
            # in CPython (PYTHONHASHSEED), which would otherwise let the
            # same input resolve to a different country on different runs.
            city = next(
                (c for cc in sorted(restrict_city_countries) if (c := _city_lookup(text, cc))),
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
