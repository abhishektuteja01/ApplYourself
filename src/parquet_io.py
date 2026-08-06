"""Atomic parquet writes.

Every canonical parquet in this repo (clean, scored, seen, health) is read by
the next stage and by the slash commands. A crash or a full disk mid-write
leaves a truncated file that pandas cannot open, which takes the whole pipeline
down until it is deleted by hand. state_io._write_state already writes through
a temp file; this is the same guarantee for parquet.
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd


def write_parquet(df: pd.DataFrame, path: Path, *, index: bool = False) -> None:
    """Write df to path atomically, creating the parent directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        df.to_parquet(tmp, index=index)
        # Same directory, so this is a real atomic replace on POSIX.
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
