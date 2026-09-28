"""What went out: one line per submit click, appended before the click.

`state.yaml` says where a role stands; this file says what was sent to a board.
The two are kept apart because a state file can be re-created, and then the
only record that a role was already applied to would be gone. `apply run`
never clicks for a role with any line here — delete that role's lines to allow
a retry.

Statuses: `clicking` (written right before the click), then one of `accepted`,
`ambiguous`, `unsent`, `rejected` once the board has answered, or `backfill`
for a line rebuilt from old run reports.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

LEDGER_NAME = "submitted.jsonl"


def url_key(url: str) -> str:
    """The form URL with scheme, `www.` and a trailing slash dropped, so the
    same posting reached two ways still compares equal."""
    key = (url or "").strip().casefold()
    for prefix in ("https://", "http://"):
        if key.startswith(prefix):
            key = key[len(prefix):]
    if key.startswith("www."):
        key = key[4:]
    return key.rstrip("/")


def records(path: Path) -> list[dict]:
    """Every line, oldest first. A line that is not JSON is skipped with a
    warning: it cannot name a role either way."""
    if not path.is_file():
        return []
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("%s:%d is not JSON; skipped", path, n)
    return out


def prior(path: Path, job_id: str, form_url: str = "") -> dict | None:
    """The latest line for this role, matched on job_id or on the form URL."""
    key = url_key(form_url)
    hit = None
    for rec in records(path):
        if rec.get("job_id") == job_id or (key and url_key(rec.get("form_url", "")) == key):
            hit = rec
    return hit


def sent_job_ids(path: Path) -> set[str]:
    return {str(rec["job_id"]) for rec in records(path) if rec.get("job_id")}


def append(path: Path, job_id: str, status: str, *, form_url: str = "",
           company: str = "", title: str = "", detail: str = "",
           at: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "at": at or datetime.now().isoformat(timespec="seconds"),
        "job_id": job_id,
        "status": status,
        "company": company,
        "title": title,
        "form_url": form_url,
    }
    if detail:
        rec["detail"] = detail
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
