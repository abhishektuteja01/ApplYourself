"""The daily shortlist: which scored roles to look at, and the markdown a human
reads. A pure read of scored.parquet, clean.parquet and pipeline/*/state.yaml
(R7, R10): it writes only shortlist/<date>.md.

Two parts:
  In pipeline   roles whose state is in shortlist.pipeline_states: the count,
                and the shortlist.pipeline_show highest-fit of them
  One per lane  the top shortlist.top_n untracked roles at fit >= cutoffs.review,
                excluding ineligible and suggested skip
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

from applyourself import contacts
from applyourself.apply.detect import is_auto_submittable
from applyourself.score_filter import ScoringConfig
from applyourself.score_store import view
from applyourself.shared import schemas, verticals
from applyourself.shared.parquet_io import read_parquet
from applyourself.state_io import load_state_index

_JOB_ID_RE = re.compile(r"`([0-9a-f]{8})`")


def compute(
    scored_path: Path,
    clean_path: Path,
    pipeline_dir: Path,
    cfg: ScoringConfig,
    scored: pd.DataFrame | None = None,
) -> dict:
    """{"pipeline": [row...], "lanes": {lane: [row...]}} in display order.
    `scored` is `view(scored_path, cfg)` when the caller already has it."""
    names = verticals.get_config().names
    if scored is None:
        scored = view(scored_path, cfg)
    if scored.empty:
        return {"pipeline": [], "lanes": {v: [] for v in names}}
    clean = read_parquet(clean_path, schemas.CLEAN_SCHEMA_VERSION).drop(
        columns=[c for c in scored.columns if c != "job_id"], errors="ignore"
    )
    df = scored.merge(clean, on="job_id", how="inner")
    states = {j: d.get("state", "") for j, d in load_state_index(pipeline_dir).items()}
    df["state"] = df["job_id"].map(states).fillna("")
    order = {label: i for i, label in enumerate(cfg.sponsorship_order)}
    df["_sponsor"] = df["sponsorship_label"].map(order).fillna(len(order))
    df["_posted"] = pd.to_datetime(df["posted_date"], errors="coerce")

    def ranked(frame: pd.DataFrame) -> pd.DataFrame:
        return frame.sort_values(
            ["fit_score", "_sponsor", "_posted", "job_id"],
            ascending=[False, True, False, True],
            na_position="last",
            kind="stable",
        )

    pool = df[
        (df["state"] == "")
        & (df["sponsorship_label"] != "ineligible")
        & (df["suggested_action"] != "skip")
        & (df["fit_score"] >= cfg.review_min)
    ]
    return {
        "pipeline": ranked(df[df["state"].isin(cfg.pipeline_states)]).to_dict("records"),
        "lanes": {
            v: ranked(pool[pool["vertical"] == v]).head(cfg.top_n).to_dict("records") for v in names
        },
    }


def previous_ids(shortlist_dir: Path, date_str: str) -> set[str]:
    """job_ids in the newest shortlist file dated before date_str."""
    older = sorted(p for p in shortlist_dir.glob("*.md") if p.stem < date_str)
    return set(_JOB_ID_RE.findall(older[-1].read_text(encoding="utf-8"))) if older else set()


def _text(value) -> str:
    return "unknown" if value is None or pd.isna(value) or str(value) == "" else str(value)


def _date(value) -> str:
    ts = pd.to_datetime(value, errors="coerce")
    return "unknown" if pd.isna(ts) else ts.date().isoformat()


def render(
    result: dict,
    cfg: ScoringConfig,
    date_str: str,
    n_scored: int,
    n_clean: int,
    seen: set[str],
    referrals: dict[str, list[str]] | None = None,
) -> str:
    vcfg = verticals.get_config()
    referrals = referrals or {}

    def ref(company: str, sep: str) -> str:
        note = contacts.referral_note(company, referrals)
        return f"{sep}{note}" if note else ""

    out = [
        f"# Shortlist — {date_str}",
        "",
        f"{n_scored} of {n_clean} scored. Top {cfg.top_n} per lane with fit >= "
        f"{cfg.review_min}. ★ = new since the last shortlist.",
    ]
    if cfg.note:
        out.append(cfg.note)
    pipe = result["pipeline"]
    out += ["", f"## In pipeline ({len(pipe)})", ""]
    out += [
        f"- `{r['job_id']}` {r['fit_score']:.0f} — {r['company']} — {r['title']} ({r['state']})"
        f"{ref(r['company'], ' · ')}"
        for r in pipe[: cfg.pipeline_show]
    ] or ["Nothing saved or tailored."]
    if len(pipe) > cfg.pipeline_show:
        out.append(f"- …and {len(pipe) - cfg.pipeline_show} more (`/standup` lists them all)")
    for v in vcfg.names:
        rows = result["lanes"][v]
        out += ["", f"## {vcfg.verticals[v].display_name} ({len(rows)})", ""]
        if not rows:
            out.append("No keepers today.")
        for i, r in enumerate(rows, 1):
            subs = json.loads(r["fit_subscores"])
            parts = (
                f"fit {subs['fit']} less {subs['penalty']} ({subs['level']}, {subs['years']} yrs)"
                if "fit" in subs
                else f"title {subs['title']} / skills {subs['skills']} / seniority "
                f"{subs['seniority']} / domain {subs['domain']}"
            )
            star = "" if r["job_id"] in seen else " ★"
            extras = [n for n in [contacts.referral_note(r["company"], referrals)] if n]
            if r["sponsorship_label"] != "unknown":
                extras.append(
                    f'sponsorship {r["sponsorship_label"]}: "{r["sponsorship_evidence"]}"'
                )
            if r["suggested_action"] != "tailor":
                extras.append(f"suggested {r['suggested_action']}")
            if not is_auto_submittable(r.get("url")):
                extras.append("manual-apply")
            out += [
                f"### {i}. {r['fit_score']:.0f} — {r['company']} — {r['title']}{star}",
                f"- `{r['job_id']}` · {_text(r.get('location'))} · posted {_date(r.get('posted_date'))}"
                f" · {r['source']}",
                f"- {parts} — {r['reasoning']}",
                f"- mirror: {', '.join(list(r['keywords_to_mirror'])[:3])}",
            ]
            if extras:
                out.append("- " + " · ".join(extras))
            out += [f"- {r['url']}", ""]
    return "\n".join(out).rstrip() + "\n"


def write(
    scored_path: Path,
    clean_path: Path,
    pipeline_dir: Path,
    shortlist_dir: Path,
    cfg: ScoringConfig,
    date_str: str,
    contacts_path: Path | None = None,
) -> tuple[Path, int]:
    """Render and write shortlist/<date_str>.md. Returns (path, lane rows)."""
    scored = view(scored_path, cfg)
    result = compute(scored_path, clean_path, pipeline_dir, cfg, scored)
    n_scored = len(scored)
    n_clean = len(read_parquet(clean_path, schemas.CLEAN_SCHEMA_VERSION, columns=["job_id"]))
    md = render(
        result,
        cfg,
        date_str,
        n_scored,
        n_clean,
        previous_ids(shortlist_dir, date_str),
        contacts.referrals_by_company(contacts_path),
    )
    shortlist_dir.mkdir(parents=True, exist_ok=True)
    path = shortlist_dir / f"{date_str}.md"
    path.write_text(md, encoding="utf-8")
    return path, sum(len(rows) for rows in result["lanes"].values())
