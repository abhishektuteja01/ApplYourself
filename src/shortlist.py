"""Shortlist computation and rendering: which rows make the cut, and the markdown
a human reads.

Deterministic (R7): a sort, a cap, an exclusion split, and a template. The
invariants are asserted inline rather than trusted, because a silently dropped
row here is a job the user never sees.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from src import verticals
from src.apply.detect import is_auto_submittable
from src.parquet_io import write_parquet
from src.scoring_io import SUBSCORE_AXES, to_jsonable
from src.state_io import load_state_index

# Shortlist caps. compute_shortlist's defaults, the assertion that guards them,
# and the header line a human reads must all be the same two numbers.
SHORTLIST_TOP_N = 25


SHORTLIST_MIN_FIT = 50


# Higher = preferred. ineligible never reaches the main list, but included
# for completeness so the sort key never sees a missing label.
SPONSORSHIP_PREF = {"sponsors": 3, "opt_ok": 2, "unknown": 1, "ineligible": 0}


# Pipeline states at or past the point of application — exclude from the
# shortlist main list because the user has already acted on these roles.
_APPLIED_STATES = frozenset({
    "applied", "recruiter_contact", "screen", "interview",
    "offer", "rejected", "withdrawn", "ghosted",
})


def _count_skips(state_history: Any) -> int:
    if not isinstance(state_history, list):
        return 0
    return sum(
        1 for h in state_history
        if isinstance(h, dict) and h.get("state") == "skip"
    )


def _load_state_metadata(pipeline_dir: Path) -> dict[str, dict]:
    """{job_id: {state, skip_count}} from pipeline/*/state.yaml."""
    return {
        jid: {
            "state": data.get("state", ""),
            "skip_count": _count_skips(data.get("state_history")),
        }
        for jid, data in load_state_index(pipeline_dir).items()
    }


def compute_shortlist(
    scored_path: Path,
    clean_path: Path,
    pipeline_dir: Path,
    top_n: int = SHORTLIST_TOP_N,
    min_fit: int = SHORTLIST_MIN_FIT,
    write_ranks: bool = True,
) -> dict:
    """Deterministic sort + cap + exclusion split.

    Returns:
      {
        "main": {<vertical>: [row, ...] for each configured vertical,
                 in profile/verticals.yaml config order}
            # top_n per vertical, each section sorted/capped independently;
            # excludes ineligible/suppressed. Sections rank independently:
            # one vertical's fit_score is never compared against another's.
        "excluded":   [row, ...]   # sponsorship_label == 'ineligible' (sponsorship is the only exclusion gate)
        "suppressed": [row, ...]   # state_history skip count >= 1 (a skip is not an ineligible)
      }

    Sort key within each vertical's section (locked):
      fit_score DESC,
      sponsorship_pref DESC (sponsors > opt_ok > unknown),
      posted_date DESC.

    Side effect (write_ranks=True): writes shortlist_rank back to scored.parquet
    (1..N within each vertical's section for main rows, NaN otherwise)."""
    cfg = verticals.get_config()
    if not scored_path.exists():
        return {"main": {v: [] for v in cfg.names}, "excluded": [], "suppressed": []}
    scored = pd.read_parquet(scored_path)
    clean = pd.read_parquet(clean_path)
    state_meta = _load_state_metadata(pipeline_dir)

    df = scored.merge(clean, on="job_id", how="left", suffixes=("", "_clean"))
    df["skip_count"] = df["job_id"].map(
        lambda j: state_meta.get(j, {}).get("skip_count", 0)
    )
    df["current_state"] = df["job_id"].map(
        lambda j: state_meta.get(j, {}).get("state", "")
    )
    df["sponsorship_pref"] = (
        df["sponsorship_label"].map(SPONSORSHIP_PREF).fillna(0).astype(int)
    )
    excluded = df[df["sponsorship_label"] == "ineligible"].copy()
    suppressed = df[df["skip_count"] >= 1].copy()

    main_pool = df[
        (df["sponsorship_label"] != "ineligible")
        & (df["skip_count"] < 1)
        & (df["fit_score"] >= min_fit)
        & (~df["current_state"].isin(_APPLIED_STATES))
    ].copy()

    new_ranks: dict[str, int] = {}
    main: dict[str, list[dict]] = {}
    for vertical in cfg.names:
        section = main_pool[main_pool["vertical"] == vertical].sort_values(
            by=["fit_score", "sponsorship_pref", "posted_date"],
            ascending=[False, False, False],
            kind="stable",
        ).head(top_n)
        for rank, jid in enumerate(section["job_id"]):
            new_ranks[jid] = rank + 1
        main[vertical] = [{k: to_jsonable(v) for k, v in r.items()} for r in section.to_dict("records")]

    if write_ranks:
        scored2 = scored.copy()
        scored2["shortlist_rank"] = (
            scored2["job_id"].map(new_ranks).astype("float64")
        )
        write_parquet(scored2, scored_path)

    return {
        "main":       main,
        "excluded":   [{k: to_jsonable(v) for k, v in r.items()} for r in excluded.to_dict("records")],
        "suppressed": [{k: to_jsonable(v) for k, v in r.items()} for r in suppressed.to_dict("records")],
    }


def render_shortlist_markdown(
    shortlist: dict, cfg: "verticals.VerticalsConfig",
    date_str: str, n_scored: int, n_clean: int,
) -> str:
    """Render shortlist/<date>.md from compute_shortlist()'s output.
    Asserts invariants inline (fit>=50, subscore sum, no ineligible/skip
    leakage, no cross-vertical leakage) — raises AssertionError, never
    silently drops a row."""
    def subscores(row):
        s = row["fit_subscores"]
        return json.loads(s) if isinstance(s, str) else s

    sections = []
    total_keepers = 0
    for v in cfg.names:
        rows = shortlist["main"].get(v, [])
        assert len(rows) <= SHORTLIST_TOP_N, (
            f"{v}: {len(rows)} rows exceeds cap {SHORTLIST_TOP_N}")
        lines = [f"## {cfg.verticals[v].display_name} ({len(rows)})", ""]
        if not rows:
            lines.append("No keepers today in this vertical.")
        for i, row in enumerate(rows, 1):
            assert row["vertical"] == v, f"{row['job_id']} leaked into {v} section"
            assert row["fit_score"] >= SHORTLIST_MIN_FIT, \
                f"{row['job_id']}: fit {row['fit_score']} < {SHORTLIST_MIN_FIT}"
            sub = subscores(row)
            assert sum(sub[a] for a in SUBSCORE_AXES) == row["fit_score"], \
                f"{row['job_id']}: subscores {sub} != fit_score {row['fit_score']}"
            assert row["sponsorship_label"] != "ineligible", f"{row['job_id']}: ineligible in main"
            status = row["application_status"] if row.get("already_seen") else "new"
            kws = ", ".join(row.get("keywords_to_mirror", [])[:3])
            # Derived from the URL, not the source name. Keying on
            # `source == "workday"` marked 0 rows (Workday roles arrive
            # labelled "indeed" as often as not) while leaving every LinkedIn,
            # Indeed and Ashby row unmarked — 146 of 286 roles at fit >= 85
            # were silently un-submittable. §12c puts all of them in the same
            # manual-apply category, so the same predicate the queue uses
            # decides it here.
            manual_apply = (
                "" if is_auto_submittable(row.get("url"))
                else " — **manual-apply, not auto-submittable**"
            )
            lines.append(
                f"### {i}. {row['fit_score']} — {row['company']} — {row['title']}\n"
                f"- **job_id:** `{row['job_id']}`\n"
                f"- **location:** {row['location']} · **source:** {row['source']}{manual_apply} "
                f"· **posted:** {row['posted_date']}\n"
                f"- **fit:** {row['fit_score']} (title {sub['title']} / skills {sub['skills']} "
                f"/ seniority {sub['seniority']} / domain {sub['domain']})\n"
                f"- **sponsorship:** {row['sponsorship_label']} — \"{row['sponsorship_evidence']}\"\n"
                f"- **why:** {row['reasoning']}\n"
                f"- **mirror in tailoring:** {kws}\n"
                f"- **status:** {status}\n"
                f"- **suggested:** {row['suggested_action']}\n"
                f"- **verify E-Verify** before submitting (manual step)\n"
                f"- {row['url']}\n"
            )
        sections.append("\n".join(lines))
        total_keepers += len(rows)

    header = (f"# Shortlist — {date_str}\n\n"
              f"({n_scored} of {n_clean} scored, top {SHORTLIST_TOP_N} "
              f"per vertical with fit >= {SHORTLIST_MIN_FIT})\n")
    if total_keepers == 0:
        return header + "\nNo keepers today in this vertical.\n"
    return header + "\n" + "\n".join(sections)
