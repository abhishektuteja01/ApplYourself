"""The search lanes: LinkedIn and Indeed through jobspy.

Every result is title-gated the moment it arrives. A posting the store
already holds is only touched; LinkedIn's separate detail page is fetched for
new postings alone, once per posting, paced.
"""
from __future__ import annotations

import contextlib
import random
import time

import jobspy
import pandas as pd
from jobspy import scrape_jobs
from jobspy.linkedin import LinkedIn
from jobspy.model import Country, DescriptionFormat, ScraperInput, Site
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.discovery import parse
from src.discovery.config import MIN_PAGE_DELAY_SECONDS, pacing_floor
from src.discovery.sources.lane import Lane, LaneStats
from src.discovery.store import make_posting
from src.shared import title_gate

DESCRIPTION_FORMAT = "markdown"
DETAIL_PACING_SECONDS = 1.0
DETAIL_JITTER_SECONDS = 0.5


class _TallyingAdapter(HTTPAdapter):
    """Times every response, below urllib3's Retry, so a silently retried 429
    is counted as the response it was."""

    def __init__(self, tally, **kwargs) -> None:
        self._tally = tally
        super().__init__(**kwargs)

    def send(self, request, **kwargs):
        t0 = time.time()
        try:
            response = super().send(request, **kwargs)
        except Exception:
            self._tally.record(request.url, time.time() - t0, None)
            raise
        self._tally.record(request.url, time.time() - t0, response.status_code)
        return response

    def build_response(self, req, resp):
        response = super().build_response(req, resp)
        for attempt in getattr(getattr(resp, "retries", None), "history", ()) or ():
            if attempt.status is not None:
                self._tally.record(req.url, 0.0, attempt.status)
        return response


@contextlib.contextmanager
def _paced_linkedin(page_delay: float, band: float, tally):
    """jobspy's LinkedIn scraper with our page delay and tally. `scrape_jobs`
    reads the `jobspy.LinkedIn` name on every call, so rebinding it is enough;
    it is restored on exit."""

    class PacedLinkedIn(LinkedIn):
        delay = page_delay
        band_delay = band

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.session.mount("https://", _TallyingAdapter(
                tally, max_retries=self.session.adapters["https://"].max_retries))

    original = jobspy.LinkedIn
    jobspy.LinkedIn = PacedLinkedIn
    try:
        yield
    finally:
        jobspy.LinkedIn = original


def _detail_client(tally) -> LinkedIn:
    """A LinkedIn scraper for detail pages only, with a short retry budget:
    jobspy's default spends ~50s on one dead page."""
    client = LinkedIn()
    client.scraper_input = ScraperInput(site_type=[Site.LINKEDIN],
                                        description_format=DescriptionFormat(DESCRIPTION_FORMAT))
    retries = Retry(total=1, connect=1, status=1,
                    status_forcelist=[500, 502, 503, 504, 429], backoff_factor=1)
    client.session.mount("https://", _TallyingAdapter(tally, max_retries=retries))
    return client


def _country_indeed(location: str) -> str:
    """Which Indeed domain a query hits. Unknown (a continent) reads as usa."""
    try:
        Country.from_string(location)
    except ValueError:
        return "usa"
    return location


def build_queries(name: str, src, vcfg, locations) -> list[tuple]:
    """(vertical, term, location, is_remote, company_ids) for every query, in
    run order: home queries first, then the `abroad` block, then the
    `companies` block; the last two remote=False only. A company query has no
    lane of its own: its rows are classified by title."""
    remote_flags = (False, True) if src.remote_queries else (False,)
    queries = [
        (v.name, term, location, is_remote, None)
        for v in vcfg.verticals.values()
        for term in (v.linkedin_terms if name == "linkedin" else v.search_terms)
        for location in locations
        for is_remote in remote_flags
    ]
    for country in src.abroad_countries:
        for lane in src.abroad_verticals:
            v = vcfg.verticals.get(lane)
            if v is not None:
                queries.extend((lane, term, country, False, None) for term in v.abroad_terms)
    for company_id in src.company_ids.values():
        queries.extend(("", term, location, False, [company_id])
                       for term in src.company_terms for location in locations)
    return queries


def _value(record: dict, key: str) -> str:
    value = record.get(key)
    return "" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value)


def posting_from(record: dict, name: str, vertical: str, term: str, is_remote: bool) -> dict:
    job_url = _value(record, "job_url")
    job_type = record.get("job_type")
    if isinstance(job_type, list):
        job_type = ", ".join(str(getattr(t, "value", [t])[0]) for t in job_type)
    return make_posting(
        source=name, company=_value(record, "company"), title=_value(record, "title"),
        url=_value(record, "job_url_direct") or job_url, key_url=job_url,
        location_raw=_value(record, "location"),
        jd_text=_value(record, "description"),
        posted_date=record.get("date_posted"),
        remote_flag=parse.as_bool(record.get("is_remote")),
        salary_min=parse.yearly_salary(record.get("min_amount"), record.get("interval")),
        salary_max=parse.yearly_salary(record.get("max_amount"), record.get("interval")),
        salary_currency=_value(record, "currency"),
        employment_type=str(job_type or ""), seniority_raw=_value(record, "job_level"),
        vertical=vertical, found_by_term=term, found_by_remote=is_remote,
    )


class SearchLane(Lane):
    def __init__(self, name: str):
        self.name = name

    def run(self, crawl, tally=None) -> LaneStats:
        src = crawl.cfg.sources[self.name]
        pacing = max(pacing_floor(self.name), src.pacing_seconds)
        page_delay = max(MIN_PAGE_DELAY_SECONDS, src.page_delay_seconds)
        band = max(0.0, src.page_delay_band_seconds)
        locations = crawl.cfg.effective_search_locations()
        queries = build_queries(self.name, src, crawl.vcfg, locations)
        stats = LaneStats()
        fetch: dict[str, dict] = {}
        seen: set[str] = set()
        client = _detail_client(tally) if self.name == "linkedin" else None
        made = saturated = 0

        stack = contextlib.ExitStack()
        if self.name == "linkedin":
            stack.enter_context(_paced_linkedin(page_delay, band, tally))
        with stack:
            for vertical, term, location, is_remote, *company in queries:
                company_ids = company[0] if company else None
                if crawl.deadline_reached():
                    stats.truncated = True
                    break
                if made:
                    time.sleep(pacing)
                made += 1
                try:
                    df = scrape_jobs(
                        site_name=self.name, search_term=term, location=location,
                        is_remote=is_remote, results_wanted=src.results_wanted,
                        hours_old=src.hours_old, country_indeed=_country_indeed(location),
                        description_format=DESCRIPTION_FORMAT,
                        linkedin_company_ids=company_ids,
                        linkedin_fetch_description=False, verbose=1)
                except Exception as e:  # noqa: BLE001 -- one bad query must not cost the run
                    stats.errors.append(f"term={term!r} remote={is_remote}: {type(e).__name__}: {e}")
                    continue
                if df is None or df.empty:
                    continue
                new_before = stats.new
                for record in df.where(pd.notnull(df), None).to_dict("records"):
                    self._one(posting_from(record, self.name, vertical, term, is_remote),
                              crawl, stats, fetch, seen)
                if len(df) >= src.results_wanted:
                    saturated += 1
                    where = f"{location} (remote)" if is_remote else location
                    where += f" company {company_ids[0]}" if company_ids else ""
                    stats.capped.append(f"{term} @ {where}: {stats.new - new_before} new")
                # Details right after the query that found them, so a deadline
                # never leaves a night's new postings without a JD.
                if client is not None and fetch:
                    self._details(fetch, crawl, stats, client)
                    fetch.clear()
        stats.notes.append(f"queries: {made} of {len(queries)}, {saturated} hit the "
                           f"{src.results_wanted}-result cap")
        if self.name == "linkedin":
            stats.notes.append(f"page delay {page_delay:g}-{page_delay + band:g}s, "
                               f"{pacing:g}s between queries")
        return stats

    def _one(self, posting: dict, crawl, stats: LaneStats, fetch: dict, seen: set) -> None:
        stats.listed += 1
        key = posting["posting_key"]
        if not key or not posting["title"]:
            return
        if not posting["vertical"]:
            posting["vertical"] = title_gate.classify_vertical_from_title(posting["title"])
            if not posting["vertical"]:
                return
        stats.classified += 1
        passes = title_gate.title_passes(posting["title"], posting["vertical"], self.name, crawl.vcfg)
        crawl.record_listing(self.name, posting["title"], posting["vertical"], passes, key)
        if not passes:
            return
        stats.passed += 1
        if key in crawl.known:
            stats.known += 1
            crawl.batch.touch(key)
            return
        if key in seen:
            return
        seen.add(key)
        stats.new += 1
        if len(posting["jd_text"].strip()) >= parse.MIN_JD_CHARS:
            crawl.known[key] = {}
            crawl.batch.upsert(posting)
        elif self.name == "linkedin":
            fetch[key] = posting
        else:
            stats.short_jd += 1

    def _details(self, fetch: dict[str, dict], crawl, stats: LaneStats, client) -> None:
        for posting in fetch.values():
            if crawl.deadline_reached():
                stats.truncated = True
                break
            source, _, job_id = posting["posting_key"].partition("|")
            if source != "linkedin" or not job_id.isdigit():
                stats.detail_failed += 1
                continue
            time.sleep(DETAIL_PACING_SECONDS + random.uniform(0, DETAIL_JITTER_SECONDS))
            try:
                details = client._get_job_details(job_id)
            except Exception as e:  # noqa: BLE001 -- a parse failure on one page
                stats.errors.append(f"detail {job_id}: {type(e).__name__}: {e}")
                details = {}
            jd = (details or {}).get("description") or ""
            if len(jd.strip()) < parse.MIN_JD_CHARS:
                stats.detail_failed += 1
                continue
            stats.detail_ok += 1
            posting["jd_text"] = jd
            if details.get("job_level"):
                posting["seniority_raw"] = details["job_level"]
            if details.get("job_type"):
                posting["employment_type"] = ", ".join(t.value[0] for t in details["job_type"])
            if details.get("job_url_direct"):
                posting["url"] = details["job_url_direct"]
            crawl.known[posting["posting_key"]] = {}
            crawl.batch.upsert(posting)
