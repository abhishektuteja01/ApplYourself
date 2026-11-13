"""Which New roles the night's tailor stage takes, in sheet order, and why it
leaves the others: a JD under settings `tailor_min_jd_chars` (`thin`); a
recorded duplicate at `tailor_dupe_min_jd` or more of a role tailored, applied,
submitted or picked earlier tonight (`dupe`, `material.new.partners`); a role
posted more than `tailor_live_check_days` ago whose board says gone (`gone`). A
skipped role stays New; nothing here writes state (R10). No LLM (R7).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from applyourself.apply import ledger
from applyourself.discovery import liveness
from applyourself.discovery.store import load_maybe_duplicates
from applyourself.material.new import partners
from applyourself.shared import clock, http_client, paths, schemas, settings_defaults
from applyourself.shared.parquet_io import read_parquet

THIN, DUPE, GONE = "thin", "dupe", "gone"
LIVE_CHECK_PACING = 2.0  # seconds between two liveness requests


def _clean() -> dict[str, dict[str, Any]]:
    if not paths.CLEAN.exists():
        return {}
    clean = read_parquet(
        paths.CLEAN,
        schemas.CLEAN_SCHEMA_VERSION,
        columns=["job_id", "url", "jd_text", "posted_date"],
    )
    return {str(r["job_id"]): {str(k): v for k, v in r.items()} for r in clean.to_dict("records")}


def _posting_urls(job_ids: set[str]) -> dict[str, list[str]]:
    if not job_ids or not paths.STORE.exists():
        return {}
    store = read_parquet(paths.STORE, schemas.STORE_SCHEMA_VERSION, columns=["job_id", "url"])
    store = store[store["job_id"].isin(job_ids)]
    out: dict[str, list[str]] = {}
    for jid, url in zip(store["job_id"], store["url"], strict=True):
        out.setdefault(str(jid), []).append(str(url or ""))
    return out


def pick(
    job_ids: list[str], limit: int | None = None, check_live: bool = True
) -> tuple[list[str], dict[str, dict[str, str]]]:
    """(the ids to tailor, {skipped id: {"why", "detail"}}). Stops once `limit`
    are picked. `check_live` False asks no board (a dry run)."""
    min_jd = int(settings_defaults.value("tailor_min_jd_chars"))
    live_days = int(settings_defaults.value("tailor_live_check_days"))
    min_dupe = float(settings_defaults.value("tailor_dupe_min_jd"))
    clean = _clean()
    today = pd.Timestamp(clock.today())
    old = set()
    if live_days and check_live:
        for jid in job_ids:
            posted = (clean.get(jid) or {}).get("posted_date")
            when = pd.to_datetime(str(posted), errors="coerce") if posted is not None else pd.NaT
            if pd.notna(when) and (today - when).days > live_days:
                old.add(jid)
    urls = _posting_urls(old)
    records = load_maybe_duplicates(paths.MAYBE_DUPES)
    submitted = paths.APPLY_RUNS / ledger.LEDGER_NAME
    sent = ledger.sent_job_ids(submitted) if submitted.exists() else set()
    pacer = http_client.Pacer(LIVE_CHECK_PACING)
    picked: list[str] = []
    skipped: dict[str, dict[str, str]] = {}
    for jid in job_ids:
        if limit is not None and len(picked) >= limit:
            break
        row = clean.get(jid) or {}
        n = len(str(row.get("jd_text") or "").strip())
        if row and n < min_jd:
            skipped[jid] = {"why": THIN, "detail": f"JD {n} chars"}
            continue
        dupe = next(
            (
                h
                for h in partners(jid, records, sent)
                if (h["reason"] == "same_form" or (h["jd_score"] or 0) >= min_dupe)
                and (not h["idle"] or h["job_id"] in picked)
            ),
            None,
        )
        if dupe is not None:
            st = "picked tonight" if dupe["idle"] else dupe["state"] or "submitted"
            skipped[jid] = {"why": DUPE, "detail": f"duplicate of {dupe['job_id']} ({st})"}
            continue
        if jid in old:
            status, where = liveness.role_status(
                [str(row.get("url") or ""), *urls.get(jid, [])], pacer
            )
            if status == liveness.GONE:
                skipped[jid] = {
                    "why": GONE,
                    "detail": f"posted {str(row.get('posted_date'))[:10]}, gone at {where}",
                }
                continue
        picked.append(jid)
    return picked, skipped
