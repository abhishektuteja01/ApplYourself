"""Manual JD clips: `inbox/*.md` with YAML frontmatter (company, title, url
required). A clip moves to `.processed/` only after its row is saved, so a
crash leaves it in the inbox for the next run."""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from src.shared import paths
from src.shared import title_gate
from src.discovery.sources.boards.base import LaneStats
from src.discovery.store import make_posting

log = logging.getLogger(__name__)

INBOX = paths.INBOX


def parse_clip(path: Path) -> dict | None:
    """One clip as a store row, or None when it is malformed."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        log.error("unreadable inbox file %s: %s", path, e)
        return None
    lines = text.split("\n")
    if lines[0].strip() != "---":
        log.error("inbox %s: missing YAML frontmatter", path.name)
        return None
    # The closing fence is a line that is exactly "---": a url may contain one.
    end = next((i for i, ln in enumerate(lines[1:], start=1) if ln.strip() == "---"), None)
    if end is None:
        log.error("inbox %s: frontmatter not closed", path.name)
        return None
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as e:
        log.error("inbox %s: YAML error: %s", path.name, e)
        return None
    if not isinstance(meta, dict):
        log.error("inbox %s: frontmatter is not a mapping", path.name)
        return None
    for required in ("company", "title", "url"):
        if not isinstance(meta.get(required), str) or not meta[required].strip():
            log.error("inbox %s: missing required field %s", path.name, required)
            return None
    title = meta["title"].strip()
    return make_posting(
        source="manual", company=meta["company"].strip(), title=title,
        url=(meta.get("url_direct") or meta["url"]).strip(), key_url=meta["url"].strip(),
        location_raw=(meta.get("location") or "").strip(),
        jd_text="\n".join(lines[end + 1:]).strip(),
        posted_date=meta.get("posted_date"), remote_flag=bool(meta.get("remote", False)),
        salary_min=meta.get("salary_min"), salary_max=meta.get("salary_max"),
        salary_currency=meta.get("salary_currency") or "",
        employment_type=meta.get("employment_type") or "",
        seniority_raw=meta.get("seniority") or "",
        vertical=meta.get("vertical") or title_gate.classify_vertical_from_title(title),
    )


class InboxLane:
    name = "manual"

    def __init__(self, inbox_dir: Path | None = None):
        self.inbox_dir = Path(inbox_dir or INBOX)
        self.read: list[Path] = []

    def run(self, crawl) -> LaneStats:
        stats = LaneStats()
        if not self.inbox_dir.exists():
            return stats
        for clip in sorted(self.inbox_dir.glob("*.md")):
            stats.listed += 1
            row = parse_clip(clip)
            if row is None:
                _move(clip, self.inbox_dir / ".malformed")
                stats.errors.append(f"malformed clip moved to .malformed/: {clip.name}")
                continue
            stats.passed += 1
            stats.new += 1
            crawl.batch.upsert(row)
            self.read.append(clip)
        return stats

    def done(self) -> None:
        """Call after the store flush that saved these clips."""
        for clip in self.read:
            _move(clip, self.inbox_dir / ".processed")
        self.read = []


def _move(clip: Path, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / clip.name
    n = 1
    while target.exists():
        target = folder / f"{clip.stem}.{n}{clip.suffix}"
        n += 1
    clip.rename(target)
