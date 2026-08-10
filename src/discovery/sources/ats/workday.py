"""Workday application boards: list -> title-classify -> detail fetch.

Read-only discovery, no submission (§12b). Workday roles are always
manual-apply: nothing here is imported by `src/apply/`, and `registry.py`'s
`ATS_URL_MARKERS` (which decides which URL survives dedupe, not which one
`apply_cli.py` can submit to) is the only other place this source's boards
are named outside this module.

Unlike Greenhouse/Lever/Ashby's single list call, Workday's list endpoint
returns title/location/`postedOn` only — no description — and is paginated
at `LIST_LIMIT` per page, so this does not fit `AtsBoardSource`'s
one-call-per-company shape. Title classification runs against the cheap
paginated list first; the per-job **detail** fetch, the only place a JD comes
from, only ever fires for the survivors (empirically ~3% of postings).

**Search-scoped, not exhaustive.** The list endpoint's `searchText` genuinely
filters server-side (confirmed live: NVIDIA's ~2,000 open reqs narrow to a
few dozen under `searchText="AI Engineer"`, at every offset, not just page
0) — so each tenant is crawled once per distinct `search_terms` string
across every configured vertical (`profile/verticals.yaml`, never
hardcoded — R7's company/vertical-agnostic rule extends to search terms
too), instead of paginating every posting a tenant has. This trades recall
(a role Workday's search does not surface under any configured term is never
seen, even if its title would classify) for a page count small enough to be
affordable across 50+ tenants; `classify_vertical_from_title` still runs on
every result as the authoritative filter, since search hits are not
assumed relevant.

**`total` cannot be trusted past page 0.** Confirmed live: NVIDIA's `total`
reads 2000 at `offset=0`, then 0 at every later offset checked, including
one *past* the page-0 total, while `jobPostings` keeps returning full pages
regardless. Pagination here stops only on a short page
(`len(jobPostings) < LIST_LIMIT`) or an empty one — never by comparing
`offset` against `total`.

`postedOn` is a relative string ("Posted Today", "Posted 19 Days Ago",
"Posted 30+ Days Ago") at both the list and detail level, never an absolute
timestamp — `relative_posted_date` converts it to an approximate date, or
`None` if it does not recognize the phrasing. `workday` is in
`CAREER_SOURCES`/`STALENESS_EXEMPT_SOURCES` (registry.py), so an approximate
or missing date does not cost a row its place in the window.

Slug is a tri-part pipe-joined string, `company|wd#|site_id` — one company
can run several sites (PwC 8, Salesforce 12, Boeing 19), and the wrong one is
usually a conference/contractor/internal portal, not the main board.
"""
from __future__ import annotations

import re
import time
from datetime import date, timedelta

from src.discovery import cleaning
from src.discovery import trace
from src.discovery import universe
from src.discovery.htmlutil import html_to_text
from src.discovery.schema import make_row
from src.discovery.sources.ats.base import PAYLOAD_SHAPE_ERRORS
from src.discovery.sources.ats.http import CareersError, fetch_json, fetch_json_post
from src.discovery.sources.base import Source, SourceResult

LIST_LIMIT = 50
# A ceiling per (company, search term) pair, not per company: search-scoping
# already cuts page count far below an exhaustive crawl, so 6 * LIST_LIMIT =
# 300 matches for one term at one tenant is generous headroom rather than a
# binding constraint in practice. Still there so one term that somehow
# matches broadly cannot consume a whole run's deadline; a page never reached
# is not a failure — it is read again, from page 0, next run.
MAX_PAGES_PER_TERM = 6

_POSTED_TODAY = re.compile(r"posted\s+today", re.IGNORECASE)
_POSTED_YESTERDAY = re.compile(r"posted\s+yesterday", re.IGNORECASE)
_POSTED_N_DAYS_AGO = re.compile(r"posted\s+(\d+)\+?\s+days?\s+ago", re.IGNORECASE)


class WorkdaySlugError(ValueError):
    """A universe.csv slug is not the tri-part company|wd#|site_id shape."""


def parse_slug(slug: str) -> tuple[str, str, str]:
    parts = slug.split("|")
    if len(parts) != 3 or not all(parts):
        raise WorkdaySlugError(f"expected company|wd#|site_id, got {slug!r}")
    company, wd, site_id = parts
    if not re.fullmatch(r"wd\d+", wd, re.IGNORECASE):
        raise WorkdaySlugError(f"expected wd<N> for the pod, got {wd!r} in {slug!r}")
    return company, wd, site_id


def relative_posted_date(text: str, today: date | None = None) -> date | None:
    """Workday's `postedOn` is always relative text, never a timestamp."""
    if not isinstance(text, str) or not text.strip():
        return None
    today = today or date.today()
    if _POSTED_TODAY.search(text):
        return today
    if _POSTED_YESTERDAY.search(text):
        return today - timedelta(days=1)
    match = _POSTED_N_DAYS_AGO.search(text)
    if match:
        return today - timedelta(days=int(match.group(1)))
    return None


def _base_url(company: str, wd: str) -> str:
    return f"https://{company}.{wd}.myworkdayjobs.com/wday/cxs/{company}"


def _list_url(company: str, wd: str, site_id: str) -> str:
    return f"{_base_url(company, wd)}/{site_id}/jobs"


def _detail_url(company: str, wd: str, site_id: str, path: str) -> str:
    return f"{_base_url(company, wd)}/{site_id}{path}"


def _public_url(company: str, wd: str, site_id: str, path: str) -> str:
    """The browsable careers-page URL — a reasonable fallback when the detail
    payload's own `externalUrl` is missing."""
    return f"https://{company}.{wd}.myworkdayjobs.com/{site_id}{path}"


def list_page(company: str, wd: str, site_id: str, offset: int, search_text: str = "",
              deadline_ts: float | None = None) -> dict:
    payload = fetch_json_post(
        _list_url(company, wd, site_id),
        {"appliedFacets": {}, "limit": LIST_LIMIT, "offset": offset, "searchText": search_text},
        deadline_ts=deadline_ts,
    )
    if not isinstance(payload, dict):
        raise TypeError(f"expected a JSON object, got {type(payload).__name__}")
    postings = payload.get("jobPostings")
    if not isinstance(postings, list):
        raise TypeError(f"expected a list under 'jobPostings', got {type(postings).__name__}")
    return payload


def search_terms(verticals_config) -> tuple[str, ...]:
    """Every distinct search_terms string across every configured vertical —
    the only source of a Workday search term (R7: never hardcoded here)."""
    seen: set[str] = set()
    terms: list[str] = []
    for v in verticals_config.verticals.values():
        for term in v.search_terms:
            key = term.strip().casefold()
            if key and key not in seen:
                seen.add(key)
                terms.append(term)
    return tuple(terms)


def _detail_row(company: str, name: str, wd: str, site_id: str, path: str,
                 list_item: dict, vertical: str, deadline_ts: float | None = None) -> dict | None:
    detail = fetch_json(_detail_url(company, wd, site_id, path), deadline_ts=deadline_ts)
    info = detail.get("jobPostingInfo") if isinstance(detail, dict) else None
    if not isinstance(info, dict):
        raise TypeError("expected a 'jobPostingInfo' object in the detail response")
    title = info.get("title") or list_item.get("title") or ""
    if not title:
        return None
    fallback_url = _public_url(company, wd, site_id, path)
    return make_row(
        site="workday",
        company=name,
        title=title,
        job_url=info.get("externalUrl") or fallback_url,
        description=html_to_text(info.get("jobDescription") or ""),
        location=info.get("location") or list_item.get("locationsText") or "",
        date_posted=relative_posted_date(info.get("postedOn") or list_item.get("postedOn")),
        vertical=vertical,
    )


class WorkdaySource(Source):
    name = "workday"

    def fetch(self, ctx) -> SourceResult:
        pacing = max(1.0, ctx.config.sources[self.name].pacing_seconds)
        companies = universe.load(self.name)
        terms = search_terms(ctx.verticals)

        rows: list[dict] = []
        errors: list[str] = []
        report_lines: list[str] = []
        kept = 0
        polled = 0
        ok = 0
        err_other = 0
        shape_errors = 0
        detail_attempts = 0
        detail_shape_errors = 0
        first_request = True

        ticker = trace.Ticker(self.name, len(companies), every=100)

        for c in companies:
            if ctx.deadline_reached():
                break
            try:
                company, wd, site_id = parse_slug(c.slug)
            except WorkdaySlugError as e:
                errors.append(f"{c.name}: {e}")
                continue

            polled += 1
            ticker.tick(polled, ok=ok, err=err_other)

            c_fetched = 0
            # Keyed by externalPath: the same posting can surface under more
            # than one search term, and must be detail-fetched only once.
            survivors: dict[str, tuple[str, dict]] = {}
            try:
                for term in terms:
                    offset = 0
                    for _ in range(MAX_PAGES_PER_TERM):
                        if ctx.deadline_reached():
                            break
                        if not first_request:
                            time.sleep(pacing)
                        first_request = False
                        payload = list_page(company, wd, site_id, offset, term,
                                             deadline_ts=ctx.deadline_ts)
                        postings = [p for p in payload["jobPostings"] if isinstance(p, dict)]
                        for item in postings:
                            c_fetched += 1
                            title = item.get("title") or ""
                            path = item.get("externalPath") or ""
                            if not title or not path or path in survivors:
                                continue
                            vertical = cleaning.classify_vertical_from_title(title)
                            if vertical:
                                survivors[path] = (vertical, item)
                        offset += LIST_LIMIT
                        # `total` cannot be trusted past page 0 (module
                        # docstring) — a short or empty page is the only
                        # reliable "no more results" signal.
                        if len(postings) < LIST_LIMIT:
                            break
                    if ctx.deadline_reached():
                        break
            except CareersError as e:
                err_other += 1
                errors.append(f"{c.name}: {e}")
                if e.permanent:
                    universe.update_health(self.name, c.slug, success=False)
                if c.priority or e.status != 404:
                    report_lines.append(
                        f"| {c.name} | ERROR | 0 | 0 | {str(e).replace('|', '\\|')[:80]} |")
                continue
            except PAYLOAD_SHAPE_ERRORS as e:
                err_other += 1
                shape_errors += 1
                if shape_errors > 1 and shape_errors == polled:
                    raise
                msg = f"malformed board payload: {type(e).__name__}: {e}"
                errors.append(f"{c.name}: {msg}")
                report_lines.append(f"| {c.name} | ERROR | 0 | 0 | {msg.replace('|', '\\|')[:80]} |")
                continue

            c_kept = 0
            for path, (vertical, item) in survivors.items():
                if ctx.deadline_reached():
                    break
                time.sleep(pacing)
                detail_attempts += 1
                try:
                    row = _detail_row(company, c.name, wd, site_id, path, item, vertical,
                                       deadline_ts=ctx.deadline_ts)
                except CareersError as e:
                    errors.append(f"{c.name}: {item.get('title', '')!r}: {e}")
                    continue
                except PAYLOAD_SHAPE_ERRORS as e:
                    detail_shape_errors += 1
                    # Every detail fetch failing the same way, across every
                    # tenant, points at a Workday schema change breaking the
                    # parser, not one dead posting — same reasoning as the
                    # list-stage guard just above, scoped to detail attempts
                    # since a survivor can come from any company.
                    if detail_shape_errors > 1 and detail_shape_errors == detail_attempts:
                        raise
                    errors.append(f"{c.name}: {item.get('title', '')!r}: malformed detail: {e}")
                    continue
                if row is not None:
                    rows.append(row)
                    c_kept += 1

            ok += 1
            kept += c_kept
            universe.update_health(self.name, c.slug, success=True, rows=c_fetched)
            if c.priority:
                report_lines.append(f"| {c.name} | OK | {c_fetched} | {c_kept} | |")

        ticker.finish(polled, ok=ok, err=err_other, kept=kept)

        summary = (f"Companies polled: {polled} | OK: {ok} | Err: {err_other} "
                   f"| Rows kept: {kept}")
        report_summary = [summary, ""]
        if report_lines:
            report_summary.extend([
                "| company | status | fetched | kept | error |",
                "|---|---|---|---|---|",
            ] + report_lines)

        return SourceResult(rows, report_summary, errors)
