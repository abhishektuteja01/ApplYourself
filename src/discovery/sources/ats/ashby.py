from src.discovery.sources.ats.base import AtsBoardSource, job_items
from src.discovery.sources.ats.http import iso_date
from src.discovery.htmlutil import html_to_text
from src.discovery.schema import make_row

def _ashby_salary(comp) -> tuple:
    if not isinstance(comp, dict):
        return None, None, ""
    for tier in comp.get("compensationTiers") or []:
        if not isinstance(tier, dict):
            continue
        for component in tier.get("components") or []:
            if isinstance(component, dict) and component.get("compensationType") == "Salary":
                return (
                    component.get("minValue"),
                    component.get("maxValue"),
                    component.get("currencyCode") or "",
                )
    return None, None, ""

def ashby_rows(payload, company: str) -> list[dict]:
    rows: list[dict] = []
    for item in job_items(payload, "jobs"):
        title = item.get("title") or ""
        job_url = item.get("jobUrl") or item.get("applyUrl") or ""
        if not title or not job_url:
            continue
            
        salary_min, salary_max, currency = _ashby_salary(item.get("compensation"))
        
        rows.append(make_row(
            site="ashby",
            company=company,
            title=title,
            job_url=job_url,
            description=html_to_text(
                item.get("descriptionHtml") or item.get("descriptionPlain")
            ),
            location=item.get("location") or "",
            date_posted=iso_date(item.get("publishedAt")),
            is_remote=item.get("isRemote") is True,
            min_amount=salary_min,
            max_amount=salary_max,
            currency=currency,
            job_type=item.get("employmentType") or "",
        ))
    return rows

class AshbySource(AtsBoardSource):
    name = "ashby"

    def board_url(self, slug: str) -> str:
        return f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"

    def parse_rows(self, payload, company: str) -> list[dict]:
        return ashby_rows(payload, company)
