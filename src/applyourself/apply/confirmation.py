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


def resolved_country(data: dict) -> tuple[str, str]:
    """(country, source) the answers will use for this role (`role_facts`)."""
    from applyourself import state_io
    from applyourself.apply.bank_schema import AnswersError, load_answers
    from applyourself.apply.role_facts import country_and_source, session_country_of
    from applyourself.shared import paths

    job_id = str(data.get("job_id") or "")
    try:
        answers = load_answers()
    except (AnswersError, OSError):
        answers = None
    state = state_io.load_state(state_io.state_path_for(paths.PIPELINE, job_id)) if job_id else None
    location = str((state or {}).get("location") or "")
    return country_and_source(job_id, session_country_of(data), answers, location)


def digest(data: dict) -> str:
    # The country the answers follow is part of what was approved: a re-score
    # that changes it undoes the confirmation.
    shown = {k: v for k, v in data.items() if k != "confirmed"}
    shown["_country"] = list(resolved_country(data))
    text = json.dumps(shown, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render(data: dict) -> str:
    """Every answer in the file, every key shown: what the user approves."""
    lines = [f"Answers for job_id {data.get('job_id', '(none)')}:"]
    # The country the work-authorization answers follow, and where it came from.
    country, source = resolved_country(data)
    lines.append(f"Country: {country} ({source})")
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
