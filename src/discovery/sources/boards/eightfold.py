"""Eightfold: a search-scoped list of ten a page with no JDs, then one detail
call per new posting that passes the title gate.

Read-only discovery. The slug is `host|domain`: the careers host and the
company domain its API asks for. A tenant answers one of two APIs, PCSX
(`/api/pcsx/...`) or the older apply/v2 (`/api/apply/v2/...`); the list tries
PCSX and falls back to v2 when PCSX is refused. Each tenant is searched with
each lane's first search term, like Workday, and paging stops on a short
page, a page with nothing that passes the gate, or `max_pages`.
"""
from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import quote

from src.discovery import parse
from src.discovery.sources.boards.base import Board, BoardLane
from src.discovery.store import make_posting
from src.shared.http_client import CareersError, fetch_json

PAGE_SIZE = 10  # the APIs ignore a larger `num`
# What a tenant without PCSX answers on it.
PCSX_REFUSED = (403, 404)


class EightfoldSlugError(ValueError):
    """A slug is not the `host|domain` shape."""


def parse_slug(slug: str) -> tuple[str, str]:
    parts = slug.split("|")
    if len(parts) != 2 or not all(p.strip() for p in parts):
        raise EightfoldSlugError(f"expected host|domain, got {slug!r}")
    return parts[0].strip().lower(), parts[1].strip().lower()


def epoch_date(value):
    """An epoch-seconds timestamp as a date, or None."""
    if not isinstance(value, (int, float, str)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC).date()
    except (ValueError, OSError, OverflowError):
        return None


def list_page(host: str, domain: str, api: str, term: str, start: int,
              deadline_ts: float | None = None) -> tuple[list[dict], int]:
    """(positions, total) for one page of one API."""
    q = quote(term)
    if api == "pcsx":
        payload = fetch_json(f"https://{host}/api/pcsx/search?domain={domain}&query={q}"
                             f"&location=&start={start}&sort_by=relevance", deadline_ts=deadline_ts)
        payload = payload.get("data") if isinstance(payload, dict) else None
    else:
        payload = fetch_json(f"https://{host}/api/apply/v2/jobs?domain={domain}&query={q}"
                             f"&start={start}&sort_by=relevance", deadline_ts=deadline_ts)
    if not isinstance(payload, dict) or not isinstance(payload.get("positions"), list):
        raise TypeError("expected a 'positions' list in the search response")
    return [p for p in payload["positions"] if isinstance(p, dict)], int(payload.get("count") or 0)


def posting_from(item: dict, host: str, api: str, company: str, term: str) -> dict | None:
    title = item.get("name") or item.get("posting_name") or ""
    pid = item.get("id")
    if not title or pid in (None, ""):
        return None
    if api == "pcsx":
        url = f"https://{host}{item.get('positionUrl') or f'/careers/job/{pid}'}"
        posted = item.get("postedTs") or item.get("creationTs")
        jd = ""
    else:
        url = item.get("canonicalPositionUrl") or f"https://{host}/careers/job/{pid}"
        posted = item.get("t_create")
        jd = parse.html_to_text(item.get("job_description") or "")
    locations = [x for x in item.get("locations") or [] if isinstance(x, str) and x.strip()]
    return make_posting(
        source="eightfold", company=company, title=title, url=url,
        location_raw="; ".join(locations) or item.get("location") or "",
        posted_date=epoch_date(posted), jd_text=jd, found_by_term=term,
        _id=str(pid), _api=api)


class EightfoldLane(BoardLane):
    name = "eightfold"

    def list_board(self, board: Board, crawl, pause) -> list[dict]:
        host, domain = parse_slug(board.slug)
        api = "pcsx"

        def fetch_page(term, offset):
            nonlocal api
            try:
                items, _ = list_page(host, domain, api, term, offset, deadline_ts=crawl.deadline_ts)
            except CareersError as e:
                if api != "pcsx" or e.status not in PCSX_REFUSED:
                    raise
                api = "v2"
                items, _ = list_page(host, domain, api, term, offset, deadline_ts=crawl.deadline_ts)
            return items

        def to_posting(item, term):
            return posting_from(item, host, api, board.name, term)

        return self.paged_search(crawl, pause, PAGE_SIZE, fetch_page, to_posting)

    def detail(self, board: Board, posting: dict, crawl) -> dict | None:
        host, domain = parse_slug(board.slug)
        pid = posting["_id"]
        if posting["_api"] == "pcsx":
            payload = fetch_json(f"https://{host}/api/pcsx/position_details?position_id={pid}"
                                 f"&domain={domain}&hl=en", deadline_ts=crawl.deadline_ts)
            info = payload.get("data") if isinstance(payload, dict) else None
            jd = (info or {}).get("jobDescription") if isinstance(info, dict) else None
        else:
            info = fetch_json(f"https://{host}/api/apply/v2/jobs/{pid}?domain={domain}",
                              deadline_ts=crawl.deadline_ts)
            jd = info.get("job_description") if isinstance(info, dict) else None
        if not isinstance(info, dict):
            raise TypeError("expected a JSON object in the detail response")
        return {**posting, "jd_text": parse.html_to_text(jd or "")}
