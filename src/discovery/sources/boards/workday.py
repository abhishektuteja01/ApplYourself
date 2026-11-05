"""Workday: a paginated, search-scoped list with no JDs, then one detail call
per new posting that passes the title gate.

Read-only discovery; Workday roles are manual-apply. The slug is
`company|wd#|site_id`. The list endpoint filters server-side on `searchText`,
so each tenant is searched once per lane (that lane's first search term).
`total` cannot be trusted past page 0, so paging stops on a short page, a
page with nothing that passes the gate, or `max_pages`.
"""
from __future__ import annotations

import random
import re

from src.discovery import parse
from src.discovery.sources.boards.base import Board, BoardLane
from src.discovery.store import make_posting
from src.shared.http_client import fetch_json, fetch_json_post

# Workday's server-side page-size ceiling: 21 fails, 20 succeeds.
LIST_LIMIT = 20


class WorkdaySlugError(ValueError):
    """A slug is not the tri-part company|wd#|site_id shape."""


def parse_slug(slug: str) -> tuple[str, str, str]:
    parts = slug.split("|")
    if len(parts) != 3 or not all(parts):
        raise WorkdaySlugError(f"expected company|wd#|site_id, got {slug!r}")
    company, wd, site_id = parts
    if not re.fullmatch(r"wd\d+", wd, re.IGNORECASE):
        raise WorkdaySlugError(f"expected wd<N> for the pod, got {wd!r} in {slug!r}")
    return company, wd, site_id


def _base(company: str, wd: str) -> str:
    return f"https://{company}.{wd}.myworkdayjobs.com/wday/cxs/{company}"


def public_url(company: str, wd: str, site_id: str, path: str) -> str:
    return f"https://{company}.{wd}.myworkdayjobs.com/{site_id}{path}"


def list_page(company: str, wd: str, site_id: str, offset: int, search_text: str = "",
              deadline_ts: float | None = None) -> dict:
    payload = fetch_json_post(
        f"{_base(company, wd)}/{site_id}/jobs",
        {"appliedFacets": {}, "limit": LIST_LIMIT, "offset": offset, "searchText": search_text},
        deadline_ts=deadline_ts)
    if not isinstance(payload, dict):
        raise TypeError(f"expected a JSON object, got {type(payload).__name__}")
    if not isinstance(payload.get("jobPostings"), list):
        raise TypeError("expected a list under 'jobPostings'")
    return payload


class WorkdayLane(BoardLane):
    name = "workday"
    list_refreshes = False

    def list_board(self, board: Board, crawl, pause) -> list[dict]:
        company, wd, site_id = parse_slug(board.slug)

        def fetch_page(term, offset):
            payload = list_page(company, wd, site_id, offset, term, deadline_ts=crawl.deadline_ts)
            return [i for i in payload["jobPostings"] if isinstance(i, dict)]

        def to_posting(item, term):
            title, path = item.get("title") or "", item.get("externalPath") or ""
            if not title or not path:
                return None
            return make_posting(
                source="workday", company=board.name, title=title,
                url=public_url(company, wd, site_id, path),
                location_raw=item.get("locationsText") or "",
                posted_date=parse.relative_posted_date(item.get("postedOn"), crawl.today.date()),
                found_by_term=term, _path=path)

        def jittered_pause():
            pause()
            _jitter()

        return self.paged_search(crawl, jittered_pause, LIST_LIMIT, fetch_page, to_posting)

    def detail(self, board: Board, posting: dict, crawl) -> dict | None:
        company, wd, site_id = parse_slug(board.slug)
        detail = fetch_json(f"{_base(company, wd)}/{site_id}{posting['_path']}",
                            deadline_ts=crawl.deadline_ts)
        info = detail.get("jobPostingInfo") if isinstance(detail, dict) else None
        if not isinstance(info, dict):
            raise TypeError("expected a 'jobPostingInfo' object in the detail response")
        return {**posting,
                "title": info.get("title") or posting["title"],
                "jd_text": parse.html_to_text(info.get("jobDescription") or ""),
                "location_raw": info.get("location") or posting["location_raw"],
                "posted_date": (parse.relative_posted_date(info.get("postedOn"), crawl.today.date())
                                or posting["posted_date"])}


def _jitter() -> None:
    """A little unevenness on top of the base pacing: Workday sits behind
    Cloudflare, and a metronome is a bot tell."""
    import time
    time.sleep(random.uniform(0.0, 0.25))
