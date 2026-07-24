import logging
import time
from src.discovery.sources.base import Source, SourceResult
from src.discovery.sources.ats.http import fetch_json, CareersError, ms_date
from src.discovery.htmlutil import html_to_text
from src.discovery import universe
from src.discovery import cleaning
from src.discovery.schema import make_row

log = logging.getLogger(__name__)

def lever_rows(payload, company: str) -> list[dict]:
    rows: list[dict] = []
    for item in payload if isinstance(payload, list) else []:
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

class LeverSource(Source):
    name = "lever"
    
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
            url = f"https://api.lever.co/v0/postings/{c.slug}?mode=json"
            try:
                payload = fetch_json(url)
            except CareersError as e:
                is_404 = "404" in str(e)
                if is_404:
                    err_404 += 1
                else:
                    err_other += 1
                errors.append(f"{c.name}: {e}")
                universe.update_health(self.name, c.slug, success=False)
                if c.priority or not is_404:
                    report_lines.append(f"| {c.name} | ERROR | 0 | 0 | {str(e).replace('|', '\\|')[:80]} |")
                continue
                
            c_fetched = 0
            c_kept = 0
            
            for row in lever_rows(payload, c.name):
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
