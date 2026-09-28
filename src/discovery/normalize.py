"""Pure row helpers every discovery step shares: names, job_id, the title
gate, posting keys, HTML and dates.

No I/O, no HTTP, no LLM (R7). One implementation of each rule, so a board lane,
the store and `ingest-url` cannot drift apart.
"""
from __future__ import annotations

import hashlib
import html as html_mod
import re
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

import pandas as pd

from src import verticals
from src.ats_registry import parse_posting_url

# Shorter than this is a JS shell or a truncated fetch, not a JD.
MIN_JD_CHARS = 200

# ---------------------------------------------------------------------
# company / title / job_id
# ---------------------------------------------------------------------

_VIA_SOURCES = ("linkedin", "indeed", "glassdoor", "google", "ziprecruiter", "zip recruiter")
_VIA_RE = re.compile(
    r"\s+via\s+(?:" + "|".join(re.escape(v) for v in _VIA_SOURCES) + r")\s*$"
)
_SUFFIX_RE = re.compile(r"\s+(?:inc|llc|corp|corporation|ltd|limited|co|gmbh|plc|sa)\s*$")
_LEADING_THE_RE = re.compile(r"^the\s+")
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]")


def normalize_company(s: str | None) -> str:
    if not isinstance(s, str) or not s:
        return ""
    x = s.lower()
    x = _VIA_RE.sub("", x)
    x = _PUNCT_RE.sub(" ", x)
    prev = None
    while prev != x:
        prev = x
        x = _SUFFIX_RE.sub("", x)
    x = _LEADING_THE_RE.sub("", x)
    return _WS_RE.sub(" ", x).strip()


def normalize_title(s: str | None) -> str:
    if not isinstance(s, str) or not s:
        return ""
    x = s.lower()
    x = _PUNCT_RE.sub(" ", x)
    return _WS_RE.sub(" ", x).strip()


def compute_job_id(company_normalized: str, title_normalized: str) -> str:
    """The inputs are frozen: url and jd_text stay out so the id survives a
    re-scrape. Changing them orphans every pipeline/<job_id>/state.yaml."""
    key = f"{company_normalized}|{title_normalized}".encode("utf-8")
    return hashlib.sha1(key).hexdigest()[:8]


_SLUG_SEP_RE = re.compile(r"[-_+]+")
# Below this a tenant slug is an internal abbreviation ("nc"), not a name.
MIN_COMPANY_SLUG_CHARS = 3


def company_from_url(url: str) -> str:
    """The employer a board posting url's tenant slug names, or "" when the
    url names no tenant we trust."""
    hit = parse_posting_url(url)
    if hit is None or not hit.slug:
        return ""
    name = _WS_RE.sub(" ", _SLUG_SEP_RE.sub(" ", unquote(hit.slug))).strip()
    if len(name) < MIN_COMPANY_SLUG_CHARS or not any(c.isalpha() for c in name):
        return ""
    return name.title() if name == name.lower() else name


# ---------------------------------------------------------------------
# vertical classifier and title gate
# ---------------------------------------------------------------------

def classify_vertical_from_title(title: str | None) -> str:
    """The first `classifier_rules` lane whose rule matches, or ""."""
    if not isinstance(title, str) or not title:
        return ""
    for vertical, rule in verticals.get_config().classifier_rules:
        if rule.matches(title):
            return vertical
    return ""


def _term_pattern(term: str) -> str:
    r"""`term` escaped, with \b only on a side that ends in a word character,
    so "c++" and ".net" still match."""
    pat = re.escape(term)
    if term[:1].isalnum():
        pat = r"\b" + pat
    if term[-1:].isalnum():
        pat = pat + r"\b"
    return pat


def _compile(terms) -> re.Pattern | None:
    if not terms:
        return None
    return re.compile("|".join(_term_pattern(t) for t in terms), re.IGNORECASE)


@lru_cache(maxsize=64)
def _gate_patterns(exclude: tuple, include: tuple, strong: tuple):
    return _compile(exclude), _compile(include), _compile(strong)


def title_passes(title: str, vertical: str, source: str = "", vcfg=None) -> bool:
    """The per-lane title gate: strong_keep OR (include_ok AND NOT exclude).
    include_ok is True when the lane sets no include terms. A manual add and
    a title in no lane are not gated here."""
    if source == "manual":
        return True
    vcfg = vcfg or verticals.get_config()
    lane = vcfg.verticals.get(vertical)
    if lane is None:
        return True
    exclude, include, strong = _gate_patterns(
        tuple(lane.title_exclude_terms), tuple(lane.title_include_terms),
        tuple(lane.title_strong_keep_terms))
    # "_" is a word character to \b, so "IN_Manager_Tax" hides "manager".
    text = (title or "").replace("_", " ")
    if strong and strong.search(text):
        return True
    if include and not include.search(text):
        return False
    return not (exclude and exclude.search(text))


def gate_reason(title: str, source: str = "", vcfg=None) -> tuple[str, bool]:
    """(vertical, passes) for a board posting: classify, then gate. A title
    that classifies into no lane never passes."""
    vertical = classify_vertical_from_title(title)
    if not vertical:
        return "", False
    return vertical, title_passes(title, vertical, source, vcfg)


# ---------------------------------------------------------------------
# posting key: one identity per posting, whatever url shape found it
# ---------------------------------------------------------------------

# Query params that only say where a click came from. Dropped from a key;
# every other param is kept, since an unknown careers page may put its
# posting id in any of them.
_TRACKING_PARAMS = frozenset({
    "src", "source", "ref", "referrer", "refid", "trk", "trackingid", "from",
    "gh_src", "lever-source", "lever-origin", "codes", "mode", "iis", "iisn",
})
_LINKEDIN_VIEW_RE = re.compile(r"/jobs/view/(?:[^/?#]*-)?(\d+)")


def posting_key(url: str) -> str:
    """A stable key for the posting a url names, or "" for no url.

    Board urls key on the board's own posting id, so `?src=LinkedIn` and a
    company careers page carrying `?gh_jid=` land on one key. LinkedIn keys on
    its numeric job id, Indeed on `jk`. Anything else is host + path plus its
    query params, minus the known tracking ones.
    """
    text = (url or "").strip()
    if not text:
        return ""
    hit = parse_posting_url(text)
    if hit is not None and hit.source == "workday":
        # The req id is the last path segment after /job/, with or without a
        # city segment before it; the site and locale in front vary.
        path = urlsplit(text).path
        if "/job/" in path:
            return f"workday|{hit.slug.lower()}|{path.rstrip('/').rsplit('/', 1)[-1].lower()}"
    if hit is not None and hit.posting_id:
        return f"{hit.source}|{hit.posting_id.lower()}"
    try:
        parts = urlsplit(text)
    except ValueError:
        return f"url|{text.lower()}"
    host = (parts.hostname or "").lower().removeprefix("www.")
    params = [(k.lower(), v) for k, v in parse_qsl(parts.query, keep_blank_values=False)]
    if host.endswith("linkedin.com"):
        m = _LINKEDIN_VIEW_RE.search(parts.path)
        job = m.group(1) if m else next((v for k, v in params if k == "currentjobid"), "")
        if job.isdigit():
            return f"linkedin|{job}"
    if "indeed." in host:
        jk = next((v for k, v in params if k == "jk"), "")
        if jk:
            return f"indeed|{jk.lower()}"
    kept = sorted((k, v) for k, v in params
                  if k not in _TRACKING_PARAMS and not k.startswith("utm_"))
    path = parts.path.rstrip("/")
    query = f"?{urlencode(kept)}" if kept else ""
    return f"url|{host}{path}{query}"


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
        parsed = parsed.astimezone(timezone.utc)
    return parsed.date()


def ms_date(value) -> date | None:
    """Lever: epoch milliseconds. A bool is an int in Python and never a date."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date()
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


def utc_today() -> date:
    return datetime.now(timezone.utc).date()
