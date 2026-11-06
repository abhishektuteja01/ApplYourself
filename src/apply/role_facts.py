"""What /apply reads about one role besides its form: the job's country
(discovery's location, state.yaml, the title, then a JD phrase) and the role's
company_answers.yaml."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from src.apply.answers import (
    UNKNOWN_COUNTRY,
    Answers,
)
from src.shared import paths, schemas
from src.shared.parquet_io import read_parquet


def job_country(state: dict | None, answers: Answers) -> str:
    """The job's country: from discovery's location for it (`clean.parquet`,
    already cleaned), else `state.yaml`'s copy, else its title's residency
    qualifier ("(Germany)"), else the one country its JD names. A location that
    reads as no country is `UNKNOWN_COUNTRY`; no location at all, or "Remote",
    is "" (the candidate's own country applies)."""
    state = state or {}
    job_id = str(state.get("job_id") or "")
    try:
        from src.shared.location.allowlist import residency_countries
        from src.shared.location.parser import parse_location
    except Exception:  # noqa: BLE001 - the parser is optional plumbing
        return ""
    for raw in (_clean_value(job_id, "location"), str(state.get("location") or "")):
        try:
            parsed = parse_location(raw)
        except Exception:  # noqa: BLE001 - libpostal missing: no country, not a crash
            continue
        if parsed.country:
            return parsed.country
        if answers.home_country in parsed.candidate_countries:
            return answers.home_country
    raw = _clean_value(job_id, "location") or str(state.get("location") or "")
    found = _pieces_country(raw, parse_location)
    if found:
        return found
    named = residency_countries(str(state.get("title") or ""))
    if len(named) == 1:
        return next(iter(named))
    found = _jd_country(job_id)
    if found:
        return found
    if _US_STATE_START.match(raw.strip()):
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


def _pieces_country(raw: str, parse_location) -> str:
    """The one country the pieces of a location name, when the whole string
    parses as none ("IN Pune", "USA-CA San Jose", "Wrocław, DO, PL")."""
    from src.apply.answers import places_named

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
            try:
                country = parse_location(piece).country
            except Exception:  # noqa: BLE001 - libpostal missing
                return ""
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
    from src.apply.answers import places_named

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
