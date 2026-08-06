from __future__ import annotations

from src.discovery.sources.ats.base import AtsBoardSource, job_items
from src.discovery.sources.ats.http import iso_date
from src.discovery.htmlutil import html_to_text
from src.discovery.schema import make_row

def greenhouse_rows(payload, company: str) -> list[dict]:
    rows: list[dict] = []
    for item in job_items(payload, "jobs"):
        title = item.get("title") or ""
        job_url = item.get("absolute_url") or ""
        if not title or not job_url:
            continue
        rows.append(make_row(
            site="greenhouse",
            company=company,
            title=title,
            job_url=job_url,
            description=html_to_text(item.get("content")),
            location=(item.get("location") or {}).get("name") or "",
            date_posted=iso_date(item.get("first_published") or item.get("updated_at")),
        ))
    return rows

class GreenhouseSource(AtsBoardSource):
    name = "greenhouse"

    def board_url(self, slug: str) -> str:
        return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"

    def parse_rows(self, payload, company: str) -> list[dict]:
        return greenhouse_rows(payload, company)
