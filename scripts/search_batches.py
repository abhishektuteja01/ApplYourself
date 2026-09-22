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
import time
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
# Results are moved out of the batch dir once merged, so a re-split can
# renumber batches without an agent overwriting a finished result file.
RESULT_DIR = OUT_DIR / "search_results"
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


def already_searched() -> set[tuple[str, str]]:
    """Every slug any completed batch already reported on."""
    done: set[tuple[str, str]] = set()
    for d in (BATCH_DIR, RESULT_DIR):
        for path in sorted(d.glob("*.result.csv")) if d.exists() else []:
            with path.open(encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    ats = (row.get("ats") or "").strip().lower()
                    slug = (row.get("slug") or "").strip()
                    if ats and slug:
                        done.add((ats, slug))
    return done


def split(args) -> int:
    rows = load_proposal(paths.REPO_ROOT / args.proposal)
    done = already_searched()
    if done:
        before = len(rows)
        rows = [r for r in rows if (r["ats"], r["slug"]) not in done]
        print(f"skipping {before - len(rows)} slug(s) already searched")
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


def verify_found(rows, pacing: float = 0.5) -> tuple[int, int]:
    """Probe every `found` claim and demote the ones that do not exist.

    Measured on the first two batches: 36 of 38 `found` claims named a board
    that returns 404. A searching agent reads a cached aggregator listing or a
    careers page and concludes the board is live, and it states that with
    complete confidence. So a claim is worth exactly what a probe says it is.

    A claim that fails becomes `searched_nothing_found`, keeping the original
    in `evidence`. That is the accurate reading rather than a convenience: the
    agent did search, and what it came back with does not exist, so the search
    produced nothing verifiable. The demotion is recorded so a human can see
    which retirements rest on a disproved claim.
    """
    import sys as _sys
    _sys.path.insert(0, str(paths.REPO_ROOT))
    from src.ats_http import fetch_json
    from src.discovery.sources.ats import workday as wd

    kept = demoted = 0
    for row in rows:
        if row["outcome"] != SEARCH_FOUND:
            continue
        ats, slug = row["new_ats"].strip().lower(), row["new_slug"].strip()
        if ats not in R.SUPPORTED_ATS or not slug:
            # No client for this platform, so the claim cannot be checked.
            # Left as `found`, which authorizes nothing either way.
            continue
        time.sleep(pacing)
        count = None
        try:
            if ats == "workday":
                company, pod, site = wd.parse_slug(slug)
                payload = wd.list_page(company, pod, site, offset=0)
                count = payload.get("total")
            else:
                payload = fetch_json(R.board_url(ats, slug))
                count = R.posting_count(ats, payload)
        except Exception:  # noqa: BLE001 -- a failed probe is a failed claim
            count = None
        if isinstance(count, int) and count > 0:
            kept += 1
            row["evidence"] = f"verified {count} postings; {row['evidence']}"
        else:
            demoted += 1
            row["outcome"] = SEARCH_EXHAUSTED
            row["evidence"] = (f"unverified_claim:{ats}/{slug} does not exist; "
                               f"{row['evidence']}")
            row["new_ats"] = row["new_slug"] = ""
    return kept, demoted


def merge(args) -> int:
    """One log from every `batch_*.result.csv` an agent wrote.

    A slug reported twice keeps its first `found`: a placement beats an
    exhausted search, so two agents disagreeing cannot downgrade a recovery
    into a retirement.
    """
    seen: dict[tuple[str, str], dict] = {}
    files = sorted(BATCH_DIR.glob("*.result.csv")) + \
            (sorted(RESULT_DIR.glob("*.result.csv")) if RESULT_DIR.exists() else [])
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

    merged = [seen[k] for k in sorted(seen)]
    if not args.no_verify:
        kept, demoted = verify_found(merged)
        print(f"verification: {kept} claim(s) confirmed live, "
              f"{demoted} disproved and demoted to {SEARCH_EXHAUSTED}")

    out = paths.REPO_ROOT / args.out if args.out else OUT_DIR / f"slug_search_{date.today()}.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLUMNS)
        w.writeheader()
        w.writerows(merged)

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
    m.add_argument("--no-verify", action="store_true",
                   help="skip probing the `found` claims. Only for inspecting raw "
                        "agent output -- an unprobed claim must never reach apply")
    m.set_defaults(fn=merge)

    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv or sys.argv[1:])
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
