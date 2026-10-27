"""`discover-dupes`: the store's existing job_ids that look like one role.

A night only matches a new posting against the store; ids already given are
never revisited. This runs the same matcher over every pair of stored ids and
records each likely pair in jobs/maybe_duplicates.jsonl, for review. It never
changes an id: retire a copy with `/track <id> skip`.

Deterministic and LLM-free (R7). Reads pipeline/*/state.yaml, never writes it
(R10).
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import pandas as pd

from src import paths
from src.discovery import store as S
from src.score_filter import load_trim
from src.state_io import load_state_index


def _rank(r: dict) -> tuple[bool, float]:
    """A merge verdict outranks any maybe; then the higher JD score."""
    return r["reason"] == "backfill_merge", r["jd_score"]


def find_pairs(store: pd.DataFrame, trim: S.Trim = S.NO_TRIM,
               date: str = "") -> list[dict]:
    """One record per pair of stored job_ids the matcher calls the same role
    ("backfill_merge") or a maybe ("backfill_" + a MAYBE_REASONS entry), best
    JD score kept."""
    index = S._Index(store, frozenset(), trim)
    best: dict[str, dict] = {}
    for row in store.to_dict("records"):
        done: set[str] = set()
        for c in index.candidates(row):
            other = c["job_id"]
            if other == row["job_id"] or other in done:
                continue
            verdict, score = S.same_job_elsewhere(row, c, trim)
            if not verdict:
                continue
            if verdict == "merge":
                done.add(other)
            reason = f"backfill_{verdict}"
            key = "|".join(sorted((row["job_id"], other)))
            if key in best and _rank(best[key]) >= (verdict == "merge", round(score, 1)):
                continue
            best[key] = {
                "pair": key, "job_id": row["job_id"], "other": other,
                "company": row["company_normalized"], "other_company": c["company_normalized"],
                "title": row["title_normalized"], "jd_score": round(score, 1),
                "reason": reason, "date": date}
    return sorted(best.values(), key=lambda r: (r["reason"], -r["jd_score"], r["pair"]))


def _line(r: dict, states: dict) -> str:
    def side(jid, company):
        st = states.get(jid, {}).get("state")
        return f"{jid} {company}" + (f" [{st}]" if st else "")
    return (f"{r['reason']:<20} {r['jd_score']:>5g}  {side(r['job_id'], r['company'])}"
            f"  /  {side(r['other'], r['other_company'])}  — {r['title']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="discover-dupes",
        description="List stored job_ids that look like one role and record them in "
                    "jobs/maybe_duplicates.jsonl. Never changes an id.")
    parser.add_argument("--dry-run", action="store_true", help="print only; write nothing")
    parser.add_argument("--jobs-dir", type=Path, default=paths.JOBS)
    args = parser.parse_args(argv)

    store = S.load_store(args.jobs_dir / paths.STORE.name)
    states = load_state_index(paths.PIPELINE)
    rows = find_pairs(store, load_trim(), str(pd.Timestamp.today().date()))
    print("pairs by reason:", dict(Counter(r["reason"] for r in rows)))
    both = [r for r in rows if r["job_id"] in states and r["other"] in states]
    one = [r for r in rows if (r["job_id"] in states) != (r["other"] in states)]
    for title, group in (("both tracked", both), ("one tracked", one)):
        print(f"\n{title}: {len(group)}")
        for r in group:
            print(" ", _line(r, states))
    if not args.dry_run:
        new = S.append_maybe_duplicates(rows, args.jobs_dir / paths.MAYBE_DUPES.name)
        print(f"\nrecorded {len(new)} new pairs in {args.jobs_dir / paths.MAYBE_DUPES.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
