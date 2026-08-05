import time
import pandas as pd
from jobspy import scrape_jobs
from src.discovery.sources.base import Source, SourceResult
from src.discovery.schema import make_row

# Bumped 50 -> 100 for coverage. A/B resolved: keep 100. Five consecutive runs
# roughly doubled rows/run on both LinkedIn and Indeed with zero 429s in any
# run report. Do not revert.
RESULTS_WANTED = 100
HOURS_OLD = 336
DESCRIPTION_FORMAT = "markdown"

class JobSpySource(Source):
    # Only Google Jobs needs a natural-language query; every other site keys
    # off search_term alone and must be passed None.
    def google_search_term(self, term: str, location: str, is_remote: bool) -> str | None:
        return None

    def fetch(self, ctx) -> SourceResult:
        rows = []
        errors = []
        report_lines = []
        
        pacing = ctx.config.sources[self.name].pacing_seconds
        pacing = max(0.5, pacing)
        
        locations = ctx.config.location_allowlist.countries or ["United States"]
        
        total_queries = 0

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
                                google_search_term=self.google_search_term(term, location, is_remote),
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

# Aggregator across boards; no 429 pressure like LinkedIn, so it would carry the
# search-term adjacency tail that linkedin_terms omits. Recency is left to the
# cleaning-stage staleness drop rather than a Google "since ..." qualifier,
# whose granularity (week/month) doesn't match HOURS_OLD.
# DISABLED in profile/discovery.yaml (2026-08-04): jobspy 1.1.82 returns 0 rows
# for every query shape tried ("initial cursor not found"). Kept wired so
# re-enabling is a one-line config flip when upstream fixes Google Jobs.
class GoogleSource(JobSpySource):
    name = "google"

    def google_search_term(self, term: str, location: str, is_remote: bool) -> str:
        where = "remote" if is_remote else f"in {location}"
        return f"{term} jobs {where}"
