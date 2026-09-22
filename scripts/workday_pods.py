#!/usr/bin/env python
"""Re-find a Workday tenant whose pod moved, and write it as a proposal.

A Workday slug is `tenant|pod|site_id`, and the pod is the customer's hosting
cell -- `wd5`, `wd103`, `wd115`. Workday migrates tenants between cells, and
when it does the old host stops answering while the tenant and site id stay
exactly the same. The audit sees a 410 or a transport failure and records
`dead` or `error`, which is right about the URL and wrong about the company.

These are the most valuable rows in the universe to get right. Enterprise
Workday boards are large: re-probing the 193 non-active rows across the known
pods recovered 15 tenants carrying 6,928 live postings, including SSM Health
(1,889), Takeda (1,782) and Comcast (707). The 6,928 were invisible to the
pipeline because one URL segment had changed.

Report only by default; `--out` writes a proposal CSV that
`scripts/apply_reconcile.py --verdict moved` can apply.

    uv run python scripts/workday_pods.py --audit jobs/slug_audit_2026-09-21.csv
    uv run python scripts/workday_pods.py --audit jobs/... --out jobs/workday_pods.csv
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from src import paths
from src.discovery.sources.ats import workday as wd

_spec = importlib.util.spec_from_file_location(
    "slug_resolvers", paths.REPO_ROOT / "scripts" / "slug_resolvers.py")
R = importlib.util.module_from_spec(_spec)
sys.modules["slug_resolvers"] = R
_spec.loader.exec_module(R)

OUT_DIR = paths.REPO_ROOT / "jobs"

# Every cell seen across the tracked universe plus the ones the recovered
# tenants moved to. Ordered by how often a move landed there, so the common
# case costs one request: wd115 accounted for 11 of the first 15 recoveries.
PODS = ("wd115", "wd5", "wd1", "wd3", "wd502", "wd503", "wd103", "wd108",
        "wd102", "wd105", "wd10", "wd12", "wd2", "wd501", "wd101", "wd104",
        "wd106", "wd107", "wd109", "wd110", "wd12", "wd123")

# The audit statuses worth re-probing. `active` boards are answering already,
# and `stale` is a judgment about posting age rather than about the URL.
RETRY_STATUSES = frozenset({"dead", "empty", "error"})


def find_pod(tenant: str, site: str, current: str, pacing: float) -> tuple[str, int] | None:
    """(pod, postings) for the first cell that serves this tenant, else None."""
    for pod in PODS:
        if pod == current:
            continue
        time.sleep(pacing)
        try:
            total = wd.list_page(tenant, pod, site, offset=0).get("total")
        except Exception:  # noqa: BLE001 -- a wrong cell is the normal case
            continue
        if isinstance(total, int) and total > 0:
            return pod, total
    return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--audit", required=True)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--pacing", type=float, default=0.15)
    p.add_argument("--limit", type=int)
    p.add_argument("--out", help="write a proposal CSV apply_reconcile can read")
    args = p.parse_args(argv or sys.argv[1:])

    with open(paths.REPO_ROOT / args.audit, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f)
                if r["ats"] == "workday" and r["status"] in RETRY_STATUSES]
    if args.limit:
        rows = rows[:args.limit]
    print(f"{len(rows)} non-active workday row(s) to re-probe across {len(PODS)} pods\n")

    lock = threading.Lock()
    hits: list[dict] = []

    def work(row):
        try:
            tenant, pod, site = wd.parse_slug(row["slug"])
        except Exception:  # noqa: BLE001 -- a malformed slug is not a pod move
            return
        found = find_pod(tenant, site, pod, args.pacing)
        if found is None:
            return
        new_pod, total = found
        with lock:
            hits.append({
                "ats": "workday", "slug": row["slug"], "name": row["name"],
                "old_status": row["status"], "verdict": R.MOVED,
                "new_ats": "workday", "new_slug": f"{tenant}|{new_pod}|{site}",
                "new_jobs": total, "acquirer": "", "confidence": R.HIGH,
                "domain": "", "evidence": f"pod {pod}->{new_pod}; {total} postings",
            })
            print(f"  {row['old_status'] if 'old_status' in row else row['status']:6} "
                  f"{row['slug'][:38]:39}-> {tenant}|{new_pod}|{site:<20} {total:5} postings")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(work, rows))

    hits.sort(key=lambda h: -int(h["new_jobs"]))
    total = sum(int(h["new_jobs"]) for h in hits)
    print(f"\n{len(hits)} of {len(rows)} tenants re-found, {total} postings behind them")

    if args.out:
        out = paths.REPO_ROOT / args.out
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=R.COLUMNS)
            w.writeheader()
            w.writerows(hits)
        print(f"-> {out}")
    else:
        print("no --out given, so nothing was written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
