from __future__ import annotations

import random
import time
from collections import defaultdict

import pandas as pd
from jobspy import scrape_jobs
from jobspy.linkedin import LinkedIn
from jobspy.model import DescriptionFormat, ScraperInput, Site
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.discovery import trace
from src.discovery.cleaning import apply_title_exclusion
from src.discovery.sources.base import Source, SourceResult
from src.discovery.schema import make_row

# 100 over 50: A/B resolved, roughly doubled rows/run with no 429s.
RESULTS_WANTED = 100
HOURS_OLD = 336
DESCRIPTION_FORMAT = "markdown"

# LinkedIn job descriptions live on a separate page, one GET per row. jobspy
# will fetch them inline (linkedin_fetch_description=True) but only ever per
# row, unpaced, before anything has filtered the rows — ~2.7x duplicate URLs
# across search terms and ~half of the unique ones failing the title gate. We
# fetch them ourselves after the term loop instead: once per unique job_url
# that will actually survive cleaning, paced.
DETAIL_PACING_SECONDS = 1.0
DETAIL_JITTER_SECONDS = 0.5

# The schema fields the detail page owns. Everything else on a row comes from
# the search card and is unaffected by the deferral.
DETAIL_FIELDS = ("description", "job_level", "job_type", "job_url_direct")


def _linkedin_detail_client() -> LinkedIn:
    """A LinkedIn scraper wired for detail fetches only.

    Reusing the instance inherits jobspy's session: browser-shaped headers,
    per-request cookie clearing, retry adapter. The adapter is re-mounted with
    a shorter budget — jobspy's default (3 attempts, backoff_factor=5) spends
    ~50s on one dead page, the worst possible behaviour under a tarpit.
    """
    client = LinkedIn()
    client.scraper_input = ScraperInput(
        site_type=[Site.LINKEDIN],
        description_format=DescriptionFormat(DESCRIPTION_FORMAT),
    )
    client.session.mount("https://", HTTPAdapter(max_retries=Retry(
        total=1, connect=1, status=1,
        status_forcelist=[500, 502, 503, 504, 429], backoff_factor=1,
    )))
    return client


def _linkedin_job_id(job_url: str | None) -> str | None:
    """The numeric id `_get_job_details` takes, or None if the URL is not one.

    jobspy emits `https://www.linkedin.com/jobs/view/<id>`, but a raw search
    href carries a slug (`.../view/<title>-at-<company>-<id>`), so try the
    trailing path segment first and the slug tail second.
    """
    tail = str(job_url or "").split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    if not tail.isdigit():
        tail = tail.rsplit("-", 1)[-1]
    return tail if tail.isdigit() else None


def _detail_values(details: dict) -> dict:
    """The DETAIL_FIELDS present in a `_get_job_details` return, coerced to the
    schema's types. Absent/empty keys are omitted so a partial page never
    blanks a field the search card already filled."""
    values = {}
    for field in ("description", "job_level", "job_url_direct"):
        if details.get(field):
            values[field] = details[field]
    # Arrives as a list of JobType enums; scrape_jobs' own frame builder
    # flattens it exactly this way.
    if details.get("job_type"):
        values["job_type"] = ", ".join(t.value[0] for t in details["job_type"])
    return values


class JobSpySource(Source):
    def fetch(self, ctx) -> SourceResult:
        rows = []
        errors = []
        report_lines = []

        pacing = ctx.config.sources[self.name].pacing_seconds
        pacing = max(0.5, pacing)

        locations = ctx.config.location_allowlist.countries or ["United States"]

        # Materialized up front only so the deadline can break one loop rather
        # than four. Iteration order is unchanged: vertical, term, location,
        # remote flag — and so, therefore, is query priority.
        queries = [
            (v.name, term, location, is_remote)
            for v in ctx.verticals.verticals.values()
            for term in (v.linkedin_terms if self.name == "linkedin" else v.search_terms)
            for location in locations
            for is_remote in (False, True)
        ]

        total_queries = 0
        deadline_hit = False

        for vertical, term, location, is_remote in queries:
            if ctx.deadline_reached():
                deadline_hit = True
                break

            if total_queries > 0:
                time.sleep(pacing)

            total_queries += 1
            q_t0 = time.time()

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
                    # Deferred to _backfill_descriptions below; see the module
                    # comment on DETAIL_PACING_SECONDS.
                    linkedin_fetch_description=False,
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

            trace.trace(f"{self.name} q{total_queries}/{len(queries)} "
                        f"{vertical} '{term}' remote={is_remote} "
                        f"rows={len(df)} {time.time() - q_t0:.1f}s")

            if not df.empty:
                df = df.where(pd.notnull(df), None)
                records = df.to_dict("records")
                for record in records:
                    record["vertical"] = vertical
                    rows.append(make_row(**record))
                report_lines.append(f"- term='{term}' remote={is_remote}: {len(df)} rows")

        if not deadline_hit:
            report_lines.append(f"Queries made: {total_queries}")

        if self.name == "linkedin" and rows:
            report_lines.extend(self._backfill_descriptions(rows, ctx, errors))

        return SourceResult(rows, report_lines, errors)

    def _backfill_descriptions(self, rows, ctx, errors) -> list[str]:
        """Fill DETAIL_FIELDS on every buffered row, one HTTP fetch per unique
        gate-passing job_url.

        Rows are neither dropped nor deduped here — that stays cleaning's job.
        A URL found under two verticals keeps both rows and both get the same
        backfill, so what lands in the shard is row-for-row what an inline
        fetch would have produced."""
        wanted = _gate_passing_urls(rows, ctx, self.name)
        by_url: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            if row["job_url"] in wanted:
                by_url[row["job_url"]].append(row)

        client = _linkedin_detail_client()
        filled = empty = unparsed = 0
        elapsed = 0.0
        stopped = False
        ticker = trace.Ticker(f"{self.name} detail", len(by_url), every=100)

        for i, (url, group) in enumerate(by_url.items()):
            if ctx.deadline_reached():
                stopped = True
                break

            job_id = _linkedin_job_id(url)
            if job_id is None:
                unparsed += 1
                continue

            if i > 0:
                time.sleep(DETAIL_PACING_SECONDS
                           + random.uniform(0, DETAIL_JITTER_SECONDS))

            t0 = time.time()
            try:
                details = client._get_job_details(job_id)
            # _get_job_details swallows its own request errors and returns {},
            # so anything reaching here is a parse failure on one page.
            except Exception as e:  # noqa: BLE001
                errors.append(f"{self.name} detail {job_id}: {type(e).__name__}: {e}")
                details = {}
            elapsed += time.time() - t0

            values = _detail_values(details)
            if not values:
                # Silent by design upstream: the bare except returns {}. Count
                # it, so a jobspy break reads as "N fetched, N empty" in the
                # report instead of an unexplained row drop during cleaning.
                empty += 1
                continue

            filled += 1
            for row in group:
                row.update(values)

            ticker.tick(filled + empty, filled=filled, empty=empty,
                        fetch=f"{elapsed / max(1, filled + empty):.2f}s")

        attempted = filled + empty
        ticker.finish(attempted, filled=filled, empty=empty, unparsed=unparsed,
                      fetch=f"{elapsed / max(1, attempted):.2f}s")

        unique_urls = len({row["job_url"] for row in rows if row["job_url"]})
        lines = [
            f"Detail fetches: {attempted} attempted ({filled} filled, {empty} empty) "
            f"for {len(by_url)} gate-passing of {unique_urls} unique urls "
            f"across {len(rows)} rows",
        ]
        if unparsed:
            lines.append(f"- {unparsed} urls had no parseable job id")
        if attempted:
            lines.append(f"- detail time: {elapsed:.1f}s total, "
                         f"{elapsed / attempted:.2f}s/page")
        if stopped:
            lines.append("**DEADLINE REACHED** during detail fetch — "
                         "remaining rows keep search-card fields only")
        return lines


def _gate_passing_urls(rows, ctx, source_name: str) -> set[str]:
    """job_urls whose row passes the per-vertical title gate.

    The gate is cleaning's own `apply_title_exclusion`, which runs at cleaning
    step 0 — before the <200-char check — so a URL skipped here belongs to a
    row cleaning was going to drop on its title regardless of description.
    Regex over titles, no LLM: R7 holds."""
    frame = pd.DataFrame([
        {"title": row["title"], "vertical": row["vertical"],
         "job_url": row["job_url"], "source": source_name}
        for row in rows
    ])
    kept, _ = apply_title_exclusion(frame, ctx.verticals)
    return {url for url in kept["job_url"] if url}


class LinkedinSource(JobSpySource):
    name = "linkedin"

class IndeedSource(JobSpySource):
    name = "indeed"
