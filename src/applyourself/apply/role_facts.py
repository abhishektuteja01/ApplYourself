"""What /apply reads about one role besides its form: the job's country and
the role's company_answers.yaml."""

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


def job_country(state: dict | None, answers: Answers, session: str = "") -> str:
    """The job's country, never read from place names here: the /apply
    session's `country` in the answers file, else the score judge's one
    country for the role, else the one country code its postings' source
    gave (the jobs store's `country`), else `UNKNOWN_COUNTRY`, so every answer
    that depends on it parks."""
    if session:
        return session
    from applyourself.shared.location.allowlist import structured_codes
    from applyourself.shared.location.places import US_LIKE

    job_id = str((state or {}).get("job_id") or "")
    judged = _judge_countries(job_id)
    if len(judged) == 1:
        return canonical_place(judged[0]) or UNKNOWN_COUNTRY
    codes = {
        "US" if c in US_LIKE else c for f in _store_countries(job_id) for c in structured_codes(f)
    }
    if len(codes) == 1:
        return CC_TO_COUNTRY.get(next(iter(codes)), UNKNOWN_COUNTRY)
    return UNKNOWN_COUNTRY


def session_country(answers_path: Path | None) -> str:
    """The country the /apply session wrote into the answers file (`"country"`):
    its canonical name, `UNKNOWN_COUNTRY` for "unknown" or a name nothing
    reads, or "" when the file sets none."""
    if answers_path is None or not Path(answers_path).is_file():
        return ""
    try:
        raw = json.loads(Path(answers_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    value = str(raw.get("country") or "").strip() if isinstance(raw, dict) else ""
    if not value:
        return ""
    return canonical_place(value) or UNKNOWN_COUNTRY


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
    from applyourself.score_store import is_v2, read_scored

    out: dict[str, str] = {}
    scored = read_scored(path)
    for jid, raw in zip(scored["job_id"], scored["judge_answer"], strict=True):
        answer = json.loads(raw) if isinstance(raw, str) and raw else None
        if isinstance(answer, dict) and is_v2(answer) and isinstance(answer.get("country"), str):
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
