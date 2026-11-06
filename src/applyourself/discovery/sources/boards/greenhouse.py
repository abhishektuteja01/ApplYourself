"""Greenhouse: list without JDs, fetch the JD only for a new or updated posting."""

from __future__ import annotations

from applyourself.discovery import parse
from applyourself.discovery.sources.boards.base import Board, BoardLane, job_items
from applyourself.discovery.store import make_posting
from applyourself.shared.http_client import fetch_json

API = "https://boards-api.greenhouse.io/v1/boards"


def posting_from(item: dict, company: str, jd: str = "") -> dict | None:
    title = item.get("title") or ""
    url = item.get("absolute_url") or ""
    if not title or not url:
        return None
    return make_posting(
        source="greenhouse",
        company=company,
        title=title,
        url=url,
        location_raw=(item.get("location") or {}).get("name") or "",
        posted_date=parse.iso_date(item.get("first_published") or item.get("updated_at")),
        updated_at=str(item.get("updated_at") or ""),
        jd_text=jd or parse.html_to_text(item.get("content")),
        _id=item.get("id"),
    )


def fetch_posting(slug: str, posting_id: str, company: str, deadline_ts=None) -> dict | None:
    item = fetch_json(f"{API}/{slug}/jobs/{posting_id}", deadline_ts=deadline_ts)
    if not isinstance(item, dict):
        raise TypeError(f"expected a JSON object, got {type(item).__name__}")
    item.setdefault("absolute_url", f"https://job-boards.greenhouse.io/{slug}/jobs/{posting_id}")
    return posting_from(item, company)


def board_name(slug: str, deadline_ts=None) -> str:
    board = fetch_json(f"{API}/{slug}", deadline_ts=deadline_ts)
    return (board or {}).get("name") or "" if isinstance(board, dict) else ""


class GreenhouseLane(BoardLane):
    name = "greenhouse"

    def list_board(self, board: Board, crawl, pause) -> list[dict]:
        payload = fetch_json(f"{API}/{board.slug}/jobs", deadline_ts=crawl.deadline_ts)
        return [p for p in (posting_from(i, board.name) for i in job_items(payload, "jobs")) if p]

    def stale(self, posting: dict, known: dict) -> bool:
        # A row with no stored updated_at is taken as current: its list row
        # fills the date in without a detail call.
        stored = known.get("updated_at", "")
        return bool(stored) and bool(posting["updated_at"]) and posting["updated_at"] != stored

    def detail(self, board: Board, posting: dict, crawl) -> dict | None:
        full = fetch_posting(board.slug, posting["_id"], board.name, crawl.deadline_ts)
        if full is None:
            return None
        # The list row's dates are the ones every other night compares with.
        return {
            **full,
            "vertical": posting["vertical"],
            "posted_date": posting["posted_date"] or full["posted_date"],
            "updated_at": posting["updated_at"] or full["updated_at"],
        }
