"""What /apply reads about one role besides its form: the job's country
(discovery's location, state.yaml, the title, then a JD phrase) and the role's
company_answers.yaml."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from applyourself.apply.answers import (
    UNKNOWN_COUNTRY,
    Answers,
)
from applyourself.shared import paths, schemas
from applyourself.shared.countries import CC_TO_COUNTRY, COUNTRY_TO_CC, canonical_country
from applyourself.shared.parquet_io import read_parquet


def job_country(state: dict | None, answers: Answers) -> str:
    """The job's country: from discovery's location for it (`clean.parquet`,
    already cleaned), else `state.yaml`'s copy, else its title's residency
    qualifier ("(Germany)"), else the one country its JD names. A location that
    reads as no country is `UNKNOWN_COUNTRY`; no location at all, or "Remote",
    is "" (the candidate's own country applies)."""
    from applyourself.shared.location.parser import allowed_codes, read_location

    state = state or {}
    job_id = str(state.get("job_id") or "")
    home = COUNTRY_TO_CC.get(answers.home_country, "")
    allowed = allowed_codes([*answers.countries, answers.home_country])
    several = False
    for raw in (_clean_value(job_id, "location"), str(state.get("location") or "")):
        found = _location_country(raw, home, allowed)
        if found == _SEVERAL:
            several = True
        elif found:
            return CC_TO_COUNTRY.get(found, "")
    raw = _clean_value(job_id, "location") or str(state.get("location") or "")
    # A region word ("South America") names no one country: its pieces are not read.
    several = several or read_location(raw, allowed).doubt
    found = "" if several else _pieces_country(raw, lambda p: _piece_name(p, allowed))
    if found:
        return found
    named = residency_countries(str(state.get("title") or ""))
    if len(named) == 1:
        return next(iter(named))
    found = _jd_country(job_id)
    if found:
        return found
    if _US_STATE_START.match(raw.strip()) and not several:
        return "United States"
    # A place was given and nothing could read it: guessing home would answer
    # a Polish job's work-authorization questions as a US one.
    if raw.strip() and not _NO_PLACE.search(raw):
        return UNKNOWN_COUNTRY
    return ""


def _us_state_start() -> re.Pattern:
    import pycountry

    codes = "|".join(
        sub.code.split("-")[1] for sub in pycountry.subdivisions.get(country_code="US")
    )
    return re.compile(r"^(?:USA?-)?(?:" + codes + r")(?:\b|$)")


_US_STATE_START = _us_state_start()
# A location that says how, not where: the candidate's own country applies.
_NO_PLACE = re.compile(
    r"\b(?:remote|remoto|remota|hybrid|anywhere|work from home|wfh)\b", re.IGNORECASE
)


_SEVERAL = "several"


def _location_country(raw: str, home: str, allowed: frozenset[str]) -> str:
    """The ISO code a location names, "" for none, or `_SEVERAL`. A multi-site
    list that holds home is home ("Boston, MA; Vancouver, Canada")."""
    from applyourself.shared.location.parser import LIST_SEPARATOR_RE, read_location

    if not re.search(r"[;/|]|\bor\b", raw) and (lead := _leading_code(raw, allowed)):
        return lead  # "CAN - Ontario - Toronto": one place, not a list
    segments = [s for s in LIST_SEPARATOR_RE.split(raw) if s.strip()]
    if len(segments) > 1:
        each = {c for s in segments if (c := _segment_country(s, home, allowed))}
        if home in each or len(each) > 1:
            return home if home in each else _SEVERAL
        if each:
            return next(iter(each))
    found = _one_country(raw, allowed)
    return found or (home if home in read_location(raw, allowed).alone else "")


def _segment_country(segment: str, home: str, allowed: frozenset[str]) -> str:
    """One segment of a multi-site list: its country, home when a part can only
    be home ("New York, San Francisco, Munich"), else its biggest city's."""
    from applyourself.shared.location.parser import read_location

    if found := _one_country(segment, allowed):
        return found
    return (
        home if home in read_location(segment, allowed).alone else _piece_country(segment, allowed)
    )


def _one_country(raw: str, allowed: frozenset[str]) -> str:
    """A closing code over its own region ("MH, IN"), then a US state closing
    the address ("Delhi, NY"), then one place for sure, then one country named
    in full ("Rotterdam, South Holland, Netherlands")."""
    from applyourself.shared.location.parser import read_location

    if found := _closing_code(raw) or _us_state_tail(raw) or _country_tail(raw):
        return found
    r = read_location(raw, allowed)
    named = r.spelled & r.countries
    if r.sure or len(named) == 1:
        return r.sure or next(iter(named))
    return _SEVERAL if r.name_or_town else ""  # "Lebanon": the country or a US town


def _piece_country(piece: str, allowed: frozenset[str]) -> str:
    """One place for sure (no small town alone), else an allowed country's own
    region name ("Virginia"), else the biggest city of that name ("Melbourne")."""
    from applyourself.shared.location.parser import read_location
    from applyourself.shared.location.places import CITY, MIN_FOREIGN_CITY, SUB_NAME, fold, saint

    name = " ".join(re.sub(r"\d+", " ", piece).split())  # "2 Holt" is Holt
    regions = set(SUB_NAME.get(fold(name), {}))
    cities = CITY.get(saint(fold(name)), [])
    big = cities[0].cc if cities and cities[0].population >= MIN_FOREIGN_CITY else ""
    small = bool(cities) and cities[0].population < MIN_FOREIGN_CITY
    if (found := read_location(piece, allowed).sure) and not small:
        return found  # a small town alone ("Holt", "Garden City") names no country
    if regions:
        # A foreign region's name ("Ontario") is no city's: unknown, so it parks.
        return next(iter(regions)) if len(regions) == 1 and regions <= allowed else ""
    return big


_CLOSING_CODE = re.compile(r"(?:^|,)\s*([A-Z]{2})\s*,\s*([A-Za-z]{2})\s*$")


def _closing_code(raw: str) -> str:
    """The country a closing ISO code names when the part before it is one of
    its own region codes ("Pune, MH, IN", "TN, IN"), or ""."""
    from applyourself.shared.location.places import CODE2, SUB_CODE

    m = _CLOSING_CODE.search(raw)
    if not m:
        return ""
    cc = CODE2.get(m.group(2).upper(), "")
    return cc if cc and cc in SUB_CODE.get(m.group(1), set()) else ""


def _piece_name(piece: str, allowed: frozenset[str]) -> str:
    return CC_TO_COUNTRY.get(_piece_country(piece, allowed), "")


def _country_tail(raw: str) -> str:
    """The country of an address closing with a 2-letter code that is no US
    state: the city's own country when the code is one of its regions
    ("Mumbai, MH", "Berlin, BE"), else the code's country ("Shaw Centre, sg")."""
    from applyourself.shared.location.places import CODE2, SUB_CODE, city_countries

    head, _, tail = _ADDRESS_TAIL.sub("", raw.strip()).rpartition(",")
    code = tail.strip().upper()
    if not head.strip() or len(code) != 2 or "US" in SUB_CODE.get(code, set()):
        return ""
    known = city_countries(head.rpartition(",")[2].strip())
    regions = known & SUB_CODE.get(code, set())
    if regions:
        return next(iter(regions)) if len(regions) == 1 else ""
    return CODE2.get(code, "") if not known or CODE2.get(code) in known else ""


_LEADING_CODE = re.compile(r"^([A-Z]{2,3})(?:-|\s+-\s+|:\s*)(\S.*)$")


def _leading_code(raw: str, allowed: frozenset[str]) -> str:
    """The country a leading ISO code names ("CA-Ontario-Toronto", "CAN - Ontario
    - Toronto") when a place after it is there."""
    import pycountry

    from applyourself.shared.location.places import CODE2

    m = _LEADING_CODE.match(raw.strip())
    if not m:
        return ""
    code = m.group(1)
    cc = (
        CODE2.get(code, "")
        if len(code) == 2
        else getattr(pycountry.countries.get(alpha_3=code), "alpha_2", "")
    )
    words = re.split(r"[-\s,()]+", m.group(2))
    return cc if cc and any(_piece_country(w, allowed) == cc for w in words if len(w) > 3) else ""


_ADDRESS_TAIL = re.compile(
    r"(?:,\s*(?:USA?|U\.S\.A?\.?|United States(?: of America)?|[\d\s-]+))+\s*$", re.IGNORECASE
)
# Countries whose states an address writes as a bare code ("Chennai, TN").
_ABBREVIATED_CC = frozenset({"AU", "BR", "CA", "IN", "MX"})


def _us_state_tail(raw: str) -> str:
    """The country of an address closing with a US state ("Delhi, NY", "Warsaw,
    IN"): the US, unless the state's letters or name are another country's and
    the city is there ("Hamburg, DE", "Chennai, TN"); `_SEVERAL` when the city
    is in both ("Windsor, CA") or a longer address the tables lack ("El Agba, El
    Hrairia, TN")."""
    from applyourself.shared.location.places import (
        CITY,
        CODE2,
        COUNTRY,
        SUB_CODE,
        SUB_NAME,
        US_LIKE,
        city_countries,
        fold,
        saint,
    )

    parts = [p.strip() for p in re.split(r",|\s-\s", _ADDRESS_TAIL.sub("", raw.strip()))]
    if len(parts) < 2 or not parts[-2]:
        return ""
    head, tail = parts[-2], parts[-1]
    code = tail if len(tail) == 2 and tail.isupper() else SUB_NAME.get(fold(tail), {}).get("US", "")
    if "US" not in SUB_CODE.get(code, set()):
        return ""
    clash = {CODE2.get(tail) if len(tail) == 2 else COUNTRY.get(fold(tail))} - {None, "US"}
    clash |= SUB_CODE.get(code, set()) & _ABBREVIATED_CC
    cities = CITY.get(saint(fold(head)), [])
    in_state = any(c.cc == "US" and c.admin1 == code for c in cities)
    known = {c.cc for c in cities}
    for part in parts[-2::-1]:
        if known:
            break
        # "München, DE", "Jakarta Selatan, ID", "Gurgaon, UNAVAILABLE, IN": another
        # spelling, a city inside the part, or an earlier part.
        words = [w for w in re.split(r"[\s.\-/]+", part) if len(w) > 3]
        known = set(city_countries(part)).union(*(city_countries(w) - {"US"} for w in words))
    if known and "US" not in known:
        clash |= SUB_CODE.get(code, set())  # "Amersfoort, UT": no US city of that name
    clash -= US_LIKE
    abroad = known & clash
    if in_state and abroad:
        return _SEVERAL
    if abroad:
        return next(iter(abroad)) if len(abroad) == 1 else _SEVERAL
    if in_state or known or not clash:
        return "US"  # a US place, a city no other reading fits, or letters only the US uses
    # A town the tables lack, with letters another country shares: a plain "Town, ST"
    # is the US; a longer address or a postcode parks ("El Agba, El Hrairia, TN").
    return "US" if len(parts) == 2 and not re.search(r"\d", raw) else _SEVERAL


def _pieces_country(raw: str, country_of) -> str:
    """The one country the pieces of a location name, when the whole string
    parses as none ("IN Pune", "USA-CA San Jose", "Wrocław, DO, PL")."""
    from applyourself.apply.answers import places_named

    if re.match(r"^\s*(?:US|USA|U\.S\.A?\.?|United States)\b", raw):
        return "United States"
    named = {
        p for p in places_named(raw) if p not in ("European Union", "Latin America", "Asia Pacific")
    }
    if len(named) == 1:
        # A country the location names outright beats a city that could be
        # in two ("Santa Clara" is in California and in Cuba).
        return named.pop()
    found = set(named)
    words = [w for w in re.split(r"[\s,;/|()-]+", raw) if w]
    for n in (3, 2, 1):
        for i in range(len(words) - n + 1):
            piece = " ".join(words[i : i + n])
            if len(piece) < 4:
                continue
            country = country_of(piece)
            if country:
                found.add(country)
    return found.pop() if len(found) == 1 else ""


def _clean_value(job_id: str, column: str) -> str:
    if not job_id or not paths.CLEAN.exists():
        return ""
    import pyarrow.parquet as pq

    if column not in pq.read_schema(paths.CLEAN).names:
        return ""
    clean = read_parquet(paths.CLEAN, schemas.CLEAN_SCHEMA_VERSION, columns=["job_id", column])
    row = clean.loc[clean["job_id"] == job_id]
    return "" if row.empty else str(row.iloc[0][column] or "")


# "must be based in Germany", "open to candidates located in India": the JD
# saying where the hire works, not every country it happens to mention.
_RESIDENCY_PHRASE = re.compile(
    r"(?:based|located|residing|reside|live|living|work(?:ing)?\s+from|remote|"
    r"authori[sz]ed\s+to\s+work|eligible\s+to\s+work|hiring)\s+(?:only\s+)?"
    r"(?:in|within|from|across)\s+(?:the\s+)?([^.;\n]{2,40})",
    re.IGNORECASE,
)


def _jd_country(job_id: str) -> str:
    """The one country the JD says the hire works in, for a posting whose
    location says only "Remote". Two countries, or none, is no answer."""
    from applyourself.apply.answers import places_named

    countries = set()
    for m in _RESIDENCY_PHRASE.finditer(_clean_value(job_id, "jd_text")):
        countries |= {
            p
            for p in places_named(m.group(1))
            if p not in ("European Union", "Latin America", "Asia Pacific")
        }
    return countries.pop() if len(countries) == 1 else ""


def company_answers_for(out_dir: Path) -> dict[str, str]:
    path = out_dir / "company_answers.yaml"
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        k: str(v).strip()
        for k, v in data.items()
        if isinstance(k, str) and isinstance(v, str) and v.strip()
    }


# A residency qualifier in a title: a country list right before "only" or
# "based" ("(Remote - ES/UK only)", "(UK Based)"). Nothing looser: a bare
# country word in a title is too often something else ("CAD", "Columbia, MD").
_RESIDENCY_RE = re.compile(r"\b(?:only|based)\b", re.IGNORECASE)
_RESIDENCY_CUT_RE = re.compile(r"[(\[|:–—]|\s-\s")
_RESIDENCY_SPLIT_RE = re.compile(r"\s*(?:/|,|&|\bor\b|\band\b)\s*", re.IGNORECASE)
_TITLE_NOISE_RE = re.compile(r"\b(?:remote|hybrid|on-?site|fully|100%)\b", re.IGNORECASE)
_CODE_ALIASES = {"UK": "GB", "EU": ""}


def _residency_token(token: str) -> set[str]:
    from applyourself.shared.location.places import REGION_TOKEN_COUNTRIES, fold

    token = token.strip(" .-")
    if not token:
        return set()
    if len(token) == 2 and token.isupper():
        if token == "EU":
            return set(REGION_TOKEN_COUNTRIES["europe"])
        name = CC_TO_COUNTRY.get(_CODE_ALIASES.get(token, token))
        return {name} if name else set()
    region = REGION_TOKEN_COUNTRIES.get(fold(token))
    if region:
        return set(region)
    name = canonical_country(token)
    return {name} if name else set()


def residency_countries(title: str) -> set[str]:
    """Countries a title restricts the hire to, or an empty set. Every token
    in the qualifier must name a country or region, or the title says
    something else ("US Citizens only", "W2 only") and is left alone."""
    out: set[str] = set()
    for m in _RESIDENCY_RE.finditer(title or ""):
        head = title[: m.start()]
        cut = list(_RESIDENCY_CUT_RE.finditer(head))
        chunk = _TITLE_NOISE_RE.sub(" ", head[cut[-1].end() :] if cut else head)
        tokens = [t for t in _RESIDENCY_SPLIT_RE.split(chunk) if t.strip(" .-")]
        found = [_residency_token(t) for t in tokens]
        if tokens and all(found):
            for f in found:
                out |= f
    return out
