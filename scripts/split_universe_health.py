"""One-time migration: jobs/universe_health.parquet -> one ledger per ATS.

The three ATS sources run concurrently, and update_health does a full
read-modify-write per company, so a shared file would race into lost updates.
Splitting by `ats` gives each lane its own writer.

The ledger is gitignored, so it has no backup: this copies the original aside
before writing anything, and refuses to overwrite existing per-ATS files unless
--force. Without the migration, discovery re-polls every board pruned so far.

    uv run python scripts/split_universe_health.py --dry-run
    uv run python scripts/split_universe_health.py
"""
from __future__ import annotations

import argparse
import shutil
import sys

import pandas as pd

from src.discovery import universe
from src.discovery.sources.ats.registry import ATS_SOURCE_NAMES
from src.parquet_io import write_parquet

LEGACY_PATH = universe.HEALTH_DIR / "universe_health.parquet"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report the split, write nothing")
    ap.add_argument("--force", action="store_true", help="overwrite existing per-ATS ledgers")
    args = ap.parse_args(argv)

    if not LEGACY_PATH.exists():
        print(f"Nothing to migrate: {LEGACY_PATH} does not exist.")
        return 0

    df = pd.read_parquet(LEGACY_PATH)
    print(f"{LEGACY_PATH}: {len(df)} rows, {int(df['pruned_at'].notna().sum())} pruned")

    existing = [a for a in ATS_SOURCE_NAMES if universe.health_path(a).exists()]
    if existing and not args.force:
        print(f"ERROR: per-ATS ledger(s) already exist: {', '.join(existing)}. "
              "Re-run with --force to overwrite.", file=sys.stderr)
        return 1

    # An `ats` value outside the registry has no lane to be read by, so it would
    # silently vanish. Report it rather than dropping it quietly.
    unknown = sorted(set(df["ats"].dropna().unique()) - set(ATS_SOURCE_NAMES))
    if unknown:
        print(f"WARNING: {int(df['ats'].isin(unknown).sum())} row(s) with unrecognised "
              f"ats {unknown} will not be migrated.", file=sys.stderr)

    plan = {ats: df[df["ats"] == ats].reset_index(drop=True) for ats in ATS_SOURCE_NAMES}
    for ats, part in plan.items():
        print(f"  {ats:11} {len(part):>5} rows, {int(part['pruned_at'].notna().sum()):>4} pruned "
              f"-> {universe.health_path(ats).name}")

    migrated = sum(len(p) for p in plan.values())
    print(f"  {'total':11} {migrated:>5} rows of {len(df)}")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    backup = LEGACY_PATH.with_suffix(".parquet.pre-split")
    shutil.copy2(LEGACY_PATH, backup)
    print(f"\nBacked up to {backup}")

    for ats, part in plan.items():
        write_parquet(part, universe.health_path(ats))

    LEGACY_PATH.unlink()
    print(f"Wrote {len(plan)} ledgers and removed {LEGACY_PATH.name} "
          f"(restore from {backup.name} if needed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
