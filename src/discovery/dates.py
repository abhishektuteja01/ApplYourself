"""Date coercions for discovery's four posting-date shapes.

One per board family, plus the pandas Series-level one cleaning and the raw
schema share. They are here rather than next to their callers because they are
pure parsing: no HTTP, no board knowledge.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import pandas as pd

_POSTED_TODAY = re.compile(r"posted\s+today", re.IGNORECASE)
_POSTED_YESTERDAY = re.compile(r"posted\s+yesterday", re.IGNORECASE)
_POSTED_N_DAYS_AGO = re.compile(r"posted\s+(\d+)\+?\s+days?\s+ago", re.IGNORECASE)


def iso_date(value) -> date | None:
    """Greenhouse/Ashby: an ISO 8601 timestamp, possibly Z-suffixed."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def ms_date(value) -> date | None:
    """Lever: epoch milliseconds. A bool is an int in Python and never a date."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value / 1000).date()
    except (ValueError, OSError, OverflowError):
        return None


def relative_posted_date(text: str, today: date | None = None) -> date | None:
    """Workday's `postedOn` is always relative text, never a timestamp."""
    if not isinstance(text, str) or not text.strip():
        return None
    today = today or date.today()
    if _POSTED_TODAY.search(text):
        return today
    if _POSTED_YESTERDAY.search(text):
        return today - timedelta(days=1)
    match = _POSTED_N_DAYS_AGO.search(text)
    if match:
        return today - timedelta(days=int(match.group(1)))
    return None


def naive_datetime(values) -> pd.Series:
    """Parse to tz-naive UTC. Both keywords are load-bearing on a column that
    concatenated shards have left as object dtype: without utc=True a single
    tz-aware value coerces every naive one to NaT, and without format="mixed"
    the format inferred from the first element does the same to every element
    that doesn't share it."""
    return pd.to_datetime(
        values, errors="coerce", utc=True, format="mixed"
    ).dt.tz_localize(None)
