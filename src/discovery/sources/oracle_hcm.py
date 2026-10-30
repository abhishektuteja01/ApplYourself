"""Oracle Recruiting Cloud: the JD of one posting, which its job page builds
in the browser. Not a board lane; the feed's rows from it carry a short JD,
and this fills it from the site's public requisition-detail API. A row named
by its tenant code ("Egug") is named from its candidate site's page."""
from __future__ import annotations

import html
import re
from urllib.parse import parse_qsl, quote, urlsplit

from src.ats_registry import parse_posting_url
from src.discovery import normalize as N

# The parts of a requisition that are the role; the corporate blurb is not.
JD_FIELDS = ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")

_SITE_NAME_RE = re.compile(r'<meta\s+property="og:site_name"\s+content="([^"]*)"')
# Oracle's default site label, which names no employer.
_DEFAULT_LABEL = "candidate experience site"
# Words a site label adds around the employer's name ("Rheem Career Site", "Jobs onsemi").
_LABEL_TAIL_RE = re.compile(r"\s+(?:careers?|job board|opportunities)\b.*$", re.I)
_LABEL_HEAD_RE = re.compile(r"^(?:jobs|careers)\s+", re.I)


def _site_number(hit, parts) -> str:
    return hit.site_id or dict(parse_qsl(parts.query)).get("site_number", "")


def detail_url(url: str) -> str:
    """The requisition-detail API url for an Oracle posting url, or ""."""
    hit = parse_posting_url(url)
    if hit is None or hit.source != "oracle" or not hit.posting_id:
        return ""
    parts = urlsplit(url)
    site = _site_number(hit, parts)
    if not site:
        return ""
    return (f"https://{parts.hostname}/hcmRestApi/resources/latest/"
            f"recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
            f"&finder=ById;Id=%22{quote(hit.posting_id)}%22,siteNumber={quote(site)}")


def jd_text(payload) -> str:
    """The JD in a requisition-detail response, "" when there is none."""
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return ""
    return "\n\n".join(t for t in (N.html_to_text(items[0].get(f) or "") for f in JD_FIELDS) if t)


def site_url(url: str, company: str) -> str:
    """The candidate site page of an Oracle posting whose company is its
    tenant code ("Egug" for egug.fa.us2.oraclecloud.com), or ""."""
    hit = parse_posting_url(url)
    if hit is None or hit.source != "oracle" or (company or "").strip().lower() != hit.slug.lower():
        return ""
    parts = urlsplit(url)
    site = _site_number(hit, parts)
    return f"https://{parts.hostname}/hcmUI/CandidateExperience/en/sites/{quote(site)}" if site else ""


def site_name(page) -> str:
    """The employer a candidate site's page names (its `og:site_name`, label
    words cut), or "" for none or Oracle's default label."""
    m = _SITE_NAME_RE.search(page) if isinstance(page, str) else None
    label = html.unescape(m.group(1)).strip() if m else ""
    if label.lower() == _DEFAULT_LABEL:
        return ""
    name = label.split(" - ")[0].split("_")[0]
    name = _LABEL_HEAD_RE.sub("", _LABEL_TAIL_RE.sub("", name)).strip(" -")
    return name if N.normalize_company(name) else ""
