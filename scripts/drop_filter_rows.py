"""Remove the `filter:*` rows from scored.parquet (one-time).

A hard-rule drop is no longer stored; `score start` checks it again each run.
After this, the next `/score` re-checks those roles under today's rules and
judges the ones that now pass. --dry-run prints the counts by code; --apply
writes, under the store's lock.

  uv run python scripts/drop_filter_rows.py --dry-run [--scored PATH]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from applyourself.shared import paths, schemas  # noqa: E402
from applyourself.shared.parquet_io import locked, schema_version, write_parquet  # noqa: E402


def _filter_mask(df: pd.DataFrame) -> pd.Series:
    return df["scored_by_model"].astype(str).str.startswith("filter:")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    ap.add_argument("--scored", type=Path, default=paths.SCORED)
    args = ap.parse_args(argv)
    if not args.scored.exists():
        print(f"{args.scored} not found: nothing to migrate")
        return 0
    with locked(args.scored):
        schemas.check(schema_version(args.scored), schemas.SCORED_SCHEMA_VERSION, args.scored)
        df = pd.read_parquet(args.scored)
        mask = _filter_mask(df)
        codes = df.loc[mask, "scored_by_model"].str[len("filter:") :].value_counts()
        print(f"{int(mask.sum())} filter rows of {len(df)}")
        for code, n in codes.items():
            print(f"  {code}: {n}")
        if args.apply and mask.any():
            write_parquet(df[~mask], args.scored, schema_version=schemas.SCORED_SCHEMA_VERSION)
    print("written" if args.apply and mask.any() else "nothing written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
