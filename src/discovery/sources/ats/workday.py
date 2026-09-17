"""Workday application boards: list -> title-classify -> detail fetch.

Read-only discovery, no submission (§12b). Workday roles are always
manual-apply: nothing here is imported by `src/apply/`, and `registry.py`'s
workday entry (`submittable=False`, no driver) is the only other place this
source's boards are named outside this module. Its host suffix is still in
the shared table, because that decides which URL survives dedupe — a human
needs the real board URL, not an aggregator repost.

Unlike Greenhouse/Lever/Ashby's single list call, Workday's list endpoint
returns title/location/`postedOn` only — no description — and is paginated
at `LIST_LIMIT` per page, so this does not fit `AtsBoardSource`'s
one-call-per-company shape. Title classification runs against the cheap
paginated list first; the per-job **detail** fetch, the only place a JD comes
from, only ever fires for the survivors (empirically ~3% of postings).

**Search-scoped, not exhaustive.** The list endpoint's `searchText` genuinely
filters server-side (confirmed live: NVIDIA's ~2,000 open reqs narrow to a
few dozen under `searchText="AI Engineer"`, at every offset, not just page
0) — so each tenant is crawled once per `search_terms` string, instead of
paginating every posting a tenant has. This trades recall (a role Workday's
search does not surface under any configured term is never seen, even if its
title would classify) for a page count small enough to be affordable across
90+ tenants; `classify_vertical_from_title` plus cleaning's title gate
(`gate.passing_titles`) still run on every result as the authoritative filter,
since search hits are not assumed relevant.

`search_terms()` scopes to the default vertical's first configured term only
(sourced from `profile/verticals.yaml`, never hardcoded — R7's
company/vertical-agnostic rule extends to search terms too), not the union
across every vertical. One term per tenant costs one page in the common case
(a tenant with a handful of matches never fills `LIST_LIMIT`, so pagination
stops at page 0). Workday therefore surfaces default-vertical roles only; the
other configured verticals stay covered by LinkedIn/Indeed and the other
board sources.

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
`CAREER_SOURCES`/`STALENESS_EXEMPT_SOURCES` (cleaning.py), so an approximate
or missing date does not cost a row its place in the window.

Slug is a tri-part pipe-joined string, `company|wd#|site_id` — one company
can run several sites (PwC 8, Salesforce 12, Boeing 19), and the wrong one is
usually a conference/contractor/internal portal, not the main board.
"""
from __future__ import annotations

import random
import re
import time
from datetime import date, timedelta

from src.discovery import cleaning
from src.discovery import gate
from src.discovery import universe
from src.discovery.crawl_cursor import load_cursor, save_cursor
from src.discovery.htmlutil import html_to_text
from src.discovery.schema import make_row
from src.discovery.sources.ats.base import PAYLOAD_SHAPE_ERRORS
from src.ats_http import CareersError, fetch_json, fetch_json_post
from src.discovery.sources.base import Source, SourceResult

#
# The `Search/jobs` endpoint 400s on any `limit` above 20 — 21 fails, 20
# succeeds. This is Workday's server-side page-size ceiling, not a per-tenant
# quirk, so it is not configurable like `pacing_seconds` is.
LIST_LIMIT = 20
# A ceiling per (company, search term) pair, not per company, so one broad
# term cannot consume a whole run's deadline. `crawl_cursor` persists where
# each pair stopped, so pages past the cap are reached on a later run.
MAX_PAGES_PER_TERM = 6
# Where the deep frontier wraps back to the head. The list endpoint never
# reports exhaustion past page 0, so a pair whose searchText-scoped result set
# is a few dozen postings still reads full pages forever: two tenants had
# walked to offsets 3,220 and 3,420. 1,000 is ~50 pages, far past any observed
# scoped result set, and bounds one pair's full cycle to ten runs.
MAX_FRONTIER_OFFSET = 1000
# How many tenants the run report's per-tenant table lists, busiest first.
# Report presentation only — it changes nothing about what is crawled or kept,
# so it is a constant here rather than a config key.
REPORT_TOP_TENANTS = 20

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


def _relaxed_sleep(pacing: float) -> None:
    """Sleep a random multiple of `pacing`, skewed slower rather than metronomic.

    Workday sits behind Cloudflare (confirmed live: `cf-ray`/`__cf_bm` on every
    response). A fixed-interval pace is a bot tell; this source also has slack
    against the deadline the audit worried about, since `LIST_LIMIT=20` only
    triples the request count, not the wall-clock budget's order of magnitude.
    Occasional longer pause mimics a human reading a results page.
    """
    time.sleep(random.uniform(pacing, pacing * 3))
    if random.random() < 0.05:
        time.sleep(random.uniform(5.0, 15.0))


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
    """The default vertical's first configured search term — the only source
    of a Workday search term (R7: never hardcoded here). Scoped to one term
    (not the union across every vertical) so tenant x term stays affordable;
    see the module docstring for why."""
    default = verticals_config.verticals[verticals_config.default_vertical]
    if not default.search_terms:
        return ()
    return (default.search_terms[0],)


def _detail_row(company: str, name: str, wd: str, site_id: str, path: str,
                 list_item: dict, vertical: str, term: str = "",
                 deadline_ts: float | None = None) -> dict | None:
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
        found_by_term=term,
    )


# A systemic parse failure means the API shape changed and the whole source is
# broken; a couple of bad postings mean two bad postings. The old check was
# `errors > 1 and errors == attempts` — order-dependent, not rate-based: two
# malformed payloads at the *head* of a run aborted the entire lane, while 49
# failures out of 50 stayed silent once any one attempt had succeeded.
ESCALATE_MIN_ATTEMPTS = 10
ESCALATE_FAILURE_RATE = 0.9


def _escalate_if_systemic(errors: int, attempts: int, what: str) -> None:
    if attempts < ESCALATE_MIN_ATTEMPTS:
        return
    if errors / attempts < ESCALATE_FAILURE_RATE:
        return
    raise RuntimeError(
        f"workday {what}: {errors}/{attempts} payloads unparseable "
        f"(>= {ESCALATE_FAILURE_RATE:.0%}) — the API shape has almost certainly "
        f"changed. Failing the source rather than reporting an empty crawl."
    )


class WorkdaySource(Source):
    name = "workday"

    def fetch(self, ctx) -> SourceResult:
        pacing = max(1.0, ctx.config.sources[self.name].pacing_seconds)
        companies = universe.load(self.name)
        ledger = universe.HealthLedger(self.name, (c.slug for c in companies))
        terms = search_terms(ctx.verticals)

        rows: list[dict] = []
        errors: list[str] = []
        error_lines: list[str] = []
        # (priority, kept, fetched, name) per polled tenant; the table shows
        # every watchlist tenant, then the busiest of the rest.
        ok_tenants: list[tuple[bool, int, int, str]] = []
        kept = 0
        polled = 0
        ok = 0
        err_other = 0
        shape_errors = 0
        list_attempts = 0
        # List-page yield, split three ways: a path never seen before that
        # classified into a vertical, one already returned by an earlier page
        # or term, and a new path no vertical claimed.
        new_matched_paths = 0
        repeat_paths = 0
        new_unmatched_paths = 0
        detail_attempts = 0
        detail_shape_errors = 0
        first_request = True

        # Resume where the last run stopped, so a deadline cut does not drop
        # the same alphabetical tail every run. Dormant at one search term:
        # every tenant completes, so the rotation starts at the same slug each
        # run. Only the deep-page frontier (`offsets`) is live — see
        # `crawl_cursor`'s docstring.
        cursor = load_cursor(self.name)
        # Priority tenants stay at a fixed head so they are crawled every run;
        # only the tail rotates. No workday entries in `profile/companies.yaml`,
        # so `head` is empty and the split is inert today.
        companies = list(companies)
        head = [c for c in companies if c.priority]
        tail = cursor.rotate([c for c in companies if not c.priority],
                              key=lambda c: c.slug)
        companies = head + tail
        completed = 0

        for c in companies:
            if ctx.deadline_reached():
                # A truncated run must not lose the health it learned.
                ledger.flush()
                break
            try:
                company, wd, site_id = parse_slug(c.slug)
            except WorkdaySlugError as e:
                errors.append(f"{c.name}: {e}")
                # Counted: this tenant was reached and dealt with. Skipping the
                # increment under-advances the cursor, so the next run
                # re-crawls a tenant it already covered.
                completed += 1
                continue

            polled += 1

            c_fetched = 0
            # Keyed by externalPath: the same posting can surface under more
            # than one search term, and must be detail-fetched only once — the
            # first term that finds it is the one recorded as `found_by_term`.
            survivors: dict[str, tuple[str, dict, str]] = {}
            # Every path this tenant's list pages have returned, matched or
            # not, so a repeat is a repeat whether or not it classified.
            seen_paths: set[str] = set()
            fatal: CareersError | None = None
            # Per TERM, not per tenant. One transient 503 on term 3 of 4 used
            # to discard every survivor terms 1-2 had already found and skip
            # the rest, so a tenant's whole yield hung on its flakiest request.
            for term in terms:
                if ctx.deadline_reached():
                    break
                # Page 0 EVERY run, then continue from the deep frontier.
                #
                # Resuming straight at the saved offset looks right and is
                # backwards for a discovery crawl: the list endpoint takes no
                # sort parameter, so Workday's default ordering puts the
                # newest postings at offset 0. A pair deep in its pagination
                # would then go several runs without ever re-reading the head
                # — blind to exactly the new postings this source exists to
                # find, and only on the busy pairs the cap was written for.
                # Head first, frontier second: freshness every run, depth
                # still advancing.
                frontier = cursor.offset_for(c.slug, term) or LIST_LIMIT
                if frontier > MAX_FRONTIER_OFFSET:
                    frontier = LIST_LIMIT
                offsets = [0] + [frontier + k * LIST_LIMIT
                                  for k in range(MAX_PAGES_PER_TERM - 1)]
                # Seeded with the frontier this run inherited, so a run that
                # reads page 0 and then stops leaves it exactly where it was.
                # Starting at 0 walked it BACKWARDS: page 0 coming back full
                # set it to `0 + LIST_LIMIT`, so a deadline cut right after the
                # head rewrote a frontier of 200 as 50. Invisible on fresh
                # pairs, where `frontier` defaults to LIST_LIMIT and the two
                # coincide — and wrong on exactly the deep pairs the cursor
                # exists for.
                next_frontier = frontier
                pages_read = 0
                # Per (company, term) — `shape_errors` is counted per term, so
                # counting attempts per company puts the escalation ratio above
                # 1.0 as soon as there is more than one term.
                list_attempts += 1
                try:
                    for offset in offsets:
                        if ctx.deadline_reached():
                            break
                        if not first_request:
                            _relaxed_sleep(pacing)
                        first_request = False
                        payload = list_page(company, wd, site_id, offset, term,
                                             deadline_ts=ctx.deadline_ts)
                        pages_read += 1
                        postings = [p for p in payload["jobPostings"] if isinstance(p, dict)]
                        for item in postings:
                            c_fetched += 1
                            title = item.get("title") or ""
                            path = item.get("externalPath") or ""
                            if not title or not path:
                                continue
                            if path in seen_paths:
                                repeat_paths += 1
                                continue
                            seen_paths.add(path)
                            vertical = cleaning.classify_vertical_from_title(title)
                            if vertical:
                                new_matched_paths += 1
                                survivors[path] = (vertical, item, term)
                            else:
                                new_unmatched_paths += 1
                        # `total` cannot be trusted past page 0 (module
                        # docstring) — a short or empty page is the only
                        # reliable "no more results" signal.
                        if len(postings) < LIST_LIMIT:
                            next_frontier = 0   # exhausted; restart at the head
                            break
                        if offset:
                            # Page 0 is the freshness read, not the frontier
                            # read. Only a deep page may advance it.
                            next_frontier = offset + LIST_LIMIT
                    # Where the next run resumes its DEEP reading. Page 0 is
                    # unconditional, so a 0 here only means "start over",
                    # never "skip the head".
                    #
                    # Only when a page was actually read. A deadline cut on the
                    # first offset leaves `next_frontier` at its initial 0,
                    # which would erase a frontier this run never looked at —
                    # sending a deep pair back to the head on every truncated
                    # run, the exact permanent blind spot the cursor exists to
                    # remove.
                    if next_frontier > MAX_FRONTIER_OFFSET:
                        next_frontier = 0
                    if pages_read:
                        cursor.set_offset(c.slug, term, next_frontier)
                except CareersError as e:
                    err_other += 1
                    errors.append(f"{c.name} [{term}]: {e}")
                    if e.permanent:
                        fatal = e
                        break
                except PAYLOAD_SHAPE_ERRORS as e:
                    err_other += 1
                    shape_errors += 1
                    errors.append(
                        f"{c.name} [{term}]: malformed board payload: "
                        f"{type(e).__name__}: {e}")
                    break

            if fatal is not None:
                ledger.mark_dead(c.slug)
                if c.priority or fatal.status != 404:
                    error_lines.append(
                        f"| {c.name} | ERROR | 0 | 0 | "
                        f"{str(fatal).replace('|', '\\|')[:80]} |")
                completed += 1
                continue
            _escalate_if_systemic(shape_errors, list_attempts, "list")

            # Cleaning's title gate, run before the paced detail fetch rather
            # than hours later: a survivor it drops is one whose detail fetch
            # would have been thrown away.
            verdicts = gate.passing_titles(
                [(item.get("title") or "", vertical)
                 for vertical, item, _ in survivors.values()],
                ctx.verticals, self.name)
            survivors = {path: v for (path, v), ok
                         in zip(list(survivors.items()), verdicts) if ok}

            c_kept = 0
            for path, (vertical, item, term) in survivors.items():
                if ctx.deadline_reached():
                    break
                _relaxed_sleep(pacing)
                detail_attempts += 1
                try:
                    row = _detail_row(company, c.name, wd, site_id, path, item, vertical,
                                       term, deadline_ts=ctx.deadline_ts)
                except CareersError as e:
                    errors.append(f"{c.name}: {item.get('title', '')!r}: {e}")
                    continue
                except PAYLOAD_SHAPE_ERRORS as e:
                    detail_shape_errors += 1
                    # Every detail fetch failing the same way, across every
                    # tenant, points at a Workday schema change breaking the
                    # parser, not one dead posting — same reasoning as the
                    # list-stage guard, scoped to detail attempts since a
                    # survivor can come from any company.
                    _escalate_if_systemic(detail_shape_errors, detail_attempts, "detail")
                    errors.append(f"{c.name}: {item.get('title', '')!r}: malformed detail: {e}")
                    continue
                if row is not None:
                    rows.append(row)
                    c_kept += 1

            ok += 1
            kept += c_kept
            completed += 1
            ledger.mark_ok(c.slug, c_kept)
            ok_tenants.append((c.priority, c_kept, c_fetched, c.name))

        # Persisted even on a deadline cut — that is the case it exists for.
        # Only the rotated tail advances; the fixed head is crawled every run.
        cursor.advance(tail, max(0, completed - len(head)), key=lambda c: c.slug)
        save_cursor(cursor)
        ledger.flush()

        summary = (f"Companies polled: {polled} | OK: {ok} | Err: {err_other} "
                   f"| Rows kept: {kept}")
        yield_line = (f"List paths: {new_matched_paths} new+classified "
                      f"| {new_unmatched_paths} new, no vertical "
                      f"| {repeat_paths} repeat")
        report_summary = [summary, yield_line, ""]
        top = sorted(ok_tenants, key=lambda t: t[:3],
                     reverse=True)[:REPORT_TOP_TENANTS]
        report_lines = [f"| {name} | OK | {fetched} | {kept_} | |"
                        for _, kept_, fetched, name in top] + error_lines
        if report_lines:
            report_summary.extend([
                "| company | status | fetched | kept | error |",
                "|---|---|---|---|---|",
            ] + report_lines)

        return SourceResult(rows, report_summary, errors)
