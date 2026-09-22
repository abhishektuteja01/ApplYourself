#!/usr/bin/env python
"""Probe the `moved_offsite` destinations on platforms we do not scrape.

The reconcile pass records a company found on a platform with no client in
`src/discovery/sources/ats/`, so it cannot probe the destination and the
migration stays an unverified claim from a bulk dump. Several of those
platforms do publish an unauthenticated board endpoint, so the claim can be
checked after all -- deterministically, with no LLM and no search.

Two things come out of that. The migration becomes evidence rather than an
assertion, which is what a retirement of the old 404 slug rests on. And the
per-platform tally says which platform is worth a real lane: a destination
holding hundreds of live postings is a scraper worth writing, one holding none
is not.

Report only. Writes its CSV and nothing else.

    uv run python scripts/offsite_probe.py --proposal jobs/slug_reconcile_2026-09-22.csv
    uv run python scripts/offsite_probe.py --proposal jobs/... --platform bamboohr
"""
from __future__ import annotations

import argparse
import csv
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import requests

from src import paths

OUT_DIR = paths.REPO_ROOT / "jobs"
COLUMNS = ["ats", "slug", "name", "platform", "dest_slug", "status", "postings", "note"]
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"}
TIMEOUT = 10

LIVE, EMPTY, DEAD, ERROR, UNSUPPORTED = "live", "empty", "dead", "error", "unsupported"


# --- one endpoint and one count per platform --------------------------------
# Each reader takes the parsed body and returns the posting count. A body of
# the wrong shape raises, and the caller reports `error` rather than counting it
# as an empty board -- the same rule the audit uses.

def _len_of(payload, key):
    items = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise TypeError(f"expected a list under {key!r}")
    return len(items)


ENDPOINTS = {
    "workable": ("https://apply.workable.com/api/v1/widget/accounts/{s}?details=true",
                 lambda p: _len_of(p, "jobs")),
    "smartrecruiters": ("https://api.smartrecruiters.com/v1/companies/{s}/postings",
                        lambda p: _len_of(p, "content")),
    "breezy": ("https://{s}.breezy.hr/json",
               lambda p: len(p) if isinstance(p, list) else _raise()),
    "recruitee": ("https://{s}.recruitee.com/api/offers/",
                  lambda p: _len_of(p, "offers")),
    "bamboohr": ("https://{s}.bamboohr.com/careers/list",
                 lambda p: _len_of(p, "result")),
    "teamtailor": ("https://{s}.teamtailor.com/jobs.json",
                   lambda p: _len_of(p, "items")),
    "personio": ("https://{s}.jobs.personio.de/search.json",
                 lambda p: len(p) if isinstance(p, list) else _raise()),
}


def _raise():
    raise TypeError("expected a JSON array")


def probe(platform: str, slug: str) -> tuple[str, int, str]:
    """(status, postings, note) for one destination. Never raises."""
    if platform not in ENDPOINTS:
        return UNSUPPORTED, 0, "no public endpoint known"
    url, count_of = ENDPOINTS[platform]
    try:
        resp = requests.get(url.format(s=slug), timeout=TIMEOUT, headers=HEADERS)
    except Exception as e:  # noqa: BLE001 -- a bad row must not end the pass
        return ERROR, 0, f"{type(e).__name__}"
    if resp.status_code in (404, 410):
        return DEAD, 0, f"HTTP {resp.status_code}"
    if resp.status_code >= 400:
        # A 403 is their firewall and a 429 is our own pacing. Neither says
        # anything about the board, so neither may read as absence.
        return ERROR, 0, f"HTTP {resp.status_code}"
    try:
        count = count_of(resp.json())
    except Exception as e:  # noqa: BLE001 -- a 200 of the wrong shape
        return ERROR, 0, f"unparseable: {type(e).__name__}"
    return (LIVE if count > 0 else EMPTY), count, ""


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--proposal", required=True)
    p.add_argument("--platform", action="append", help="repeatable; default all")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--pacing", type=float, default=0.3, help="seconds between calls")
    p.add_argument("--out", help="default jobs/offsite_probe_<date>.csv")
    args = p.parse_args(argv or sys.argv[1:])

    with open(paths.REPO_ROOT / args.proposal, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f)
                if (r.get("verdict") or "").strip() == "moved_offsite"
                and (r.get("new_slug") or "").strip()]
    if args.platform:
        want = {x.lower() for x in args.platform}
        rows = [r for r in rows if r["new_ats"].strip().lower() in want]

    out = paths.REPO_ROOT / args.out if args.out else OUT_DIR / f"offsite_probe_{date.today()}.csv"
    print(f"{len(rows)} offsite destination(s) -> {out}\n")

    lock = threading.Lock()
    results: list[list] = []
    counts: Counter = Counter()
    postings: Counter = Counter()

    def work(row):
        platform = row["new_ats"].strip().lower()
        dest = row["new_slug"].strip()
        time.sleep(args.pacing)
        status, n, note = probe(platform, dest)
        with lock:
            counts[(platform, status)] += 1
            postings[platform] += n
            results.append([row["ats"], row["slug"], row["name"], platform, dest,
                            status, n, note])

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(work, rows))

    results.sort(key=lambda r: (r[3], r[4]))
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        w.writerows(results)

    platforms = sorted({p for p, _ in counts})
    print(f"{'platform':18}{'live':>7}{'empty':>7}{'dead':>7}{'error':>7}"
          f"{'unsupp':>8}{'postings':>10}")
    for plat in platforms:
        print(f"{plat:18}" + "".join(f"{counts[(plat, s)]:>7}"
                                     for s in (LIVE, EMPTY, DEAD, ERROR))
              + f"{counts[(plat, UNSUPPORTED)]:>8}{postings[plat]:>10}")
    live = sum(v for (_, s), v in counts.items() if s == LIVE)
    print(f"\n{live} of {len(rows)} destinations confirmed live, "
          f"{sum(postings.values())} postings behind them.")
    print(f"Wrote {out}. Nothing else was modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
