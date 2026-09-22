"""Seed jobs/company_aliases.parquet from the roles already being tracked.

    uv run python scripts/seed_company_aliases.py [--dry-run]

job_id is sha1(company_normalized|title_normalized). Once dedupe merges two
spellings of one company, one spelling feeds that hash — and every role whose
id was built from the losing spelling is orphaned from its
pipeline/<job_id>/state.yaml and its applications/<dir>. This script pins the
choice before the merge is switched on, so nothing already tracked moves.

What it does:
  - reads every pipeline/*/state.yaml (READ only — /track is the sole writer,
    R10) and rebuilds each role's normalized company/title,
  - blocks them into candidate pairs and applies the merge predicate from
    src/discovery/dedupe.py, pulling JD text from jobs/clean.parquet; a role no
    longer in clean.parquet simply has no JD evidence, and no merge happens on
    a guess,
  - picks each group's surviving job_id with the sticky-id rule (tracked beats
    untracked; several tracked resolve by state precedence then last_touch),
  - appends variant -> canonical rows for the group, source_of_truth `manual`.

--dry-run writes nothing and exits 1 if any tracked job_id would change that is
not in the reviewed expected-merge list (--expected, default
jobs/expected_merges.txt, one `<job_id> <job_id>` pair per line, `#` comments).
That is the acceptance gate: unexpected must be empty.

Roles whose stored company/title no longer reproduce their own job_id are
reported separately and excluded from the gate count, so pre-existing drift
cannot mask a real regression.

The /track worklist it prints is advice. This script never runs a transition
and never touches a state.yaml.
"""
from __future__ import annotations

import argparse
import itertools
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

import pandas as pd

from src import paths, state_io
from src.discovery import aliases, cleaning, dedupe

DEFAULT_LEDGER = paths.JOBS / "company_aliases.parquet"
DEFAULT_EXPECTED = paths.JOBS / "expected_merges.txt"

#: A loser in one of these needs no transition — it is already closed.
CLOSED = state_io.CLOSED_STATES


def load_records(pipeline_dir: Path, clean_path: Path) -> tuple[list[dict], list[dict]]:
    """(usable records, records whose job_id no longer reproduces)."""
    jd_by_id: dict[str, str] = {}
    if clean_path.exists():
        clean = pd.read_parquet(clean_path, columns=["job_id", "jd_text"])
        jd_by_id = {j: (t or "") for j, t in zip(clean["job_id"], clean["jd_text"])}

    good, drifted = [], []
    for job_id, state in sorted(state_io.load_state_index(pipeline_dir).items()):
        company = state.get("company") or ""
        title = state.get("title") or ""
        record = {
            "job_id": job_id,
            "company": company,
            "title": title,
            "url": state.get("url") or "",
            "company_normalized": cleaning.normalize_company(company),
            "title_normalized": cleaning.normalize_title(title),
            "jd_text": jd_by_id.get(job_id, ""),
            "state": state.get("state") or "",
            "last_touch": state.get("last_touch") or "",
            "first_touch": _first_touch(state),
        }
        if cleaning.compute_job_id(
            record["company_normalized"], record["title_normalized"]
        ) != job_id:
            drifted.append(record)
        else:
            good.append(record)
    return good, drifted


def _first_touch(state: Mapping) -> str:
    history = state.get("state_history") or []
    if history and isinstance(history[0], Mapping):
        return str(history[0].get("at") or "")
    return str(state.get("last_touch") or "")


def merge_groups(records: list[dict]) -> list[list[dict]]:
    """Union-find over the pairs the merge predicate accepts."""
    parent = {r["job_id"]: r["job_id"] for r in records}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_title: dict[str, list[dict]] = {}
    for record in records:
        by_title.setdefault(record["title_normalized"], []).append(record)

    for title, group in by_title.items():
        if not title or len(group) < 2:
            continue
        for a, b in itertools.combinations(group, 2):
            if dedupe.blocks_together(a, b) and dedupe.same_posting(a, b):
                parent[find(a["job_id"])] = find(b["job_id"])

    clusters: dict[str, list[dict]] = {}
    for record in records:
        clusters.setdefault(find(record["job_id"]), []).append(record)
    return [g for g in clusters.values() if len(g) > 1]


def plan_group(group: list[dict], state_index: Mapping[str, Mapping]) -> dict:
    """Canonical spelling, surviving job_id and the ids it absorbs."""
    ordered = sorted(group, key=lambda r: (r["first_touch"], r["job_id"]))
    canonical = aliases.choose_canonical([
        aliases.AliasCandidate(r["company_normalized"], aliases.source_of_truth(r["url"]))
        for r in ordered
    ])
    survivor = aliases.select_sticky_id([r["job_id"] for r in ordered], state_index)
    return {
        "canonical": canonical,
        "survivor": survivor,
        "members": ordered,
        "losers": [r for r in ordered if r["job_id"] != survivor],
    }


def load_expected(path: Path) -> set[frozenset[str]]:
    if not path.exists():
        return set()
    pairs = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        ids = line.split()
        if len(ids) == 2:
            pairs.add(frozenset(ids))
    return pairs


def track_worklist(plans: Iterable[dict]) -> list[str]:
    lines = []
    for plan in plans:
        for loser in plan["losers"]:
            if loser["state"] in CLOSED:
                lines.append(
                    f"# {loser['job_id']} already closed ({loser['state']}) — "
                    f"duplicate of {plan['survivor']}"
                )
            else:
                lines.append(
                    f"uv run track {loser['job_id']} withdrawn "
                    f'--note "duplicate of {plan["survivor"]}"'
                )
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report only; exit 1 on any unexpected job_id change")
    parser.add_argument("--pipeline-dir", type=Path, default=paths.PIPELINE)
    parser.add_argument("--clean", type=Path, default=paths.CLEAN)
    parser.add_argument("--out", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--expected", type=Path, default=DEFAULT_EXPECTED)
    args = parser.parse_args()

    records, drifted = load_records(args.pipeline_dir, args.clean)
    state_index = {r["job_id"]: r for r in records}
    plans = [plan_group(g, state_index) for g in merge_groups(records)]
    plans.sort(key=lambda p: p["survivor"])
    expected_pairs = load_expected(args.expected)

    mappings = []
    for plan in plans:
        for member in plan["members"]:
            mappings.append((member["company_normalized"], plan["canonical"], "manual"))

    print(f"tracked roles: {len(records)}   merge groups: {len(plans)}")

    print("\nmappings")
    for variant, canonical, origin in mappings:
        marker = "=" if variant == canonical else "->"
        print(f"  {variant} {marker} {canonical}  [{origin}]")

    expected, unexpected = [], []
    for plan in plans:
        for loser in plan["losers"]:
            line = (
                f"  {loser['job_id']} ({loser['state']}) -> {plan['survivor']}"
                f"   {loser['company']} / {_survivor_of(plan)['company']}"
            )
            target = expected if frozenset(
                {loser["job_id"], plan["survivor"]}
            ) in expected_pairs else unexpected
            target.append(line)

    print(f"\njob_id changes — expected ({len(expected)})")
    for line in expected:
        print(line)
    print(f"\njob_id changes — UNEXPECTED ({len(unexpected)})")
    for line in unexpected:
        print(line)

    print(f"\nexcluded, job_id does not reproduce from its own company/title ({len(drifted)})")
    for record in drifted:
        print(f"  {record['job_id']}  {record['company']} / {record['title']}")

    print("\n/track worklist (not run by this script)")
    for line in track_worklist(plans):
        print(line)

    if args.dry_run:
        if unexpected:
            print(f"\ngate FAILED: {len(unexpected)} unexpected job_id change(s)",
                  file=sys.stderr)
            return 1
        print("\ngate passed; nothing written")
        return 0

    ledger = aliases.load_ledger(args.out)
    before = len(ledger)
    ledger = aliases.append_aliases(ledger, mappings, pd.Timestamp.today())
    aliases.write_ledger(ledger, args.out)
    print(f"\nwrote {args.out}: {before} -> {len(ledger)} rows")
    return 0


def _survivor_of(plan: dict) -> dict:
    return next(m for m in plan["members"] if m["job_id"] == plan["survivor"])


if __name__ == "__main__":
    raise SystemExit(main())
