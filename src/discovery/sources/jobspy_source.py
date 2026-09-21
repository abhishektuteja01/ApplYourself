from __future__ import annotations

import contextlib
import random
import threading
import time
from collections import defaultdict

import jobspy
import pandas as pd
from jobspy import scrape_jobs
from jobspy.linkedin import LinkedIn
from jobspy.model import Country, DescriptionFormat, ScraperInput, Site
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.discovery.config import MIN_PAGE_DELAY_SECONDS, pacing_floor
from src.discovery.gate import gate_passing_urls
from src.discovery.sources.base import Source, SourceResult
from src.discovery.schema import make_row

# 100 over 50 gives materially more rows per query without tripping 429s.
# Whether a higher cap is safe is untested: every query now saturates at 100.
RESULTS_WANTED = 100
HOURS_OLD = 336
DESCRIPTION_FORMAT = "markdown"

# LinkedIn job descriptions live on a separate page, one GET per row. jobspy
# will fetch them inline (linkedin_fetch_description=True) but only ever per
# row, unpaced, and before anything has filtered the rows — most of which are
# duplicates across search terms or fail the title gate. We fetch them
# ourselves after the term loop instead: once per unique job_url that will
# actually survive cleaning, paced.
DETAIL_PACING_SECONDS = 1.0
DETAIL_JITTER_SECONDS = 0.5

# The schema fields the detail page owns. Everything else on a row comes from
# the search card and is unaffected by the deferral.
DETAIL_FIELDS = ("description", "job_level", "job_type", "job_url_direct")


class HttpTally:
    """Per-request counters for one LinkedIn phase.

    jobspy's session mounts `Retry(total=3, backoff_factor=5,
    status_forcelist=[..., 429])`, which retries a rate-limit silently and
    sleeps 5s, 10s, 20s while doing it. A throttled night therefore looks
    identical to a slow one in the report — the only visible symptom is the
    lane taking three times as long for no stated reason. These counters make
    the difference legible: requests made, seconds actually spent in HTTP, and
    how many of those responses were 429s.

    Thread-safe because the lanes run concurrently and a future caller may
    share one tally across workers.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests = 0
        self.seconds = 0.0
        self.rate_limited = 0
        self.errors = 0

    def record(self, seconds: float, status: int | None) -> None:
        with self._lock:
            self.requests += 1
            self.seconds += seconds
            if status is None:
                self.errors += 1
            elif status == 429:
                self.rate_limited += 1

    def report_lines(self, phase: str, sleep_seconds: float) -> list[str]:
        """The tally as report prose. `sleep_seconds` is the phase's deliberate
        pacing, passed in rather than measured so the split between 'waiting on
        purpose' and 'waiting on LinkedIn' is explicit."""
        if not self.requests:
            return []
        lines = [
            f"- {phase} http: {self.requests} requests, {self.seconds:.1f}s "
            f"({self.seconds / self.requests:.2f}s/req), "
            f"{sleep_seconds:.1f}s deliberate sleep",
        ]
        if self.rate_limited:
            # Loud: this is the number that decides whether the page delay
            # goes down further or back up.
            lines.append(
                f"- **RATE LIMITED** {phase}: {self.rate_limited} of "
                f"{self.requests} responses were 429. jobspy retried them "
                f"silently (backoff 5/10/20s); the page delay is too low.")
        if self.errors:
            lines.append(f"- {phase} transport errors: {self.errors}")
        return lines


class _TallyingAdapter(HTTPAdapter):
    """An HTTPAdapter that times every response and hands it to a tally.

    Sits at the adapter layer, below urllib3's Retry, so a silently-retried
    429 is counted as the response it was rather than vanishing into the
    eventual 200.
    """

    def __init__(self, tally: HttpTally, **kwargs) -> None:
        self._tally = tally
        super().__init__(**kwargs)

    def send(self, request, **kwargs):
        t0 = time.time()
        try:
            response = super().send(request, **kwargs)
        except Exception:
            self._tally.record(time.time() - t0, None)
            raise
        self._tally.record(time.time() - t0, response.status_code)
        return response

    def build_response(self, req, resp):
        # urllib3 swallows retried responses inside one `send`, so `send` only
        # ever sees the last one. The retry history is the rest of the story.
        response = super().build_response(req, resp)
        history = getattr(getattr(resp, "retries", None), "history", ()) or ()
        for attempt in history:
            if attempt.status is not None:
                self._tally.record(0.0, attempt.status)
        return response


def paced_linkedin(page_delay: float, band: float, tally: HttpTally):
    """A `LinkedIn` subclass with a configured inter-page delay and a tallying
    adapter, plus the class-name patch that makes `scrape_jobs` pick it up.

    `scrape_jobs` builds its `SCRAPER_MAPPING` from the `jobspy.LinkedIn`
    module global on every call, so rebinding that one name is enough. Scoped
    to a context manager and restored on exit: the other lanes run on their
    own threads at the same time, and they resolve different names.
    """

    class PacedLinkedIn(LinkedIn):
        delay = page_delay
        band_delay = band

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.session.mount("https://", _TallyingAdapter(
                tally, max_retries=self.session.adapters["https://"].max_retries))

    @contextlib.contextmanager
    def patched():
        original = jobspy.LinkedIn
        jobspy.LinkedIn = PacedLinkedIn
        try:
            yield PacedLinkedIn
        finally:
            jobspy.LinkedIn = original

    return patched()


def _linkedin_detail_client(tally: HttpTally | None = None) -> LinkedIn:
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
    retries = Retry(
        total=1, connect=1, status=1,
        status_forcelist=[500, 502, 503, 504, 429], backoff_factor=1,
    )
    client.session.mount("https://", HTTPAdapter(max_retries=retries)
                         if tally is None
                         else _TallyingAdapter(tally, max_retries=retries))
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


def _country_indeed_value(location: str) -> str:
    """jobspy's `country_indeed` picks which Indeed domain (indeed.com vs.
    indeed.de, etc.) a query hits -- it's independent of `location=`, so a
    hardcoded "usa" silently scoped every non-US query to the US site once
    the allowlist stopped being US-only. Falls back to "usa" for anything
    jobspy's own Country enum doesn't recognize (e.g. a continent name)."""
    try:
        Country.from_string(location)
    except ValueError:
        return "usa"
    return location


class JobSpySource(Source):
    def fetch(self, ctx) -> SourceResult:
        rows = []
        errors = []
        report_lines = []

        source_cfg = ctx.config.sources[self.name]
        pacing = max(pacing_floor(self.name), source_cfg.pacing_seconds)
        page_delay = max(MIN_PAGE_DELAY_SECONDS, source_cfg.page_delay_seconds)
        page_band = max(0.0, source_cfg.page_delay_band_seconds)
        search_tally = HttpTally()
        query_sleep = 0.0

        # Where we search, which is not the same as what cleaning accepts:
        # `search_locations` when configured, else the allowlist's effective
        # countries. Sorted for deterministic query order.
        locations = ctx.config.effective_search_locations()

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
        saturated_queries = 0
        deadline_hit = False

        # Scoped for the whole term loop rather than per query: `scrape_jobs`
        # re-reads the name every call, so entering once covers all of them.
        # ExitStack rather than a `with` block so the loop below keeps its
        # indentation and stays diff-legible.
        stack = contextlib.ExitStack()
        if self.name == "linkedin":
            stack.enter_context(paced_linkedin(page_delay, page_band, search_tally))

        with stack:
            for vertical, term, location, is_remote in queries:
                if ctx.deadline_reached():
                    deadline_hit = True
                    break

                if total_queries > 0:
                    time.sleep(pacing)
                    query_sleep += pacing

                total_queries += 1

                try:
                    df = scrape_jobs(
                        site_name=self.name,
                        search_term=term,
                        location=location,
                        is_remote=is_remote,
                        results_wanted=RESULTS_WANTED,
                        hours_old=HOURS_OLD,
                        country_indeed=_country_indeed_value(location),
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

                if not df.empty:
                    df = df.where(pd.notnull(df), None)
                    records = df.to_dict("records")
                    for record in records:
                        record["vertical"] = vertical
                        record["found_by_term"] = term
                        # The QUERY's remote flag, not JobSpy's per-row `is_remote`:
                        # this records which query surfaced the row.
                        record["found_by_remote"] = is_remote
                        rows.append(make_row(**record))
                    # A query returning the full RESULTS_WANTED was truncated by
                    # the cap, not exhausted: there are more rows we never saw.
                    saturated = len(df) >= RESULTS_WANTED
                    saturated_queries += saturated
                    suffix = " (SATURATED — more results exist)" if saturated else ""
                    report_lines.append(
                        f"- term='{term}' remote={is_remote}: {len(df)} rows{suffix}")

        if not deadline_hit:
            report_lines.append(f"Queries made: {total_queries}")
        if total_queries:
            report_lines.append(
                f"Saturated queries: {saturated_queries} of {total_queries}")

        if self.name == "linkedin":
            # Stated every night, tuned or not: the night's timings are only
            # interpretable against the pacing that produced them.
            report_lines.append(
                f"Page delay: {page_delay:g}-{page_delay + page_band:g}s "
                f"between pages, {pacing:g}s between queries")
            report_lines.extend(search_tally.report_lines("search", query_sleep))

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
        wanted = gate_passing_urls(rows, ctx, self.name)
        by_url: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            if row["job_url"] in wanted:
                by_url[row["job_url"]].append(row)

        tally = HttpTally()
        client = _linkedin_detail_client(tally)
        filled = empty = unparsed = 0
        elapsed = 0.0
        detail_sleep = 0.0
        stopped = False

        for i, (url, group) in enumerate(by_url.items()):
            if ctx.deadline_reached():
                stopped = True
                break

            job_id = _linkedin_job_id(url)
            if job_id is None:
                unparsed += 1
                continue

            if i > 0:
                nap = DETAIL_PACING_SECONDS + random.uniform(0, DETAIL_JITTER_SECONDS)
                time.sleep(nap)
                detail_sleep += nap

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

        attempted = filled + empty

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
        lines.extend(tally.report_lines("detail", detail_sleep))
        if stopped:
            lines.append("**DEADLINE REACHED** during detail fetch — "
                         "remaining rows keep search-card fields only")
        return lines


class LinkedinSource(JobSpySource):
    name = "linkedin"

class IndeedSource(JobSpySource):
    name = "indeed"
