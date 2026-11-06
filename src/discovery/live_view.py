"""The live view: jobs/clean.parquet, rebuilt from the store every night.

One row per live `job_id`, exactly CLEAN_COLUMNS. Downstream reads only this.

Deterministic and LLM-free (R7). Reads pipeline/*/state.yaml, never writes it
(R10).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pandas as pd

from src.discovery import parse
from src.discovery import store as store_mod
from src.discovery.config import JOBSPY_SOURCES
from src.discovery.store import coerce_store
from src.score_filter import ScoringConfig, drop_reason, load_scoring
from src.shared import paths, title_gate
from src.shared.job_boards import is_applyable
from src.shared.parquet_io import locked, write_parquet
from src.state_io import ACTIVE_STATES

log = logging.getLogger(__name__)

CLEAN = paths.CLEAN

# Closed schema, fixed order: what every downstream reader of clean.parquet
# expects.
CLEAN_COLUMNS: list[str] = [
    "job_id",
    "source",
    "company",
    "company_normalized",
    "title",
    "title_normalized",
    "location",
    "posted_date",
    "url",
    "jd_text",
    "salary_min",
    "salary_max",
    "salary_currency",
    "employment_type",
    "seniority_raw",
    "vertical",
    "application_status",
]


@dataclass
class ViewInputs:
    today: pd.Timestamp
    state_index: dict
    scores: dict[str, float]
    live_days: int
    search_max_age_days: int
    score_threshold: float
    high_days: int
    low_days: int
    # today's hard filter; an unscored row it drops gets low_days. None: no filter.
    scoring: ScoringConfig | None = None


def load_filter() -> ScoringConfig | None:
    import yaml

    from src.apply.answers import AnswersError

    try:
        return load_scoring()
    except (OSError, ValueError, yaml.YAMLError, AnswersError) as e:
        log.warning("retention without the score filter: %s", e)
        return None


def read_scores(scored_path: Path) -> dict[str, float]:
    if not Path(scored_path).exists():
        return {}
    from src.score_store import view

    try:
        scored = view(Path(scored_path))
    except (OSError, ValueError, KeyError) as e:
        log.warning("could not read %s: %s", scored_path, e)
        return {}
    return dict(zip(scored["job_id"], scored["fit_score"]))


def inputs_from(cfg, today, state_index: dict, scored_path: Path) -> ViewInputs:
    scores = read_scores(scored_path)
    return ViewInputs(
        today=pd.Timestamp(today).normalize(),
        state_index=state_index,
        scores=scores,
        live_days=cfg.live_days,
        search_max_age_days=cfg.search_max_age_days,
        score_threshold=cfg.retention_score_threshold,
        high_days=cfg.retention_high_days,
        low_days=cfg.retention_low_days,
        scoring=load_filter(),
    )


def build_view(
    store: pd.DataFrame, allowlist, inp: ViewInputs, vcfg=None
) -> tuple[pd.DataFrame, dict]:
    """One row per job_id downstream should see, and the funnel that got
    there. Every count is a subset of the one before it, plus the active
    tracked roles added back at the end."""
    today = inp.today
    tracked = frozenset(inp.state_index)
    active = frozenset(j for j, d in inp.state_index.items() if d.get("state") in ACTIVE_STATES)
    df = coerce_store(store)
    funnel = {"store_postings": len(df)}

    live = df["last_seen"] >= today - pd.Timedelta(days=inp.live_days)
    df_live = df[live]
    funnel["live"] = len(df_live)

    # Only search rows are cut on age; a board listing is its own liveness
    # signal. A search row with no posted date is kept: retention bounds it.
    posted = df_live["posted_date"]
    fresh = (
        ~df_live["source"].isin(JOBSPY_SOURCES)
        | posted.isna()
        | (posted >= today - pd.Timedelta(days=inp.search_max_age_days))
        | df_live["job_id"].isin(tracked)
    )
    df_live = df_live[fresh]
    funnel["after_search_age"] = len(df_live)

    gate = [
        title_gate.title_passes(t, v, s, vcfg)
        for t, v, s in zip(df_live["title"], df_live["vertical"], df_live["source"])
    ]
    df_live = df_live[pd.Series(gate, index=df_live.index, dtype=bool)]
    funnel["after_title_gate"] = len(df_live)

    checks = [
        allowlist.check(raw, v, t)
        for raw, v, t in zip(df_live["location_raw"], df_live["vertical"], df_live["title"])
    ]
    keep = pd.Series([c.keep for c in checks], index=df_live.index, dtype=bool)
    df_live = df_live.assign(
        location=[c.location for c in checks], _resolved=[c.resolved for c in checks]
    )[keep]
    funnel["after_location"] = len(df_live)

    long_enough = (df_live["jd_text"].str.strip().str.len() >= parse.MIN_JD_CHARS) | df_live[
        "job_id"
    ].isin(tracked)
    df_live = df_live[long_enough]
    funnel["after_short_jd"] = len(df_live)

    view = _one_per_job(df_live)
    funnel["jobs"] = len(view)

    first_seen = df.groupby("job_id")["first_seen"].min()
    remembered = df.groupby("job_id")["last_score"].max()
    lifetimes = []
    for row in cast(list[dict[str, Any]], view.to_dict("records")):
        jid = row["job_id"]
        score = inp.scores.get(jid)
        if score is None or pd.isna(score):
            score = remembered.get(jid)
        if score is None or pd.isna(score):
            high = inp.scoring is None or drop_reason(row, inp.scoring) is None
        else:
            high = score >= inp.score_threshold
        lifetimes.append(inp.high_days if high else inp.low_days)
    expires = view["job_id"].map(first_seen) + pd.to_timedelta(lifetimes, unit="D")
    within = (today <= expires) | view["job_id"].isin(tracked) | (view["source"] == "manual")
    view = view[within.to_numpy()]
    funnel["after_retention"] = len(view)

    missing = sorted(active - set(view["job_id"]))
    if missing:
        rest = df[df["job_id"].isin(missing)].assign(_resolved=False)
        rest = rest.assign(location=rest["location_raw"])
        back = _one_per_job(rest)
        view = pd.concat([view, back], ignore_index=True) if len(back) else view
    funnel["active_added"] = len(view) - funnel["after_retention"]

    state_map = {
        j: d.get("state", "") for j, d in inp.state_index.items() if isinstance(d.get("state"), str)
    }
    view = view.assign(application_status=view["job_id"].map(state_map).fillna(""))
    view = view[CLEAN_COLUMNS].sort_values("job_id", kind="stable").reset_index(drop=True)
    funnel["clean"] = len(view)
    return view, funnel


def _one_per_job(df: pd.DataFrame) -> pd.DataFrame:
    """The representative posting of each job_id: an applyable url, then a
    resolved location, then the newest sighting, then the longest JD."""
    if df.empty:
        return df.drop(columns=["_resolved"], errors="ignore")
    work = df.assign(
        _apply=[not is_applyable(u) for u in df["url"]],
        _unres=~df["_resolved"].astype(bool),
        _jd=df["jd_text"].str.len(),
    ).sort_values(
        ["job_id", "_apply", "_unres", "last_seen", "_jd", "posting_key"],
        ascending=[True, True, True, False, False, True],
        kind="stable",
    )
    out = work.drop_duplicates("job_id", keep="first")
    return out.drop(columns=["_apply", "_unres", "_jd", "_resolved"])


def write_view(view: pd.DataFrame, path: Path | None = None) -> None:
    path = path or CLEAN
    with locked(path):
        write_parquet(view, path)


def rebuild_view(cfg, p, today, state_index: dict) -> tuple[dict, dict]:
    """Refresh remembered scores, forget long-gone postings, then write
    clean.parquet. Returns (store stats, view funnel)."""
    from src.shared.location.allowlist import Allowlist

    tracked = frozenset(state_index)
    scores = read_scores(p.scored)
    with locked(p.store):
        store = store_mod.remember_scores(store_mod.load_store(p.store), scores)
        store, forgotten = store_mod.forget(store, today, cfg.forget_after_days, tracked)
        store_mod.save_store(store, p.store)
    inp = inputs_from(cfg, today, state_index, p.scored)
    view, funnel = build_view(store, Allowlist(cfg), inp)
    write_view(view, p.clean)
    return {
        "postings": len(store),
        "job_ids": int(store["job_id"].nunique()),
        "forgotten": forgotten,
    }, funnel
