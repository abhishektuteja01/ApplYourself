"""Is one posting still up? Asked by the night's tailor stage of an old role.

`posting_status(url)` is "live", "gone" or "" (could not tell). A board with a
JSON API we read (Greenhouse, Lever, Ashby, Workday, arbeitsagentur) answers
through it: a 404 is gone, and so is Workday's 403 `S22`. Any other url is gone
only on a plain 404 or 410 for its page. LinkedIn and Indeed are never asked.

Deterministic and LLM-free (R7).
"""

from __future__ import annotations

from urllib.parse import urlsplit

from applyourself.discovery.sources.boards import workday
from applyourself.discovery.sources.feed.stapply import posting_api
from applyourself.shared import http_client

LIVE, GONE, UNKNOWN = "live", "gone", ""
GONE_PAGE = (404, 410)
WORKDAY_GONE = "S22"  # Workday's errorCode for a removed posting, on a 403
_NOT_ASKED = ("linkedin.com", "indeed.com")


def _host_not_asked(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _NOT_ASKED)


def posting_status(url: str, deadline_ts: float | None = None) -> str:
    if not url or _host_not_asked(url):
        return UNKNOWN
    try:
        api = workday.detail_api_url(url)
        if api:
            status, body = http_client.fetch_page(api)
            if status == 200:
                return LIVE
            gone = status == 404 or (status == 403 and WORKDAY_GONE in body)
            return GONE if gone else UNKNOWN
        found = posting_api(url, deadline_ts)
        if found is not None:
            try:
                return LIVE if found[1]() else UNKNOWN
            except http_client.CareersError as e:
                return GONE if e.status == 404 else UNKNOWN
        status, _ = http_client.fetch_page(url)
    except Exception:  # noqa: BLE001 -- one posting; unknown means tailor as before
        return UNKNOWN
    return GONE if status in GONE_PAGE else UNKNOWN


def role_status(urls: list[str], pacer: http_client.Pacer | None = None) -> tuple[str, str]:
    """(status, the url that said it) for a role's posting urls: gone when one
    says gone and none says live; live when one says live; else unknown."""
    gone = ""
    for url in dict.fromkeys(u for u in urls if u):
        if _host_not_asked(url):
            continue
        if pacer is not None:
            pacer.wait()
        status = posting_status(url)
        if status == LIVE:
            return LIVE, url
        if status == GONE and not gone:
            gone = url
    return (GONE, gone) if gone else (UNKNOWN, "")
