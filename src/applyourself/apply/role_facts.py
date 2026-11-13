"""What /apply reads about one role besides its form: the job's country, the
role's company_answers.yaml and its cover letter's text."""

from __future__ import annotations

import functools
import json
from pathlib import Path

import yaml

from applyourself.apply.answers import (
    UNKNOWN_COUNTRY,
    Answers,
    canonical_place,
)
from applyourself.shared import paths
from applyourself.shared.countries import CC_TO_COUNTRY


def job_country(state: dict | None, answers: Answers | None = None, session: str = "") -> str:
    """The job's country, never read from place names here (see `country_and_source`)."""
    state = state or {}
    return country_and_source(
        str(state.get("job_id") or ""), session, answers, str(state.get("location") or "")
    )[0]


def country_and_source(
    job_id: str, session: str = "", answers: Answers | None = None, location: str = ""
) -> tuple[str, str]:
    """(country, where it came from): the /apply session's `country` in the
    answers file, else the score judge's one country for the role (or your own
    country when the judge lists several and yours among them), else the one
    code its postings' source gave (the jobs store's `country`), else your own
    country when the judge named none and the job's location names a region
    holding it, else `UNKNOWN_COUNTRY`, so every answer that depends on it parks."""
    from applyourself.apply.places import REGION_COUNTRIES
    from applyourself.shared.location.allowlist import structured_codes
    from applyourself.shared.location.places import US_LIKE

    if session:
        return session, "session"
    home = answers.home_country if answers is not None else ""
    judged = _judge_countries(job_id)
    if len(judged) == 1:
        return country_name(judged[0]), "judge"
    if home and home in {country_name(c) for c in judged}:
        return home, "judge (home listed)"
    codes = {
        "US" if c in US_LIKE else c for f in _store_countries(job_id) for c in structured_codes(f)
    }
    if len(codes) == 1:
        return CC_TO_COUNTRY.get(next(iter(codes)), UNKNOWN_COUNTRY), "site field"
    if home and not judged:
        for pattern, members in REGION_COUNTRIES.values():
            if home in members and pattern.search(location):
                return home, "location region (home listed)"
    return UNKNOWN_COUNTRY, "unknown"


def header_location(job_id: str) -> tuple[str, str, str]:
    """(location, country, source) for the role's resume and letter header: the
    bank's `resume_location` on the row of the role's country (`country_and_source`),
    else "" so resume.yaml's line stands. No bank file is no override; a bank
    that fails to load raises `AnswersError`."""
    from applyourself.apply.bank_schema import load_answers

    country, source = country_and_source(job_id)
    if not paths.ANSWER_BANK.is_file():
        return "", country, source
    row = load_answers().countries.get(country)
    return (row.resume_location if row else ""), country, source


def country_name(value: str) -> str:
    """The bank's key for a country the judge or the session named; Puerto Rico,
    Guam, the US Virgin Islands and the Northern Marianas are the US; a region
    ("Europe"), "default" or a name nothing reads is `UNKNOWN_COUNTRY`."""
    from applyourself.apply.places import REGIONS
    from applyourself.shared.location.places import US_LIKE

    name = canonical_place(value)
    if name in {CC_TO_COUNTRY[c] for c in US_LIKE}:
        return "United States"
    if not name or name == "default" or name in REGIONS:
        return UNKNOWN_COUNTRY
    return name


def session_country(answers_path: Path | None) -> str:
    """The country the /apply session wrote into the answers file (`"country"`),
    or "" when the file sets none."""
    if answers_path is None or not Path(answers_path).is_file():
        return ""
    try:
        raw = json.loads(Path(answers_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return session_country_of(raw)


def session_country_of(data: object) -> str:
    """`session_country` for a loaded answers file: its canonical country,
    `UNKNOWN_COUNTRY` for "unknown" or a region, "" when unset."""
    value = str(data.get("country") or "").strip() if isinstance(data, dict) else ""
    return country_name(value) if value else ""


@functools.cache
def _store_table(path: Path, mtime: float) -> dict[str, list[str]]:
    import pyarrow.parquet as pq

    if "country" not in pq.read_schema(path).names:
        return {}
    table = pq.read_table(path, columns=["job_id", "country"]).to_pydict()
    out: dict[str, list[str]] = {}
    for jid, field in zip(table["job_id"], table["country"], strict=True):
        if field:
            out.setdefault(str(jid), []).append(str(field))
    return out


@functools.cache
def _judged_table(path: Path, mtime: float) -> dict[str, str]:
    from applyourself.score_store import read_scored

    out: dict[str, str] = {}
    scored = read_scored(path)
    for jid, raw in zip(scored["job_id"], scored["judge_answer"], strict=True):
        answer = json.loads(raw) if isinstance(raw, str) and raw else None
        # Today's answers and the old judge's alike state the role's country.
        if isinstance(answer, dict) and isinstance(answer.get("country"), str):
            out[str(jid)] = answer["country"]
    return out


def _judge_countries(job_id: str) -> list[str]:
    """The countries the score judge named for the role (";"-separated)."""
    if not job_id or not paths.SCORED.exists():
        return []
    value = _judged_table(paths.SCORED, paths.SCORED.stat().st_mtime).get(job_id, "")
    return [c.strip() for c in value.split(";") if c.strip() and c.strip().lower() != "remote"]


def _store_countries(job_id: str) -> list[str]:
    """The source country fields ("US", "IE,US") of the job's postings."""
    if not job_id or not paths.STORE.exists():
        return []
    return _store_table(paths.STORE, paths.STORE.stat().st_mtime).get(job_id, [])


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


def cover_letter_for(out_dir: Path) -> str:
    """The role's cover_letter.yaml as the text a cover-letter box takes: the
    salutation, each body paragraph, then the template's own sign-off (its
    text after `{{BODY}}`), blank lines between. "" with no letter."""
    path = out_dir / "cover_letter.yaml"
    if not path.is_file():
        return ""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return ""
    body = [str(p).strip() for p in data.get("body") or [] if str(p).strip()]
    if not body:
        return ""
    salutation = str(data.get("salutation") or "").strip()
    closing = _template_closing(paths.PROFILE / "cover_letter_template.docx")
    return "\n\n".join(part for part in (salutation, *body, closing) if part)


def _template_closing(template: Path) -> str:
    """The template's text after its `{{BODY}}` paragraph, one line per
    paragraph, leaving out placeholders; "" with no template."""
    if not template.is_file():
        return ""
    from docx import Document

    lines: list[str] = []
    seen_body = False
    for paragraph in Document(str(template)).paragraphs:
        text = paragraph.text.strip()
        if text == "{{BODY}}":
            seen_body = True
        elif seen_body and text and not text.startswith("{{"):
            lines.append(text)
    return "\n".join(lines)
