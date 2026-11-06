"""The one atomic text write: a temp file unique to this writer, fsynced, then
renamed over the target, so a reader never sees half a file."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4


def atomic_write_text(path: Path, text: str, *, mkdir: bool = False) -> None:
    """Write text to path through a sibling temp file and an atomic rename.
    The file keeps the usual mode (not mkstemp's 0600)."""
    path = Path(path)
    if mkdir:
        path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
