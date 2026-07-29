import time
import pandas as pd
from jobspy import scrape_jobs
from src.discovery.sources.base import Source, SourceResult
from src.discovery.schema import make_row

# A/B test (2026-07-29): bumped from 50 for more ai_eng coverage. Revert to 50
# if run reports show LinkedIn 429s climbing.
RESULTS_WANTED = 100
HOURS_OLD = 336
DESCRIPTION_FORMAT = "markdown"

class JobSpySource(Source):
    def fetch(self, ctx) -> SourceResult:
        rows = []
        errors = []
        report_lines = []
        
        pacing = ctx.config.sources[self.name].pacing_seconds
        pacing = max(0.5, pacing)
        
        locations = ctx.config.location_allowlist.countries or ["United States"]
        
        total_queries = 0
        total_rows = 0
        
        for v in ctx.verticals.verticals.values():
            terms = v.linkedin_terms if self.name == "linkedin" else v.search_terms
            for term in terms:
                for location in locations:
                    for is_remote in (False, True):
                        if ctx.deadline_reached():
                            return SourceResult(rows, report_lines, errors)
                            
                        if total_queries > 0:
                            time.sleep(pacing)
                            
                        total_queries += 1
                        
                        try:
                            df = scrape_jobs(
                                site_name=self.name,
                                search_term=term,
                                google_search_term=None,
                                location=location,
                                is_remote=is_remote,
                                results_wanted=RESULTS_WANTED,
                                hours_old=HOURS_OLD,
                                country_indeed="usa",
                                description_format=DESCRIPTION_FORMAT,
                                linkedin_fetch_description=(self.name == "linkedin"),
                                verbose=1,
                            )
                            if df is None:
                                df = pd.DataFrame()
                        except Exception as e:
                            msg = f"{type(e).__name__}: {e}"
                            errors.append(f"{self.name} term='{term}' remote={is_remote}: {msg}")
                            df = pd.DataFrame()
                            
                        if not df.empty:
                            df = df.where(pd.notnull(df), None)
                            records = df.to_dict("records")
                            for record in records:
                                record["vertical"] = v.name
                                rows.append(make_row(**record))
                            report_lines.append(f"- term='{term}' remote={is_remote}: {len(df)} rows")
                        total_rows += len(df)
                        
        report_lines.append(f"Queries made: {total_queries}")
        return SourceResult(rows, report_lines, errors)

class LinkedinSource(JobSpySource):
    name = "linkedin"

class IndeedSource(JobSpySource):
    name = "indeed"

# A/B test (2026-07-29): new source, remove (here + config.py allowed_sources/
# defaults + orchestrator.py get_sources/fixed_order) if it errors out or
# returns zero rows across runs.
class ZipRecruiterSource(JobSpySource):
    name = "zip_recruiter"
