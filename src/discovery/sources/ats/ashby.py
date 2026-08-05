import time
from src.discovery.sources.base import Source, SourceResult
from src.discovery.sources.ats.http import fetch_json, CareersError, iso_date
from src.discovery.htmlutil import html_to_text
from src.discovery import universe
from src.discovery import cleaning
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
    for item in (payload or {}).get("jobs", []):
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

class AshbySource(Source):
    name = "ashby"
    
    def fetch(self, ctx) -> SourceResult:
        pacing = max(1.0, ctx.config.sources[self.name].pacing_seconds)
        companies = universe.load(self.name)
        
        rows = []
        errors = []
        report_lines = []
        fetched = 0
        kept = 0
        polled = 0
        ok = 0
        err_404 = 0
        err_other = 0
        
        for i, c in enumerate(companies):
            if ctx.deadline_reached():
                break
                
            if i > 0:
                time.sleep(pacing)
            
            polled += 1
            url = f"https://api.ashbyhq.com/posting-api/job-board/{c.slug}?includeCompensation=true"
            try:
                payload = fetch_json(url, deadline_ts=ctx.deadline_ts)
            except CareersError as e:
                is_404 = e.status == 404
                if is_404:
                    err_404 += 1
                else:
                    err_other += 1
                errors.append(f"{c.name}: {e}")
                if e.permanent:
                    universe.update_health(self.name, c.slug, success=False)
                if c.priority or not is_404:
                    report_lines.append(f"| {c.name} | ERROR | 0 | 0 | {str(e).replace('|', '\\|')[:80]} |")
                continue
                
            c_fetched = 0
            c_kept = 0
            
            for row in ashby_rows(payload, c.name):
                c_fetched += 1
                vertical = cleaning.classify_vertical_from_title(row["title"])
                if not vertical:
                    continue
                c_kept += 1
                row["vertical"] = vertical
                rows.append(row)
            
            ok += 1
            fetched += c_fetched
            kept += c_kept
            universe.update_health(self.name, c.slug, success=True, rows=c_fetched)
            
            if c.priority:
                report_lines.append(f"| {c.name} | OK | {c_fetched} | {c_kept} | |")
            
        summary = f"Companies polled: {polled} | OK: {ok} | 404: {err_404} | Err: {err_other} | Rows kept: {kept}"
        report_summary = [summary, ""]
        if report_lines:
            report_summary.extend([
                "| company | status | fetched | kept | error |",
                "|---|---|---|---|---|",
            ] + report_lines)
            
        return SourceResult(rows, report_summary, errors)
