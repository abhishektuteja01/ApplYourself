"""Lever: one call per board, JDs included."""

from __future__ import annotations

from src.discovery import parse
from src.discovery.sources.boards.base import Board, BoardLane
from src.discovery.store import make_posting
from src.shared.http_client import fetch_json


def api_host(region: str = "") -> str:
    return "api.eu.lever.co" if region == "eu" else "api.lever.co"


def posting_from(item: dict, company: str) -> dict | None:
    title = item.get("text") or ""
    url = item.get("hostedUrl") or ""
    if not title or not url:
        return None
    categories = item.get("categories") or {}
    salary = item.get("salaryRange") or {}
    parts = []
    intro = item.get("description")
    if isinstance(intro, str) and intro.strip():
        parts.append(intro)
    for section in item.get("lists") or []:
        if not isinstance(section, dict):
            continue
        heading = (section.get("text") or "").strip()
        content = (section.get("content") or "").strip()
        if content:
            parts.append(f"<h3>{heading}</h3>\n{content}" if heading else content)
    jd = (
        parse.html_to_text("\n\n".join(parts))
        if parts
        else parse.html_to_text(item.get("descriptionPlain"))
    )
    return make_posting(
        source="lever",
        company=company,
        title=title,
        url=url,
        jd_text=jd,
        location_raw=categories.get("location") or "",
        posted_date=parse.ms_date(item.get("createdAt")),
        remote_flag=(item.get("workplaceType") or "").strip().lower() == "remote",
        salary_min=parse.yearly_salary(salary.get("min"), salary.get("interval")),
        salary_max=parse.yearly_salary(salary.get("max"), salary.get("interval")),
        salary_currency=salary.get("currency") or "",
        employment_type=categories.get("commitment") or "",
    )


def postings_from(payload, company: str) -> list[dict]:
    if not isinstance(payload, list):
        raise TypeError(f"expected a JSON array, got {type(payload).__name__}")
    return [p for p in (posting_from(i, company) for i in payload if isinstance(i, dict)) if p]


def fetch_posting(slug: str, posting_id: str, company: str, region: str = "") -> dict | None:
    item = fetch_json(f"https://{api_host(region)}/v0/postings/{slug}/{posting_id}")
    return posting_from(item, company) if isinstance(item, dict) else None


class LeverLane(BoardLane):
    name = "lever"

    def list_board(self, board: Board, crawl, pause) -> list[dict]:
        payload = fetch_json(
            f"https://{api_host()}/v0/postings/{board.slug}?mode=json",
            deadline_ts=crawl.deadline_ts,
        )
        return postings_from(payload, board.name)
