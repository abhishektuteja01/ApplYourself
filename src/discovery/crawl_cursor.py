"""Where a paged, search-scoped crawl left off, persisted across runs.

Workday is the first source whose cost scales as tenants x search terms rather
than tenants. With 55 tenants and 48 distinct terms that is 2,640 list
requests as a *floor* — one page each, before a single posting is looked at —
which at the configured pacing does not fit a run's deadline. Two separate
problems come out of that, and this module holds the state for both:

**The floor.** A run cannot visit every (tenant, term) pair, so it visits a
slice and the next run resumes after it. `next_slug` is that resume point.
Without it a deadline cut drops the same alphabetical tail every run,
forever — the tenants at the end of the CSV would never be crawled at all.

**The ceiling.** `MAX_PAGES_PER_TERM` bounds one pair's pagination. The claim
that "a page never reached is read again from page 0 next run, so nothing is
lost" was only true of deadline truncation, not of the cap: with every run
starting at offset 0, pages past the cap were never read on any run.
`offsets` records each pair's deep frontier so later pages are eventually
reached, and resets to 0 when a pair runs out of results.

Note the frontier is the *second* page a run reads, never the first. Workday's
list endpoint takes no sort parameter and defaults to newest-first, so
skipping offset 0 to resume deep would blind the crawl to new postings — the
one thing it is for. `workday.py` reads page 0 every run and uses this only
to continue past it.

Deliberately not a column on `universe_health_*.parquet`: that schema is read
by existing files on disk, and this state is per (slug, term) rather than per
slug. Kept as JSON because it is small, human-readable when something looks
wrong, and rewritten whole.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from src.paths import JOBS

log = logging.getLogger(__name__)

CURSOR_DIR = JOBS
_SCHEMA_VERSION = 1


def cursor_path(ats: str) -> Path:
    """Per-ATS, matching `universe.health_path`'s reasoning: sources run
    concurrently and a shared file would race into lost updates."""
    return CURSOR_DIR / f"crawl_cursor_{ats}.json"


@dataclass
class CrawlCursor:
    ats: str
    next_slug: str = ""
    """The tenant to resume at. A *slug*, not an index: `universe.load()` sorts
    by (priority, last_yield), `last_yield` is rewritten every run and pruning
    changes the list length, so position 20 in one run is a different tenant
    in the next. A positional cursor made coverage lumpy — simulated over 12
    runs it gave 1-7 visits per tenant against an ideal 4.4, where a
    slug-keyed one gives a tight 4-5."""

    offsets: dict[str, int] = field(default_factory=dict)
    """`"<slug>\\x00<term>"` -> the offset to resume that pair's paging from."""

    @staticmethod
    def _key(slug: str, term: str) -> str:
        # NUL cannot appear in either half, so the join is unambiguous where
        # a "|" would collide with Workday's tri-part slug.
        return f"{slug}\x00{term}"

    def offset_for(self, slug: str, term: str) -> int:
        return int(self.offsets.get(self._key(slug, term), 0))

    def set_offset(self, slug: str, term: str, offset: int) -> None:
        key = self._key(slug, term)
        if offset:
            self.offsets[key] = int(offset)
        else:
            # Exhausted: drop the key rather than storing a zero, so the file
            # stays proportional to what is actually mid-pagination.
            self.offsets.pop(key, None)

    def rotate(self, items: list, key=lambda x: x) -> list:
        """`items` reordered to start where the last run stopped.

        Returns everything, not a slice — the caller stops on its own
        deadline. Rotating rather than truncating means a short run still
        makes progress on the tenants a long run would have reached last,
        instead of re-crawling the same head every time and never reaching
        the tail.

        Resuming by slug rather than index: if the saved tenant is gone
        (pruned, or removed from the CSV) this falls back to the head, which
        costs one run's rotation and never mis-seeks.
        """
        if not items:
            return []
        keys = [key(i) for i in items]
        try:
            start = keys.index(self.next_slug)
        except ValueError:
            start = 0
        return items[start:] + items[:start]

    def advance(self, items: list, done: int, key=lambda x: x) -> None:
        """Record the tenant the next run should resume at: the one after the
        last completed. A completed pass wraps back to the head."""
        if not items:
            return
        self.next_slug = key(items[done % len(items)]) if done < len(items) else key(items[0])


def load_cursor(ats: str) -> CrawlCursor:
    """Never raises: a corrupt or absent cursor means "start from the top",
    which costs one run's rotation and cannot break a crawl."""
    path = cursor_path(ats)
    if not path.exists():
        return CrawlCursor(ats=ats)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != _SCHEMA_VERSION:
            return CrawlCursor(ats=ats)
        return CrawlCursor(
            ats=ats,
            next_slug=str(data.get("next_slug", "") or ""),
            offsets={str(k): int(v) for k, v in (data.get("offsets") or {}).items()},
        )
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        log.warning("crawl cursor %s unreadable (%s); starting from the top", path, exc)
        return CrawlCursor(ats=ats)


def save_cursor(cursor: CrawlCursor) -> None:
    """Best-effort: losing a cursor write costs coverage on the next run, and
    must never fail a crawl that already did its work."""
    path = cursor_path(cursor.ats)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "schema_version": _SCHEMA_VERSION,
                "next_slug": cursor.next_slug,
                "offsets": cursor.offsets,
            }, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("could not write crawl cursor %s: %s", path, exc)
