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
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

_VERSION_KEY = b"schema_version"


def write_parquet(
    df: pd.DataFrame, path: Path, *, index: bool = False, schema_version: int | None = None
) -> None:
    """Write df to path atomically, creating the parent directory. A
    `schema_version` is stored in the file's metadata."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        if schema_version is None:
            df.to_parquet(tmp, index=index)
        else:
            table = pa.Table.from_pandas(df, preserve_index=index)
            meta = {**(table.schema.metadata or {}), _VERSION_KEY: str(schema_version).encode()}
            pq.write_table(table.replace_schema_metadata(meta), tmp)  # type: ignore[no-untyped-call]
        with tmp.open("rb") as f:
            os.fsync(f.fileno())
        # Same directory, so this is a real atomic replace on POSIX.
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def schema_version(path: Path) -> int | None:
    """The version `write_parquet` stored in path, or None for a file without one."""
    raw = (pq.read_schema(path).metadata or {}).get(_VERSION_KEY)  # type: ignore[no-untyped-call]
    return int(raw) if raw is not None else None


def read_parquet(path: Path, known: int, **kwargs: Any) -> pd.DataFrame:
    """pd.read_parquet, refusing a file stamped newer than `known`."""
    from src.shared import schemas

    schemas.check(schema_version(path), known, path)
    return pd.read_parquet(path, **kwargs)


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
