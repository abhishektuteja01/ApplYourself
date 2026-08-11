"""Which board a posting URL belongs to, and whether /apply can submit to it.

Split out of `apply_cli` so `shortlist.py` can ask the question without
importing the whole submission CLI. Both callers must agree: a role the
shortlist calls auto-submittable and the queue calls manual-apply is exactly
the silent gap §13 exists to close.

Deterministic and LLM-free (R7). No company or vertical names — the parsers
key off URL shape only.
"""
from __future__ import annotations

from src.apply import ashby, lever
from src.apply.greenhouse import ApplyUrlError, parse_posting

_ATS_PARSERS = {
    "greenhouse": parse_posting,
    "lever": lever.parse_posting,
    "ashby": ashby.parse_posting,
}

# Boards with a working browser driver — the ones `apply run --submit` can
# actually reach.
#
# `tests/apply/test_fill.py` asserts this stays equal to `fill._DRIVER_NAMES`,
# so adding a driver without widening this set fails the suite rather than
# leaving the shortlist quietly understating what the queue can do.
SUBMITTABLE_ATS = frozenset({"greenhouse", "lever", "ashby"})


def detect_ats(url: str) -> str | None:
    """The board this URL belongs to, or None if it is not one we parse."""
    for ats, parser in _ATS_PARSERS.items():
        try:
            parser(url)
        except ApplyUrlError:
            continue
        return ats
    return None


def is_auto_submittable(url: str | None) -> bool:
    """Whether `/apply` can submit to this posting without a human.

    False covers two different situations that look identical on a shortlist
    — Workday, and LinkedIn/Indeed and other aggregator reposts — and both
    mean the same thing to the user: apply to this one by hand.
    """
    return detect_ats(url or "") in SUBMITTABLE_ATS
