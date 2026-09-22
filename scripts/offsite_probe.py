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

import importlib.util
_spec = importlib.util.spec_from_file_location(
    "slug_resolvers", paths.REPO_ROOT / "scripts" / "slug_resolvers.py")
R = importlib.util.module_from_spec(_spec)
sys.modules["slug_resolvers"] = R
_spec.loader.exec_module(R)

OUT_DIR = paths.REPO_ROOT / "jobs"
COLUMNS = ["ats", "slug", "name", "platform", "dest_slug", "status", "postings", "note"]
SWEEP_COLUMNS = ["ats", "slug", "name", "old_verdict", "platform", "dest_slug",
                 "postings", "confidence"]
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


def sweep_stems(name: str, slug: str) -> list[str]:
    """Slugs worth trying on another platform for this company.

    A tenant usually reuses the same handle across vendors, so the old slug and
    the normalized name place most of the ones that can be placed at all.
    """
    import re
    normalized = R.normalize_name(name)
    out = []
    for candidate in (slug.lower(), R.name_stem(name),
                      re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")):
        if candidate and len(candidate) > 2 and candidate not in out:
            out.append(candidate)
    return out


def sweep(args) -> int:
    """Guess each unplaced company's slug on every probeable platform.

    Measured at ~6% on a 150-row sample: small, but deterministic and far
    cheaper than a search. A hit is proof the company is alive and hiring,
    which is what stops its 404 slug being retired as gone.

    A short or generic stem matching a board elsewhere is weak evidence -- a
    three-letter handle collides across unrelated companies -- so those are
    reported `low` and are for a human to confirm, exactly as in the name-match
    tier.
    """
    with open(paths.REPO_ROOT / args.proposal, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f)
                if (r.get("verdict") or "").strip() in ("alive_no_board", "review", "gone")]
    if args.limit:
        rows = rows[:args.limit]
    platforms = [p for p in ENDPOINTS
                 if not args.platform or p in {x.lower() for x in args.platform}]

    out = (paths.REPO_ROOT / args.out if args.out
           else OUT_DIR / f"offsite_sweep_{date.today()}.csv")
    print(f"{len(rows)} unplaced row(s) x {len(platforms)} platform(s) -> {out}\n")

    lock = threading.Lock()
    hits: list[list] = []
    done = [0]

    def work(row):
        for platform in platforms:
            for stem in sweep_stems(row["name"], row["slug"]):
                time.sleep(args.pacing)
                status, n, _ = probe(platform, stem)
                if status == LIVE:
                    confidence = ("low" if R.is_ambiguous_name(R.normalize_name(row["name"]))
                                  else "high")
                    with lock:
                        hits.append([row["ats"], row["slug"], row["name"],
                                     row["verdict"], platform, stem, n, confidence])
                    return
        with lock:
            done[0] += 1

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(work, rows))

    hits.sort(key=lambda r: (r[4], r[1]))
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(SWEEP_COLUMNS)
        w.writerows(hits)

    by_platform = Counter(h[4] for h in hits)
    high = sum(1 for h in hits if h[7] == "high")
    print(f"{'platform':18}{'hits':>6}{'postings':>10}")
    for plat in sorted(by_platform, key=lambda p: -by_platform[p]):
        print(f"{plat:18}{by_platform[plat]:>6}"
              f"{sum(h[6] for h in hits if h[4] == plat):>10}")
    print(f"\n{len(hits)} of {len(rows)} placed ({high} high confidence, "
          f"{len(hits) - high} need a human).")
    print(f"Wrote {out}. Nothing else was modified.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--proposal", required=True)
    p.add_argument("--platform", action="append", help="repeatable; default all")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--pacing", type=float, default=0.3, help="seconds between calls")
    p.add_argument("--out", help="default jobs/offsite_probe_<date>.csv")
    p.add_argument("--sweep", action="store_true",
                   help="instead of checking recorded destinations, guess each "
                        "unplaced company's slug on every probeable platform")
    p.add_argument("--limit", type=int)
    args = p.parse_args(argv or sys.argv[1:])
    if args.sweep:
        return sweep(args)

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
