"""Row factories built from the columns src/ really writes, so a renamed or
dropped column fails every test that builds rows here. Unknown keys raise."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

from src import score_store, state_io
from src.discovery.live_view import CLEAN_COLUMNS
from src.score_store import SCORED_COLUMNS
from src.shared import identity

_NOW = datetime(2026, 9, 27, 12, 0)
_NUMERIC = {"salary_min", "salary_max"}


def _check(row: dict, allowed, what: str) -> None:
    unknown = sorted(set(row) - set(allowed))
    if unknown:
        raise KeyError(f"unknown {what} key: {', '.join(unknown)}")


def clean_row(**over) -> dict:
    """One clean.parquet row. company/title feed the normalized names and the
    job_id unless those are given."""
    _check(over, CLEAN_COLUMNS, "clean.parquet")
    row = {c: float("nan") if c in _NUMERIC else "" for c in CLEAN_COLUMNS}
    row.update(posted_date=pd.NaT, company="Acme", title="Widget Analyst")
    row.update(over)
    row["company_normalized"] = row["company_normalized"] or identity.normalize_company(row["company"])
    row["title_normalized"] = row["title_normalized"] or identity.normalize_title(row["title"])
    row["job_id"] = row["job_id"] or identity.compute_job_id(
        row["company_normalized"], row["title_normalized"])
    return row


def clean_rows(*rows: dict) -> pd.DataFrame:
    """A clean.parquet frame, exactly CLEAN_COLUMNS, one row per dict."""
    return pd.DataFrame([clean_row(**r) for r in rows], columns=CLEAN_COLUMNS)


def write_clean(path: Path, *rows: dict) -> pd.DataFrame:
    df = clean_rows(*rows)
    df.to_parquet(path, index=False)
    return df


def scored_row(**over) -> dict:
    """One scored.parquet row, defaults from score_store's own row builder."""
    _check(over, SCORED_COLUMNS, "scored.parquet")
    base = score_store._row(over.get("job_id", ""), "", score_store._zero(), "unknown", "", "",
                            [], "skip", "", _NOW)
    return {**base, **over}


def scored_rows(*rows: dict) -> pd.DataFrame:
    return pd.DataFrame([scored_row(**r) for r in rows], columns=SCORED_COLUMNS)


def state_record(state: str = "saved", **over) -> dict:
    """A state.yaml record shaped by state_io's own creation path."""
    base = state_io._transitioned(Path("state.yaml"), None, state, None,
                                  _NOW.isoformat(), {"job_id": "aaaaaaaa", "company": "Acme",
                                                     "title": "Widget Analyst"})
    _check(over, base, "state.yaml")
    return {**base, **over}


def write_state(pipeline: Path, job_id: str, state: str = "saved", **over) -> Path:
    path = state_io.state_path_for(pipeline, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(state_record(state, job_id=job_id, **over), sort_keys=False),
                    encoding="utf-8")
    return path
