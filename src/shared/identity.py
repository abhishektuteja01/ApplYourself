"""How a job is identified: company and title names, the job_id hash, the posting key.

No I/O, no LLM (R7). One implementation of each rule, so discovery, scoring and
/apply cannot drift apart.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from src.shared.job_boards import parse_posting_url

# ---------------------------------------------------------------------
# company / title / job_id
# ---------------------------------------------------------------------

_VIA_SOURCES = ("linkedin", "indeed", "glassdoor", "google", "ziprecruiter", "zip recruiter")
_VIA_RE = re.compile(r"\s+via\s+(?:" + "|".join(re.escape(v) for v in _VIA_SOURCES) + r")\s*$")
# What job portals print when the employer is not named ("see description",
# "not provided"). Kept as a company, these would merge different employers'
# roles under one id pair.
_PLACEHOLDER_COMPANIES = frozenset(
    {"siehe beschreibung", "non renseigné", "non renseigne", "unavailable"}
)
# Frozen: company_normalized feeds job_id. Widen DEDUPE_SUFFIXES instead.
_ID_SUFFIXES = ("inc", "llc", "corp", "corporation", "ltd", "limited", "co", "gmbh", "plc", "sa")
# Legal forms dropped for matching only (dedupe_key), never for the id.
DEDUPE_SUFFIXES = (
    *_ID_SUFFIXES,
    *("ag", "kg", "llp", "lp", "bv", "b v", "nv", "n v", "se", "ab", "as", "oy", "oyj"),
    *("srl", "spa", "sas", "sarl", "pvt", "private", "pty", "pte", "kk", "ug", "sl"),
)


def _suffix_re(suffixes: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(r"\s+(?:" + "|".join(re.escape(x) for x in suffixes) + r")\s*$")


_SUFFIX_RE = _suffix_re(_ID_SUFFIXES)
_DEDUPE_SUFFIX_RE = _suffix_re(DEDUPE_SUFFIXES)
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
    x = _WS_RE.sub(" ", x).strip()
    return "" if x in _PLACEHOLDER_COMPANIES else x


def normalize_title(s: str | None) -> str:
    if not isinstance(s, str) or not s:
        return ""
    x = s.lower()
    x = _PUNCT_RE.sub(" ", x)
    return _WS_RE.sub(" ", x).strip()


def dedupe_key(company_normalized: str, title_normalized: str) -> str:
    """The pair as the matcher compares it: wider legal-suffix list, spaces
    squashed. Matching only; it never feeds job_id, so it may change freely."""
    x, prev = company_normalized or "", None
    while prev != x:
        prev = x
        x = _DEDUPE_SUFFIX_RE.sub("", x)
    company = x.replace(" ", "")
    if not company or not title_normalized:
        return ""
    return f"{company}|{title_normalized}"


def compute_job_id(company_normalized: str, title_normalized: str) -> str:
    """The inputs are frozen: url and jd_text stay out so the id survives a
    re-scrape. Changing them orphans every pipeline/<job_id>/state.yaml."""
    key = f"{company_normalized}|{title_normalized}".encode()
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
# posting key: one identity per posting, whatever url shape found it
# ---------------------------------------------------------------------

# Query params that only say where a click came from. Dropped from a key;
# every other param is kept, since an unknown careers page may put its
# posting id in any of them.
_TRACKING_PARAMS = frozenset(
    {
        "src",
        "source",
        "ref",
        "referrer",
        "refid",
        "trk",
        "trackingid",
        "from",
        "gh_src",
        "lever-source",
        "lever-origin",
        "codes",
        "mode",
        "iis",
        "iisn",
    }
)
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
    if hit is not None and hit.source == "oracle" and hit.posting_id:
        # Requisition ids are per tenant, not global.
        return f"oracle|{hit.slug.lower()}|{hit.posting_id.lower()}"
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
    kept = sorted(
        (k, v) for k, v in params if k not in _TRACKING_PARAMS and not k.startswith("utm_")
    )
    path = parts.path.rstrip("/")
    query = f"?{urlencode(kept)}" if kept else ""
    return f"url|{host}{path}{query}"
