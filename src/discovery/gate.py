"""The pre-fetch title gate.

Cleaning's `apply_title_exclusion` is the authoritative per-vertical title
filter, but it runs at cleaning step 0 — hours after a crawl. Any source that
pays for a per-row fetch (LinkedIn's detail backfill, Workday's detail stage)
runs the same gate first, so the rows it drops never cost a request. Single
implementation: the callers must not drift.

Regex over titles, no LLM: R7 holds.
"""
from __future__ import annotations

import pandas as pd

from src.discovery.cleaning import apply_title_exclusion


def passing_titles(items, verticals_config, source_name: str) -> list[bool]:
    """Per-item gate verdict, aligned with `items` — (title, vertical) pairs."""
    items = list(items)
    if not items:
        return []
    frame = pd.DataFrame(
        [{"title": title, "vertical": vertical, "source": source_name}
         for title, vertical in items]
    )
    kept, _ = apply_title_exclusion(frame, verticals_config)
    return frame.index.isin(kept.index).tolist()


def gate_passing_urls(rows, ctx, source_name: str) -> set[str]:
    """job_urls whose row passes the per-vertical title gate.

    A URL skipped here belongs to a row cleaning was going to drop on its
    title regardless of description."""
    verdicts = passing_titles(
        ((row["title"], row["vertical"]) for row in rows), ctx.verticals, source_name)
    return {row["job_url"] for row, ok in zip(rows, verdicts) if ok and row["job_url"]}
