"""The user's confirmation of an /apply answers file.

`apply confirm <file>` shows the file's answers, takes a typed yes on a
terminal, and stores a hash of what it showed in `<file>.confirmed`. A file
counts as confirmed only while that hash matches its current content, so a
session-written `"confirmed": true` or a later edit does not.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

SIDECAR_SUFFIX = ".confirmed"
_NOT_ANSWERS = ("job_id", "confirmed", "country")


def sidecar(path: Path) -> Path:
    return path.with_name(path.name + SIDECAR_SUFFIX)


def load(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a JSON object")
    return data


def digest(data: dict) -> str:
    shown = {k: v for k, v in data.items() if k != "confirmed"}
    text = json.dumps(shown, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render(data: dict) -> str:
    """Every answer in the file, every key shown: what the user approves."""
    lines = [f"Answers for job_id {data.get('job_id', '(none)')}:"]
    # The role's country, as the session read it: work-authorization answers follow it.
    lines.append(f"Country: {data.get('country') or '(not set: the source field or unknown)'}")
    entries = [(k, v) for k, v in data.items() if k not in _NOT_ANSWERS]
    if not entries:
        lines.append("  (no answers)")
    for field_id, entry in entries:
        if not isinstance(entry, dict):
            lines.append(f"{field_id}: {json.dumps(entry, ensure_ascii=False)}")
            continue
        label = entry.get("label")
        lines.append(f"{field_id}" + (f' — "{label}"' if label else ""))
        value = entry.get("value")
        if isinstance(value, list):
            value = " | ".join(str(v) for v in value)
        fact = entry.get("fact")
        if isinstance(fact, dict):
            value = f"Yes when {fact.get('fact')} is {fact.get('yes_when')}"
        tail = f"  [{entry['source']}]" if entry.get("source") else ""
        if entry.get("question"):
            tail += f" -> saved as {entry['question']}"
        lines.append(f"  {value}{tail}")
        for key in sorted(set(entry) - {"label", "value", "fact", "source", "question"}):
            lines.append(f"  {key}: {json.dumps(entry[key], ensure_ascii=False)}")
    return "\n".join(lines)


def record(path: Path, data: dict) -> Path:
    out = sidecar(path)
    out.write_text(digest(data) + "\n", encoding="utf-8")
    return out


def is_confirmed(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        stored = sidecar(Path(path)).read_text(encoding="utf-8").strip()
        return bool(stored) and stored == digest(load(path))
    except (OSError, ValueError):
        return False
