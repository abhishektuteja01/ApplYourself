"""`app new` (open a role's folder and snapshot its JD) and `app dupes`.
Part of `uv run app` (src/applyourself/app_cli.py)."""

from __future__ import annotations

import contextlib
import sys
from typing import Any

import pandas as pd
import yaml

from applyourself import resume_model, score_store, track_cli
from applyourself.apply import ledger
from applyourself.discovery.store import load_maybe_duplicates
from applyourself.material.folders import (
    SNAPSHOT,
    AppError,
    _atomic_write,
    _clean_value,
    _identity,
    _slug,
    _state,
    _version,
    role_folder,
    role_info,
)
from applyourself.shared import clock, paths, verticals


def _load_row(job_id: str) -> dict[str, Any]:
    if not paths.CLEAN.exists():
        raise AppError("jobs/clean.parquet missing — run discovery first.")
    if not paths.SCORED.exists():
        raise AppError("jobs/scored.parquet missing — run /score first.")
    clean = pd.read_parquet(paths.CLEAN)
    scored = score_store.view(paths.SCORED)
    c = clean[clean["job_id"] == job_id]
    s = scored[scored["job_id"] == job_id]
    if c.empty:
        raise AppError(f"job_id {job_id} not in clean.parquet")
    if s.empty:
        raise AppError(f"job_id {job_id} not in scored.parquet — run /score first")
    return {
        str(k): _clean_value(v) for k, v in {**c.iloc[0].to_dict(), **s.iloc[0].to_dict()}.items()
    }


def cmd_new(job_id: str, today: str | None = None) -> dict[str, Any]:
    today = today or clock.today().isoformat()
    if not (paths.PROFILE / "resume_template.docx").is_file():
        raise AppError("profile/resume_template.docx missing.")
    resume_model.load_sources()
    row = _load_row(job_id)
    cfg = verticals.get_config()
    lane = row.get("vertical") or ""
    if lane not in cfg.verticals or cfg.verticals[lane].resume is None:
        raise AppError(
            f"{job_id} is scored in lane {lane!r}, which has no resume: block in "
            f"profile/verticals.yaml. Re-score it or add the lane."
        )
    with contextlib.redirect_stdout(sys.stderr):
        registered = track_cli.main(["ensure", job_id])
    if registered != 0:
        raise AppError(f"could not register {job_id} in pipeline/.")

    newest = role_folder(job_id)
    _, slug = _identity()
    recorded = _state(job_id).get("tailored_dirs") or []
    if (
        newest
        and newest.parent.name == lane
        and (
            not (newest / f"{slug}_Resume.docx").exists()
            or str(newest.relative_to(paths.APPLICATIONS)) not in recorded
        )
    ):
        folder = newest  # an unfinished or unrecorded run: resume it
    else:
        base = f"{today}_{_slug(row.get('company', '')) or job_id}_{_slug(row.get('title', ''))}_{job_id}"
        folder = paths.APPLICATIONS / lane / (f"{base}_v{_version(newest) + 1}" if newest else base)
        folder.mkdir(parents=True)

    facts = {
        "job_id": job_id,
        "company": row.get("company", ""),
        "title": row.get("title", ""),
        "url": row.get("url", ""),
        "posted_date": row.get("posted_date", ""),
        "vertical": lane,
        "keywords_to_mirror": [str(k) for k in row.get("keywords_to_mirror") or []],
        "snapshot_at": today,
    }
    _atomic_write(
        folder / SNAPSHOT,
        "---\n"
        + yaml.safe_dump(facts, sort_keys=False, allow_unicode=True)
        + "---\n\n"
        + str(row.get("jd_text") or "")
        + "\n",
    )
    return {
        **role_info(job_id),
        "work_dir": str(folder.relative_to(paths.APPLICATIONS.parent)),
        "company": facts["company"],
        "title": facts["title"],
        "keywords_to_mirror": facts["keywords_to_mirror"],
    }


# A partner in these states (or untracked) is no risk: nothing was sent for it.
_IDLE_STATES = frozenset({"", "saved", "skip"})


def cmd_dupes(job_id: str) -> dict[str, Any]:
    """Other job_ids recorded in maybe_duplicates.jsonl with this one whose
    role is tailored or further, or was submitted. Read-only; a warning for
    the session to show, never a stop."""
    submitted = paths.APPLY_RUNS / ledger.LEDGER_NAME
    sent = ledger.sent_job_ids(submitted) if submitted.exists() else set()
    found: dict[str, dict[str, Any]] = {}
    for rec in load_maybe_duplicates(paths.MAYBE_DUPES):
        ids = str(rec["pair"]).split("|")
        if job_id not in ids or len(ids) != 2:
            continue
        other = ids[1] if ids[0] == job_id else ids[0]
        state = _state(other)
        status = str(state.get("state") or "")
        if status in _IDLE_STATES and other not in sent:
            continue
        mine = rec.get("job_id") == other
        hit = {
            "job_id": other,
            "company": state.get("company") or rec.get("company" if mine else "other_company", ""),
            "title": state.get("title") or rec.get("title", ""),
            "state": status,
            "submitted": other in sent,
            "reason": rec.get("reason", ""),
            "jd_score": rec.get("jd_score"),
        }
        if other not in found or (hit["jd_score"] or 0) > (found[other]["jd_score"] or 0):
            found[other] = hit
    return {"job_id": job_id, "duplicates": sorted(found.values(), key=lambda h: h["job_id"])}
