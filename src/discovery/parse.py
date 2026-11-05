"""Read fields out of what a source sends: HTML, JobPosting blocks, dates,
salary, booleans. One reading of each, whichever source sends it.
"""
from __future__ import annotations

import html as html_mod
import json
import re
from datetime import UTC, date, datetime, timedelta

import pandas as pd

from src.shared.identity import normalize_company

# Shorter than this is a JS shell or a truncated fetch, not a JD.
MIN_JD_CHARS = 200

# ---------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------

_BR_RE = re.compile(r"<br\s*/?>", re.I)
_LI_RE = re.compile(r"<li[^>]*>", re.I)
_HEAD_RE = re.compile(r"<h[1-6][^>]*>", re.I)
_BLOCK_END_RE = re.compile(r"</(?:p|div|li|ul|ol|h[1-6]|tr|table|section|blockquote)>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_DROP_BLOCKS_RE = re.compile(r"<(script|style|noscript|template|svg)\b.*?</\1\s*>", re.I | re.S)
_SPACES_RE = re.compile(r"[ \t]{2,}")
_BLANKS_RE = re.compile(r"\n{3,}")
MAX_DESCRIPTION_CHARS = 25_000


def html_to_text(value) -> str:
    """HTML (possibly entity-encoded twice, as Greenhouse sends it) ->
    markdown-ish text: list items become `- `, headings `## `. Script and
    style bodies are dropped, so a full page works too."""
    if not isinstance(value, str) or not value.strip():
        return ""
    x = html_mod.unescape(value)
    x = _DROP_BLOCKS_RE.sub(" ", x)
    x = _BR_RE.sub("\n", x)
    x = _LI_RE.sub("\n- ", x)
    x = _HEAD_RE.sub("\n\n## ", x)
    x = _BLOCK_END_RE.sub("\n", x)
    x = _TAG_RE.sub(" ", x)
    x = html_mod.unescape(x)
    lines = [_SPACES_RE.sub(" ", ln.strip()) for ln in x.splitlines()]
    x = _BLANKS_RE.sub("\n\n", "\n".join(lines)).strip()
    return x[:MAX_DESCRIPTION_CHARS]


# A schema.org JobPosting block, the structured copy job sites embed for
# search engines: the JD of a page whose own markup is a JS shell.
_LD_JSON_RE = re.compile(
    r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.I | re.S)


def _job_postings(node):
    if isinstance(node, list):
        for item in node:
            yield from _job_postings(item)
    elif isinstance(node, dict):
        kind = node.get("@type")
        if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
            yield node
        yield from _job_postings(node.get("@graph"))


def _page_postings(page):
    if not isinstance(page, str):
        return
    for m in _LD_JSON_RE.finditer(page):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        yield from _job_postings(data)


def jobposting_text(page) -> str:
    """The JD in a page's schema.org JobPosting block, as text, or ""."""
    for posting in _page_postings(page):
        text = html_to_text(posting.get("description"))
        if text:
            return text
    return ""


def jobposting_employer(page) -> str:
    """The employer a page's JobPosting block names (`hiringOrganization`),
    or "" for none or a placeholder."""
    for posting in _page_postings(page):
        org = posting.get("hiringOrganization")
        name = org.get("name") if isinstance(org, dict) else org
        if isinstance(name, str) and normalize_company(name):
            return name.strip()
    return ""


_LOWER_RE = re.compile(r"[a-z]")
_UPPER_RE = re.compile(r"[A-Z]")
_DIGIT_RUN_RE = re.compile(r"\d{4,}")


def looks_like_slug(name) -> bool:
    """A company written as an identifier, not a name: no capital letter
    ("whatfix", "careers.pnc.com"), or a long digit run ("appsilon-1739358905")."""
    if not isinstance(name, str):
        return False
    return bool(_LOWER_RE.search(name) and not _UPPER_RE.search(name)) or bool(_DIGIT_RUN_RE.search(name))


# ---------------------------------------------------------------------
# field values -- one reading of each, whichever source sends it
# ---------------------------------------------------------------------

_TRUE_WORDS = frozenset({"true", "yes", "y", "1"})


def as_bool(value) -> bool:
    """A flag that may arrive as a bool, a number, NaN or text ("false")."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == value and value != 0
    return isinstance(value, str) and value.strip().lower() in _TRUE_WORDS


# Working periods per year, by the word a pay period is named with.
_PERIODS_PER_YEAR = (("hour", 2080), ("day", 260), ("daily", 260), ("week", 52),
                     ("month", 12))


def yearly_salary(amount, period) -> float | None:
    """`amount` per `period` ("HOUR", "hourly", "per-month-salary", "1 YEAR")
    as a yearly figure. No period means yearly; no number means None."""
    value = pd.to_numeric(amount, errors="coerce")
    if pd.isna(value):
        return None
    word = str(period or "").lower()
    factor = next((n for key, n in _PERIODS_PER_YEAR if key in word), 1)
    return float(value) * factor


# ---------------------------------------------------------------------
# dates -- always UTC, so the laptop's timezone never changes a row
# ---------------------------------------------------------------------

_POSTED_TODAY = re.compile(r"posted\s+today", re.IGNORECASE)
_POSTED_YESTERDAY = re.compile(r"posted\s+yesterday", re.IGNORECASE)
_POSTED_N_DAYS_AGO = re.compile(r"posted\s+(\d+)\+?\s+days?\s+ago", re.IGNORECASE)


def iso_date(value) -> date | None:
    """Greenhouse/Ashby: an ISO 8601 timestamp, possibly Z-suffixed."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC)
    return parsed.date()


def ms_date(value) -> date | None:
    """Lever: epoch milliseconds. A bool is an int in Python and never a date."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC).date()
    except (ValueError, OSError, OverflowError):
        return None


def relative_posted_date(text: str, today: date) -> date | None:
    """Workday's `postedOn` is relative text ("Posted 3 Days Ago")."""
    if not isinstance(text, str) or not text.strip():
        return None
    if _POSTED_TODAY.search(text):
        return today
    if _POSTED_YESTERDAY.search(text):
        return today - timedelta(days=1)
    match = _POSTED_N_DAYS_AGO.search(text)
    if match:
        return today - timedelta(days=int(match.group(1)))
    return None


def naive_datetime(values) -> pd.Series:
    """Parse to tz-naive UTC. `utc=True` and `format="mixed"` are both needed
    on an object column mixing naive and aware values."""
    return pd.to_datetime(
        values, errors="coerce", utc=True, format="mixed"
    ).dt.tz_localize(None)
