#!/usr/bin/env python
"""One-off bulk import of a `companies.csv` dump into the universe overlay.

Reshapes a four-column `ats,name,slug,url` dump into the `name,slug,active`
shape `universe._load_csv` reads, and merges it into the untracked
`data/universe/<ats>.local.csv` -- never the tracked list, which holds only
self-verified slugs.

Workday's `slug` column is the wrong shape (`23andme/23`), so that lane's slug
is derived from the `url` column instead and validated through the source's own
`parse_slug`. Every other lane's slug is already ours.

Tombstones (`active=0`) in either the tracked or the local file win over this
import: a bulk dump must not resurrect a slug the liveness audit disproved.

    uv run python scripts/import_companies_csv.py --dry-run
    uv run python scripts/import_companies_csv.py --source /tmp/companies.csv
    uv run python scripts/import_companies_csv.py --ats workday
"""
from __future__ import annotations

import argparse
import csv
import re
import sys

from src import paths
from src.discovery.sources.ats.workday import parse_slug

DEFAULT_SOURCE = paths.REPO_ROOT / "companies.csv"
UNIVERSE_DIR = paths.REPO_ROOT / "data" / "universe"
ATS_LANES = ("ashby", "greenhouse", "lever", "workday")
COLUMNS = ["name", "slug", "active"]
COUNTERS = ("in_source", "added", "already_known", "tombstoned", "unparseable")

_WORKDAY_URL = re.compile(
    r"^https?://(?P<tenant>[^./]+)\.(?P<pod>wd\d+)\.myworkdayjobs\.com/(?P<path>.+)$",
    re.IGNORECASE,
)
# A path may carry a locale in front of the site id (`/en-US/SiteName`).
_LOCALE = re.compile(r"^[a-z]{2}([-_][a-z0-9]{2,4})?$", re.IGNORECASE)


def workday_slug(url: str) -> str:
    """`https://23andme.wd5.myworkdayjobs.com/23` -> `23andme|wd5|23`.

    Raises ValueError on anything else; the caller counts and skips.
    """
    m = _WORKDAY_URL.match((url or "").strip())
    if not m:
        raise ValueError(f"not a Workday board URL: {url!r}")
    segments = [s for s in m.group("path").split("/") if s]
    if len(segments) > 1 and _LOCALE.match(segments[0]):
        segments = segments[1:]
    if len(segments) != 1:
        raise ValueError(f"cannot pick a site id out of {url!r}")
    return f"{m.group('tenant')}|{m.group('pod')}|{segments[0]}"


def derive_slug(ats: str, row: dict) -> str:
    if ats != "workday":
        return (row.get("slug") or "").strip()
    slug = workday_slug(row.get("url") or "")
    parse_slug(slug)  # the source's own validator is the authority on shape
    return slug


def read_universe_csv(path) -> dict:
    """Every row of one universe CSV, keyed by slug, as `(name, active)`.

    Same fail-open reading as `universe._load_csv`: anything but `0` in
    `active` is live, including a missing column and a blank cell. `active` is
    kept verbatim rather than normalized, so a merge cannot rewrite a value
    this script did not author.
    """
    rows: dict[str, tuple[str, str]] = {}
    if not path.exists():
        return rows
    with open(path, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            name = (row.get("name") or "").strip()
            slug = (row.get("slug") or "").strip()
            if not name or not slug:
                continue
            rows[slug] = (name, (row.get("active") or "").strip())
    return rows


def tombstones(rows: dict) -> set:
    return {slug for slug, (_, active) in rows.items() if active == "0"}


def read_source(path) -> dict:
    rows: dict[str, list] = {ats: [] for ats in ATS_LANES}
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in ("ats", "name", "slug", "url") if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: missing column(s) {', '.join(missing)}")
        for row in reader:
            ats = (row.get("ats") or "").strip().lower()
            if ats in rows:
                rows[ats].append(row)
            elif ats:
                print(f"warning: unsupported ats {ats!r}, skipping row", file=sys.stderr)
    return rows


def merge_lane(ats: str, source_rows: list, universe_dir) -> tuple[list, dict]:
    """(output rows for <ats>.local.csv, counters). Pure apart from reading."""
    counts = dict.fromkeys(COUNTERS, 0)
    counts["in_source"] = len(source_rows)

    tracked = read_universe_csv(universe_dir / f"{ats}.csv")
    local = read_universe_csv(universe_dir / f"{ats}.local.csv")
    dead = tombstones(tracked) | tombstones(local)

    # Seeded with the whole local file, tombstones included: an existing row's
    # name and verdict survive the rewrite untouched.
    out = dict(local)
    for row in source_rows:
        name = (row.get("name") or "").strip()
        try:
            slug = derive_slug(ats, row)
        except ValueError as e:
            counts["unparseable"] += 1
            print(f"warning: {ats}: {e}", file=sys.stderr)
            continue
        if not name or not slug:
            counts["unparseable"] += 1
            print(f"warning: {ats}: empty name or slug in source row, skipping",
                  file=sys.stderr)
            continue
        if slug in dead:
            counts["tombstoned"] += 1
            continue
        if slug in tracked:
            counts["already_known"] += 1
            continue
        if slug in out:
            # Already in the overlay, or a duplicate earlier in this dump: the
            # existing name and verdict stand.
            counts["already_known"] += 1
            continue
        out[slug] = (name, "1")
        counts["added"] += 1

    rows = [[out[s][0], s, out[s][1]] for s in sorted(out)]
    return rows, counts


def write_lane(path, rows) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        w.writerows(rows)


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", help=f"input CSV (default {DEFAULT_SOURCE.name} at the repo root)")
    p.add_argument("--ats", action="append", choices=list(ATS_LANES),
                   help="repeatable; default every lane in the source file")
    p.add_argument("--universe-dir", help="overlay directory (default data/universe)")
    p.add_argument("--dry-run", action="store_true", help="print the summary, write nothing")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    source = paths.REPO_ROOT / args.source if args.source else DEFAULT_SOURCE
    universe_dir = (paths.REPO_ROOT / args.universe_dir) if args.universe_dir else UNIVERSE_DIR
    if not source.exists():
        print(f"error: no such source file: {source}", file=sys.stderr)
        return 1
    if not universe_dir.is_dir():
        print(f"error: no such universe directory: {universe_dir}", file=sys.stderr)
        return 1

    try:
        by_ats = read_source(source)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    lanes = tuple(dict.fromkeys(args.ats)) if args.ats else ATS_LANES
    summary = {}
    for ats in lanes:
        rows, counts = merge_lane(ats, by_ats[ats], universe_dir)
        summary[ats] = counts
        if not args.dry_run and counts["added"]:
            write_lane(universe_dir / f"{ats}.local.csv", rows)

    header = f"{'ats':11} " + " ".join(f"{c:>14}" for c in COUNTERS)
    print(header)
    for ats in lanes:
        print(f"{ats:11} " + " ".join(f"{summary[ats][c]:>14}" for c in COUNTERS))
    if args.dry_run:
        print("\n--dry-run: nothing was written.")
    else:
        touched = [a for a in lanes if summary[a]["added"]]
        print("\nWrote " + (", ".join(f"{a}.local.csv" for a in touched) if touched
                            else "nothing -- no new slugs") + ".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
