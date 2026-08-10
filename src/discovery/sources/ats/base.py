"""Shared paced pass over one ATS's company universe. Subclasses supply the board
URL and the row parser; everything else is here.
"""
from __future__ import annotations

import time

from src.discovery import cleaning
from src.discovery import trace
from src.discovery import universe
from src.discovery.sources.ats.http import CareersError, fetch_json
from src.discovery.sources.base import Source, SourceResult

# A 200 can decode to anything: [], a JSON string, {"error": ...}. Raised out of
# a row parser these become an AttributeError/TypeError that escapes the company
# loop, and the orchestrator then discards the whole source's shard — every
# company polled before the bad one, which can be hours of paced fetching.
#
# No health strike on this path: it means valid JSON of an unexpected shape,
# which points at an API change rather than a dead board, and update_health is
# documented as permanent-death-only. A decommissioned board serving an HTML
# error page decodes as invalid JSON and arrives as CareersError instead.
PAYLOAD_SHAPE_ERRORS = (AttributeError, TypeError, KeyError, ValueError, IndexError)


def job_items(payload, key: str) -> list[dict]:
    """Items under `key` of a dict payload, skipping anything that isn't a dict.

    Raises TypeError when the payload itself is the wrong shape, so a garbage
    200 is reported as a malformed board rather than counted as a healthy poll
    of a board with no openings. An empty `{"jobs": []}` is the latter.
    """
    if not isinstance(payload, dict):
        raise TypeError(f"expected a JSON object, got {type(payload).__name__}")
    items = payload.get(key)
    if not isinstance(items, list):
        raise TypeError(f"expected a list under {key!r}, got {type(items).__name__}")
    return [i for i in items if isinstance(i, dict)]


class AtsBoardSource(Source):
    """One paced pass over `universe.load(self.name)`."""

    def board_url(self, slug: str) -> str:
        raise NotImplementedError

    def parse_rows(self, payload, company: str) -> list[dict]:
        raise NotImplementedError

    def fetch(self, ctx) -> SourceResult:
        pacing = max(1.0, ctx.config.sources[self.name].pacing_seconds)
        companies = universe.load(self.name)

        rows: list[dict] = []
        errors: list[str] = []
        report_lines: list[str] = []
        kept = 0
        polled = 0
        ok = 0
        err_404 = 0
        err_other = 0
        shape_errors = 0

        ticker = trace.Ticker(self.name, len(companies), every=250)

        for i, c in enumerate(companies):
            if ctx.deadline_reached():
                break

            if i > 0:
                time.sleep(pacing)

            polled += 1
            ticker.tick(polled, ok=ok, err=err_404 + err_other)
            try:
                payload = fetch_json(self.board_url(c.slug), deadline_ts=ctx.deadline_ts)
                # Parsed inside the try: a malformed payload must stay a
                # per-company error like a 404 does.
                company_rows = self.parse_rows(payload, c.name)
            except CareersError as e:
                is_404 = e.status == 404
                if is_404:
                    err_404 += 1
                else:
                    err_other += 1
                errors.append(f"{c.name}: {e}")
                if e.permanent:
                    universe.update_health(self.name, c.slug, success=False)
                if c.priority or not is_404:
                    report_lines.append(
                        f"| {c.name} | ERROR | 0 | 0 | {str(e).replace('|', '\\|')[:80]} |")
                continue
            except PAYLOAD_SHAPE_ERRORS as e:
                err_other += 1
                shape_errors += 1
                # These are the same exception types a bug in parse_rows would
                # raise. One company means bad data; every company means the
                # parser is broken, and swallowing that would hand the
                # orchestrator a valid empty shard instead of failing loud.
                if shape_errors > 1 and shape_errors == polled:
                    raise
                msg = f"malformed board payload: {type(e).__name__}: {e}"
                errors.append(f"{c.name}: {msg}")
                report_lines.append(
                    f"| {c.name} | ERROR | 0 | 0 | {msg.replace('|', '\\|')[:80]} |")
                continue

            c_fetched = 0
            c_kept = 0
            for row in company_rows:
                c_fetched += 1
                vertical = cleaning.classify_vertical_from_title(row["title"])
                if not vertical:
                    continue
                c_kept += 1
                row["vertical"] = vertical
                rows.append(row)

            ok += 1
            kept += c_kept
            universe.update_health(self.name, c.slug, success=True, rows=c_fetched)

            if c.priority:
                report_lines.append(f"| {c.name} | OK | {c_fetched} | {c_kept} | |")

        ticker.finish(polled, ok=ok, err=err_404 + err_other, kept=kept)

        summary = (f"Companies polled: {polled} | OK: {ok} | 404: {err_404} "
                   f"| Err: {err_other} | Rows kept: {kept}")
        report_summary = [summary, ""]
        if report_lines:
            report_summary.extend([
                "| company | status | fetched | kept | error |",
                "|---|---|---|---|---|",
            ] + report_lines)

        return SourceResult(rows, report_summary, errors)
