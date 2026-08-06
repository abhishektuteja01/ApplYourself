from __future__ import annotations

from src.discovery.sources.ats.base import AtsBoardSource
from src.discovery.sources.ats.http import ms_date
from src.discovery.htmlutil import html_to_text
from src.discovery.schema import make_row

def lever_rows(payload, company: str) -> list[dict]:
    # Lever returns a bare array. A non-list payload is a malformed board, not
    # an empty one — raising lets base report it per company.
    if not isinstance(payload, list):
        raise TypeError(f"expected a JSON array, got {type(payload).__name__}")
    rows: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        title = item.get("text") or ""
        job_url = item.get("hostedUrl") or ""
        if not title or not job_url:
            continue

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
            if not content:
                continue
            parts.append(f"<h3>{heading}</h3>\n{content}" if heading else content)
        description = (
            html_to_text("\n\n".join(parts))
            if parts
            else html_to_text(item.get("descriptionPlain"))
        )

        rows.append(make_row(
            site="lever",
            company=company,
            title=title,
            job_url=job_url,
            description=description,
            location=categories.get("location") or "",
            date_posted=ms_date(item.get("createdAt")),
            is_remote=(item.get("workplaceType") or "").strip().lower() == "remote",
            min_amount=salary.get("min"),
            max_amount=salary.get("max"),
            currency=salary.get("currency") or "",
            job_type=categories.get("commitment") or "",
        ))
    return rows

class LeverSource(AtsBoardSource):
    name = "lever"

    def board_url(self, slug: str) -> str:
        return f"https://api.lever.co/v0/postings/{slug}?mode=json"

    def parse_rows(self, payload, company: str) -> list[dict]:
        return lever_rows(payload, company)
