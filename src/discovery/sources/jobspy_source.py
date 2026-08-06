from __future__ import annotations

import time
import pandas as pd
from jobspy import scrape_jobs
from src.discovery.sources.base import Source, SourceResult
from src.discovery.schema import make_row

# 100 over 50: A/B resolved, roughly doubled rows/run with no 429s.
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
                        # Broad on purpose: jobspy raises anything from any
                        # site, and one bad query must not cost the run.
                        except Exception as e:  # noqa: BLE001
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
