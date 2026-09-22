#!/usr/bin/env python
"""Split a reconcile proposal into batches for the web-search pass, and merge
the results back into one search log.

The deterministic tiers in `reconcile_slugs.py` place a board by name match,
DNS and a careers-page scrape. What is left is the judgment half: a company
whose site is up with no board link on it, or an ambiguous name match that
needs a human-shaped decision. That half is worked by agent sessions with
WebSearch/WebFetch, one batch each, and their verdicts land in a search log.

The log is what authorizes a `dead_end` retirement in `apply_reconcile.py`, so
a slug absent from it is simply not retired. Coverage is therefore allowed to
be partial: an unsearched slug stays in the universe, which is the safe default.

    uv run python scripts/search_batches.py split --proposal jobs/slug_reconcile_2026-09-22.csv
    uv run python scripts/search_batches.py merge --out jobs/slug_search_2026-09-22.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date

from src import paths

import importlib.util
_spec = importlib.util.spec_from_file_location(
    "slug_resolvers", paths.REPO_ROOT / "scripts" / "slug_resolvers.py")
R = importlib.util.module_from_spec(_spec)
sys.modules["slug_resolvers"] = R
_spec.loader.exec_module(R)

OUT_DIR = paths.REPO_ROOT / "jobs"
BATCH_DIR = OUT_DIR / "search_batches"
BATCH_COLUMNS = ["ats", "slug", "name", "old_status", "verdict", "domain", "evidence"]
LOG_COLUMNS = ["ats", "slug", "name", "outcome", "new_ats", "new_slug", "evidence"]

SEARCH_EXHAUSTED = "searched_nothing_found"
SEARCH_FOUND = "found"

# Most likely to yield a board first. A company whose website answered is one
# whose board we merely failed to find; a row where nothing resolved anywhere
# is probably genuinely gone and is the least rewarding place to spend a
# search. `review` sits between: a candidate exists and the question is whether
# it is the same company, which is exactly the judgment a search can settle.
PRIORITY = {R.ALIVE_NO_BOARD: 0, R.REVIEW: 1, R.GONE: 2}


def needs_search(row: dict) -> bool:
    """Only dead boards, and only ones no tier could place.

    `error` never qualifies: a 403 is a firewall and a 429 is our own
    throttling, so neither says anything about the board, and a row that
    reached the search log could then be retired on no evidence at all.
    """
    if (row.get("old_status") or "").strip() not in R.NOT_FOUND_STATUSES:
        return False
    return (row.get("verdict") or "").strip() in PRIORITY


def load_proposal(path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if needs_search(r)]
    rows.sort(key=lambda r: (PRIORITY[r["verdict"].strip()], r["ats"], r["slug"]))
    return rows


def split(args) -> int:
    rows = load_proposal(paths.REPO_ROOT / args.proposal)
    if args.limit:
        rows = rows[:args.limit]
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    for stale in BATCH_DIR.glob("batch_*.csv"):
        stale.unlink()

    size = args.size
    batches = [rows[i:i + size] for i in range(0, len(rows), size)]
    for n, batch in enumerate(batches, 1):
        path = BATCH_DIR / f"batch_{n:03d}.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=BATCH_COLUMNS, extrasaction="ignore")
            w.writeheader()
            w.writerows(batch)
    print(f"{len(rows)} rows needing a search -> {len(batches)} batches of {size}")
    print(f"  by verdict: ", end="")
    for verdict in sorted(PRIORITY, key=PRIORITY.get):
        print(f"{verdict}={sum(1 for r in rows if r['verdict'].strip() == verdict)} ",
              end="")
    print(f"\n  -> {BATCH_DIR}")
    return 0


def merge(args) -> int:
    """One log from every `batch_*.result.csv` an agent wrote.

    A slug reported twice keeps its first `found`: a placement beats an
    exhausted search, so two agents disagreeing cannot downgrade a recovery
    into a retirement.
    """
    seen: dict[tuple[str, str], dict] = {}
    files = sorted(BATCH_DIR.glob("batch_*.result.csv"))
    for path in files:
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ats = (row.get("ats") or "").strip().lower()
                slug = (row.get("slug") or "").strip()
                outcome = (row.get("outcome") or "").strip()
                if not ats or not slug or outcome not in (SEARCH_FOUND, SEARCH_EXHAUSTED):
                    continue
                key = (ats, slug)
                if key in seen and seen[key]["outcome"] == SEARCH_FOUND:
                    continue
                seen[key] = {c: (row.get(c) or "").strip() for c in LOG_COLUMNS}
                seen[key]["ats"], seen[key]["slug"] = ats, slug

    out = paths.REPO_ROOT / args.out if args.out else OUT_DIR / f"slug_search_{date.today()}.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLUMNS)
        w.writeheader()
        w.writerows(seen[k] for k in sorted(seen))

    found = sum(1 for v in seen.values() if v["outcome"] == SEARCH_FOUND)
    print(f"{len(files)} result file(s) -> {len(seen)} slugs: "
          f"{found} found, {len(seen) - found} exhausted")
    print(f"-> {out}")
    return 0


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("split", help="write per-agent batch CSVs")
    s.add_argument("--proposal", required=True)
    s.add_argument("--size", type=int, default=40, help="rows per batch (default 40)")
    s.add_argument("--limit", type=int, help="only the first N prioritized rows")
    s.set_defaults(fn=split)

    m = sub.add_parser("merge", help="merge batch results into one search log")
    m.add_argument("--out", help="default jobs/slug_search_<date>.csv")
    m.set_defaults(fn=merge)

    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv or sys.argv[1:])
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
