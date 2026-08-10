"""Harvest the application forms of already-scored roles and census their questions.

    uv run python scripts/harvest_forms.py [--min-score 75] [--limit N]
                                            [--ats greenhouse,lever,ashby]
                                            [--pacing 2.0] [--dry-run]

Read-only. GETs each posting's public application form — the same request
`uv run apply plan` already makes for a single role — and writes
`jobs/harvest/harvest_{raw,census}.json`. Nothing is filled, nothing is
submitted, no browser opens.

Output feeds the Tier B rule question: which unanswered questions recur often
enough to be worth a rule, versus which are one-offs. The grouping judgment
stays with a human/command session; this only counts (R7).

Paced by default, because a few hundred requests to a few hundred employers
back to back from one IP is what gets an IP flagged.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

from src import paths
from src.apply.detect import detect_ats
from src.apply.harvest import DEFAULT_PACING_SECONDS, harvest, write_census

OUT_DIR = paths.JOBS / "harvest"


def corpus(min_score: float, keep_ats: set[str]) -> list[tuple[str, str, str, str]]:
    """`(job_id, url, ats, company)` for every scored role on a readable board.

    Deduped by job_id, ordered by score so a `--limit` run harvests the roles
    that matter most first.
    """
    scored = pd.read_parquet(paths.SCORED, columns=["job_id", "fit_score"])
    clean = pd.read_parquet(paths.CLEAN, columns=["job_id", "url", "company", "title"])
    df = scored.merge(clean, on="job_id", how="left")
    df = df[df["fit_score"] >= min_score]
    df = df.sort_values("fit_score", ascending=False).drop_duplicates("job_id")

    out = []
    for row in df.itertuples():
        url = str(row.url or "")
        ats = detect_ats(url)
        if ats in keep_ats:
            out.append((row.job_id, url, ats, str(row.company or "")))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--min-score", type=float, default=75.0)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--ats", default="greenhouse,lever,ashby")
    p.add_argument("--pacing", type=float, default=DEFAULT_PACING_SECONDS)
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be fetched and exit")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)

    keep = {a.strip() for a in args.ats.split(",") if a.strip()}
    rows = corpus(args.min_score, keep)
    if args.limit:
        rows = rows[:args.limit]

    by_ats: dict[str, int] = {}
    for _, _, ats, _ in rows:
        by_ats[ats] = by_ats.get(ats, 0) + 1
    est = len(rows) * args.pacing / 60
    print(f"{len(rows)} postings at fit >= {args.min_score}: {by_ats}")
    print(f"pacing {args.pacing}s -> ~{est:.0f} min of sleeping, plus request time")

    if args.dry_run:
        for job_id, url, ats, company in rows[:20]:
            print(f"  {job_id}  {ats:11} {company[:28]:30} {url[:70]}")
        if len(rows) > 20:
            print(f"  ... and {len(rows) - 20} more")
        return 0

    started = time.time()

    def progress(done, total, result):
        if done % 10 == 0 or done == total:
            rate = done / max(time.time() - started, 1e-9)
            print(f"  {done}/{total}  ok={len(result.ok)} "
                  f"expired={len(result.expired)} failed={len(result.failed)} "
                  f"({rate * 60:.1f}/min)", flush=True)

    result = harvest([(j, u) for j, u, _, _ in rows],
                      pacing=args.pacing, progress=progress)

    raw, agg = write_census(result, OUT_DIR)
    print(f"\nboards ok={len(result.ok)} expired={len(result.expired)} "
          f"failed={len(result.failed)}; {len(result.questions)} fields")
    print(f"wrote {raw}\nwrote {agg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
