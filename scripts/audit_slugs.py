#!/usr/bin/env python
"""One-off liveness audit of the ATS slug universe. Report only.

The nightly run learns one thing about a board: whether it answered. Three
consecutive 404s prune it, and `last_kept_at` records when it last produced a
row *we kept* -- which our own title gate decides. So a board serving 200 with
zero postings, and one whose newest posting is two years old, are both
indistinguishable from a healthy board that simply has no AI roles.

This walks every slug once and says which it is. It writes a single CSV and
prints a summary; it does not touch the health ledgers, `data/universe/*.csv`,
or anything under `pipeline/`.

    uv run python scripts/audit_slugs.py --stale-days 90
    uv run python scripts/audit_slugs.py --ats ashby --limit 50
    uv run python scripts/audit_slugs.py --resume        # continue today's run
"""
from __future__ import annotations

import argparse
import csv
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from src import paths
from src.ats_http import CareersError, fetch_json, fetch_json_post
from src.discovery import universe
from src.discovery.config import load_config, pacing_floor
from src.discovery.dates import iso_date, ms_date, relative_posted_date
from src.discovery.sources.ats import workday as wd

OUT_DIR = paths.REPO_ROOT / "jobs"
DEFAULT_ATS = ("ashby", "greenhouse", "lever")
DEFAULT_STALE_DAYS = 90
COLUMNS = ["ats", "slug", "name", "status", "postings", "newest_posting", "http", "note"]

ACTIVE, STALE, EMPTY, DEAD, ERROR = "active", "stale", "empty", "dead", "error"


# --- payload -> (count, newest posting date). Pure; one per ATS. ------------
# Each reads the same date field its scraper in src/discovery/sources/ats/
# reads, so "newest posting" here means the same thing a kept row's
# `posted_date` would. A payload of the wrong shape raises, and the caller
# reports it as `error` rather than counting it as an empty board.

def _newest(dates) -> date | None:
    real = [d for d in dates if d]
    return max(real) if real else None


def _items(payload, key: str) -> list[dict]:
    if not isinstance(payload, dict):
        raise TypeError(f"expected a JSON object, got {type(payload).__name__}")
    items = payload.get(key)
    if not isinstance(items, list):
        raise TypeError(f"expected a list under {key!r}, got {type(items).__name__}")
    return [i for i in items if isinstance(i, dict)]


def greenhouse_summary(payload) -> tuple[int, date | None]:
    items = _items(payload, "jobs")
    return len(items), _newest(
        iso_date(i.get("first_published") or i.get("updated_at")) for i in items
    )


def ashby_summary(payload) -> tuple[int, date | None]:
    items = _items(payload, "jobs")
    return len(items), _newest(iso_date(i.get("publishedAt")) for i in items)


def lever_summary(payload) -> tuple[int, date | None]:
    if not isinstance(payload, list):
        raise TypeError(f"expected a JSON array, got {type(payload).__name__}")
    items = [i for i in payload if isinstance(i, dict)]
    return len(items), _newest(ms_date(i.get("createdAt")) for i in items)


def workday_summary(payload) -> tuple[int, date | None]:
    """Count from page 0's `total`, date from page 0's postings only.

    `total` is untrustworthy past page 0 (see workday.py) but page 0's own
    value is the board's stated size, which is all a liveness call needs. The
    date is therefore a lower bound on the board's newest posting -- Workday
    does not promise the default sort is by date.
    """
    items = _items(payload, "jobPostings")
    total = payload.get("total")
    count = total if isinstance(total, int) and total >= len(items) else len(items)
    return count, _newest(relative_posted_date(i.get("postedOn") or "") for i in items)


SUMMARIZE = {
    "greenhouse": greenhouse_summary,
    "ashby": ashby_summary,
    "lever": lever_summary,
    "workday": workday_summary,
}


def board_url(ats: str, slug: str) -> str:
    """The audit's own URLs, deliberately lighter than the scrapers'.

    Greenhouse drops `content=true` and Ashby drops `includeCompensation`: a
    liveness call needs the titles and dates, not 9,000 full JDs.
    """
    if ats == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    if ats == "ashby":
        return f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    if ats == "lever":
        return f"https://api.lever.co/v0/postings/{slug}?mode=json"
    raise ValueError(f"no audit URL for ats {ats!r}")


def classify(count: int, newest: date | None, stale_days: int, today: date) -> str:
    """`empty` and `stale` are the two the nightly cannot see.

    A board with postings but no parseable date reads `active`: unknown age is
    not evidence of abandonment, and calling it stale would drop live boards.
    """
    if count <= 0:
        return EMPTY
    if newest is None:
        return ACTIVE
    return STALE if (today - newest).days > stale_days else ACTIVE


def probe(ats: str, slug: str) -> tuple[str, int, date | None, str, str]:
    """(status, postings, newest, http, note) for one slug. Never raises."""
    try:
        if ats == "workday":
            company, pod, site_id = wd.parse_slug(slug)
            payload = wd.list_page(company, pod, site_id, offset=0)
        else:
            payload = fetch_json(board_url(ats, slug))
    except CareersError as e:
        status = DEAD if e.status in (404, 410) else ERROR
        return status, 0, None, str(e.status or ""), str(e)
    except Exception as e:  # noqa: BLE001 -- a bad slug must not end the pass
        return ERROR, 0, None, "", f"{type(e).__name__}: {e}"

    try:
        count, newest = SUMMARIZE[ats](payload)
    except Exception as e:  # noqa: BLE001 -- malformed 200, not a dead board
        return ERROR, 0, None, "200", f"malformed payload: {type(e).__name__}: {e}"
    return None, count, newest, "200", ""


class Writer:
    """Appends a row per slug and flushes each one, so a run killed at hour two
    keeps everything it learned and `--resume` picks up from there."""

    def __init__(self, path, existing: bool):
        self._fh = path.open("a" if existing else "w", newline="", encoding="utf-8")
        self._csv = csv.writer(self._fh)
        self._lock = threading.Lock()
        if not existing:
            self._csv.writerow(COLUMNS)
            self._fh.flush()

    def write(self, row: list) -> None:
        with self._lock:
            self._csv.writerow(row)
            self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def audit_lane(ats: str, companies, pacing: float, stale_days: int, today: date,
               writer: Writer, counts: dict) -> None:
    for n, company in enumerate(companies):
        if n:
            time.sleep(pacing)
        status, count, newest, http, note = probe(ats, company.slug)
        if status is None:
            status = classify(count, newest, stale_days, today)
        counts[status] = counts.get(status, 0) + 1
        writer.write([ats, company.slug, company.name, status, count,
                      newest.isoformat() if newest else "", http, note])


def load_done(path) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    with path.open(encoding="utf-8") as f:
        return {(r["ats"], r["slug"]) for r in csv.DictReader(f)}


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ats", action="append", choices=sorted(SUMMARIZE),
                   help="repeatable; default ashby/greenhouse/lever (workday is opt-in)")
    p.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS,
                   help=f"newest posting older than this is stale (default {DEFAULT_STALE_DAYS})")
    p.add_argument("--pacing", type=float,
                   help="seconds between fetches in one lane; default is discovery.yaml's")
    p.add_argument("--limit", type=int, help="first N slugs per lane, for a smoke run")
    p.add_argument("--resume", action="store_true",
                   help="skip slugs already in today's output file")
    p.add_argument("--out", help="output CSV (default jobs/slug_audit_<date>.csv)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv or sys.argv[1:])
    lanes = tuple(dict.fromkeys(args.ats)) if args.ats else DEFAULT_ATS
    today = date.today()
    out = paths.REPO_ROOT / args.out if args.out else OUT_DIR / f"slug_audit_{today}.csv"

    cfg = load_config()

    done = load_done(out) if args.resume else set()
    if args.resume and not done:
        print(f"--resume: nothing to resume from in {out}, starting fresh")

    work = {}
    for ats in lanes:
        companies = sorted(universe.universe_companies(ats), key=lambda c: c.slug)
        companies = [c for c in companies if (ats, c.slug) not in done]
        if args.limit:
            companies = companies[:args.limit]
        work[ats] = companies

    pacings = {ats: (args.pacing if args.pacing is not None
                     else max(pacing_floor(ats), cfg.sources[ats].pacing_seconds))
               for ats in lanes}

    for ats in lanes:
        print(f"{ats:11} {len(work[ats]):6} slugs at {pacings[ats]:g}s "
              f"(~{len(work[ats]) * pacings[ats] / 60:.0f} min)")
    if done:
        print(f"resuming: {len(done)} already done")
    print(f"-> {out}\n")

    writer = Writer(out, existing=bool(done) and out.exists())
    counts = {ats: {} for ats in lanes}
    try:
        with ThreadPoolExecutor(max_workers=len(lanes)) as pool:
            futures = {ats: pool.submit(audit_lane, ats, work[ats], pacings[ats],
                                        args.stale_days, today, writer, counts[ats])
                       for ats in lanes}
            for ats, fut in futures.items():
                fut.result()
    finally:
        writer.close()

    print(f"{'ats':11} {'active':>7} {'stale':>7} {'empty':>7} {'dead':>7} {'error':>7}")
    for ats in lanes:
        c = counts[ats]
        print(f"{ats:11} " + " ".join(f"{c.get(k, 0):>7}"
                                      for k in (ACTIVE, STALE, EMPTY, DEAD, ERROR)))
    print(f"\nWrote {out}. Nothing else was modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
