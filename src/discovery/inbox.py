from __future__ import annotations

import logging
import yaml
from pathlib import Path
import pandas as pd
from src.discovery import cleaning
from src.discovery.sources.base import Source, SourceResult
from src import paths

log = logging.getLogger(__name__)

INBOX = paths.INBOX

def parse_inbox_file(path: Path) -> dict | None:
    """Parse one manual JD clip. Returns a JobSpy-shaped row dict, or None on
    malformed (missing required frontmatter field, bad YAML, unclosed fence)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        log.error("Unreadable inbox file %s: %s", path, e)
        return None
    lines = text.split("\n")
    if lines[0].strip() != "---":
        log.error("Inbox %s missing YAML frontmatter delimiter", path.name)
        return None
    # The closing delimiter is a line that is exactly "---". Splitting on the
    # bare string instead would cut inside a value that happens to contain one
    # -- a Workday url with a "---" in its slug truncates the url and spills
    # the rest of the frontmatter into the body.
    end = next((i for i, ln in enumerate(lines[1:], start=1)
                if ln.strip() == "---"), None)
    if end is None:
        log.error("Inbox %s frontmatter not closed", path.name)
        return None
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as e:
        log.error("Inbox %s YAML error: %s", path.name, e)
        return None
    if not isinstance(meta, dict):
        log.error("Inbox %s frontmatter is not a mapping", path.name)
        return None
    for required in ("company", "title", "url"):
        v = meta.get(required)
        if not isinstance(v, str) or not v.strip():
            log.error("Inbox %s missing required field: %s", path.name, required)
            return None
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    title = meta["title"].strip()
    vertical = meta.get("vertical") or cleaning.classify_vertical_from_title(title)
    return {
        "site": "manual",
        "company": meta["company"].strip(),
        "title": title,
        "location": (meta.get("location") or "").strip(),
        "job_url": meta["url"].strip(),
        "job_url_direct": meta.get("url_direct") or meta["url"].strip(),
        "description": body,
        "date_posted": meta.get("posted_date"),
        "is_remote": bool(meta.get("remote", False)),
        "min_amount": meta.get("salary_min"),
        "max_amount": meta.get("salary_max"),
        "currency": meta.get("salary_currency") or "",
        "job_type": meta.get("employment_type") or "",
        "job_level": meta.get("seniority") or "",
        "vertical": vertical,
        "found_by_term": "",
        "found_by_remote": False,
    }


def ingest_inbox(inbox_dir: Path | None = None) -> tuple[pd.DataFrame, dict]:
    """Read inbox_dir/*.md (skipping .processed/ and .malformed/).
    Valid clips become DataFrame rows + move to .processed/.
    Malformed clips move to .malformed/ with a stderr log.
    inbox_dir defaults to INBOX, resolved at call time so patching it takes;
    both destinations derive from it, so a passed-in dir is self-contained."""
    inbox_dir = INBOX if inbox_dir is None else inbox_dir
    if not inbox_dir.exists():
        return pd.DataFrame(), {"processed": 0, "malformed": 0}
    processed_dir = inbox_dir / ".processed"
    malformed_dir = inbox_dir / ".malformed"
    rows: list[dict] = []
    processed = malformed = 0
    for md in sorted(inbox_dir.glob("*.md")):
        parsed = parse_inbox_file(md)
        if parsed is None:
            malformed_dir.mkdir(parents=True, exist_ok=True)
            md.rename(malformed_dir / md.name)
            malformed += 1
        else:
            rows.append(parsed)
            processed_dir.mkdir(parents=True, exist_ok=True)
            md.rename(processed_dir / md.name)
            processed += 1
    df = pd.DataFrame(rows) if rows else pd.DataFrame()
    return df, {"processed": processed, "malformed": malformed}


class InboxSource(Source):
    name = "manual"

    def fetch(self, ctx) -> SourceResult:
        df, counts = ingest_inbox()
        # A Source returns list[dict], and NaN must become None on the way out so
        # downstream sees a missing value, not a float.
        if not df.empty:
            df = df.where(pd.notnull(df), None)
            rows = df.to_dict("records")
        else:
            rows = []

        lines = [
            f"Inbox: processed={counts.get('processed', 0)}, malformed={counts.get('malformed', 0)}"
        ]
        return SourceResult(rows=rows, report_lines=lines, errors=[])
