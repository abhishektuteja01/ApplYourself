"""Atomic parquet writes.

Every canonical parquet in this repo (store, boards, clean, scored) is read by
the next stage and by the slash commands. A crash or a full disk mid-write
leaves a truncated file that pandas cannot open, which takes the whole pipeline
down until it is deleted by hand. state_io._write_state already writes through
a temp file; this is the same guarantee for parquet.

The temp name is unique per writer: an `ingest-url` concurrent with a nightly
run would otherwise race on one shared `<name>.tmp`.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import pandas as pd


def write_parquet(df: pd.DataFrame, path: Path, *, index: bool = False) -> None:
    """Write df to path atomically, creating the parent directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        df.to_parquet(tmp, index=index)
        with tmp.open("rb") as f:
            os.fsync(f.fileno())
        # Same directory, so this is a real atomic replace on POSIX.
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    """Exclusive advisory lock on `<path>.lock` for a read-modify-write of
    path. Blocks until the other writer finishes."""
    lock = Path(path).with_name(f".{Path(path).name}.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("w", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
