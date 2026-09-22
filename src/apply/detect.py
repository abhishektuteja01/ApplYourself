"""Which board a posting URL belongs to, and whether /apply can submit to it.

Split out of `apply_cli` so `shortlist.py` can ask the question without
importing the whole submission CLI. Both callers must agree: a role the
shortlist calls auto-submittable and the queue calls manual-apply is exactly
the silent gap §13 exists to close.

Board identity itself lives in one table, `discovery/sources/ats/registry.py`,
shared with discovery — so dedupe, ingest and submission cannot drift on what
a URL is.

Deterministic and LLM-free (R7). No company or vertical names — detection
keys off URL shape only.
"""
from __future__ import annotations

from src.apply import ashby, lever
from src.apply.greenhouse import ApplyUrlError, parse_posting
from src.discovery.sources.ats.registry import (
    SUBMITTABLE_SOURCES,
    detect_source,
)

_ATS_PARSERS = {
    "greenhouse": parse_posting,
    "lever": lever.parse_posting,
    "ashby": ashby.parse_posting,
}

# Boards with a working browser driver — the ones `apply run --submit` can
# actually reach. Derived from the registry, which `tests/apply/test_fill.py`
# also holds `fill._DRIVER_NAMES` against, so adding a driver without
# widening `submittable` fails the suite rather than leaving the shortlist
# quietly understating what the queue can do.
SUBMITTABLE_ATS = SUBMITTABLE_SOURCES

__all__ = ["SUBMITTABLE_ATS", "ApplyUrlError", "detect_ats",
           "is_auto_submittable"]


def detect_ats(url: str) -> str | None:
    """The board this URL belongs to, or None if it is not one we parse."""
    return detect_source(url or "")


def is_auto_submittable(url: str | None) -> bool:
    """Whether `/apply` can submit to this posting without a human.

    False covers two different situations that look identical on a shortlist
    — Workday, and LinkedIn/Indeed and other aggregator reposts — and both
    mean the same thing to the user: apply to this one by hand.
    """
    return detect_ats(url or "") in SUBMITTABLE_ATS
