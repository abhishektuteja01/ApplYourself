"""The stapply feed: one nightly parquet of public job postings from many ATSs
and job sites (storage.stapply.ai), read in place of crawling them.

`prepare` runs before the other lanes: it reads the manifest, skips a
snapshot older than `max_snapshot_age_hours`, downloads the file, title-gates
every row one row group at a time, maps the passing rows, and records which
of our boards the file carried so the board lanes skip them. `run` then
saves the rows, fetching a JD from the posting's host API or its own page
only for a new row the file has none for. The file is deleted once read.

Deterministic and LLM-free (R7).
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from applyourself.discovery import parse
from applyourself.discovery.config import pacing_for
from applyourself.discovery.sources.boards.base import load_boards
from applyourself.discovery.sources.feed import arbeitsagentur, oracle
from applyourself.discovery.sources.feed.failed_reads import FailedReads
from applyourself.discovery.sources.lane import Lane, LaneStats
from applyourself.discovery.store import make_posting
from applyourself.shared import http_client, identity, title_gate
from applyourself.shared.countries import CC_TO_COUNTRY
from applyourself.shared.http_client import CareersError
from applyourself.shared.job_boards import ATS_SOURCE_NAMES, board_key
from applyourself.shared.location.parser import parse_location

log = logging.getLogger(__name__)

MANIFEST_URL = "https://storage.stapply.ai/jobhive/v1/manifest.json"
# The columns a posting is built from; the rest of the file is never read.
COLUMNS = [
    "url",
    "title",
    "company",
    "ats_type",
    "location",
    "country_iso",
    "is_remote",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_period",
    "employment_type",
    "description",
    "posted_at",
]
# Our own board lanes: rows of these ATSs say which boards the file covered.
_BOARD_ATS = frozenset(ATS_SOURCE_NAMES)
# A site whose first this-many page reads tonight all failed is not read again
# tonight: its pages carry no JobPosting block, or it blocks us.
MAX_HOST_FAILURES = 10


def snapshot_age_hours(generated_at: str, now: datetime | None = None) -> float | None:
    try:
        when = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - when).total_seconds() / 3600


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(http_client.DOWNLOAD_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def board_names(boards_dir=None) -> dict[str, str]:
    """{Board.key: name} for every board we list: the feed names a known
    board's company by its slug ("ddome"), we by its name ("DataDome")."""
    return {b.key: b.name for b in load_boards(boards_dir=boards_dir)}


def company_names(companies: pd.DataFrame) -> dict[tuple[Any, str], str]:
    """{(ats, slug or site host): name} from the feed's own company list. A
    row's `company` is often that slug ("whatfix101") or host
    ("jobs.danaher.com"); the list has the name ("Whatfix", "Danaher")."""
    out: dict[tuple[Any, str], str] = {}
    for ats, name, slug, url in zip(
        companies["ats"], companies["name"], companies["slug"], companies["url"], strict=True
    ):
        if not isinstance(name, str) or not name.strip():
            continue
        for key in (slug, urlsplit(url).hostname if isinstance(url, str) else None):
            if isinstance(key, str) and key.strip():
                out.setdefault((ats, key.strip().lower()), name.strip())
    return out


def located(location, country_iso) -> str:
    """The feed's location, with its country code's name added when our own
    parser finds no country in it ("DE (DE91C)" -> "DE (DE91C), Germany")."""
    text = location.strip() if isinstance(location, str) else ""
    code = (country_iso or "").strip().upper() if isinstance(country_iso, str) else ""
    country = CC_TO_COUNTRY.get("GB" if code == "UK" else code, "")
    if not country:
        return text
    parsed = parse_location(text)
    if parsed.country or parsed.candidate_countries:
        return text
    return f"{text}, {country}" if text else country


def posting_from(
    row: dict, vertical: str, names: dict[str, str], feed_names: dict[tuple[Any, str], str]
) -> dict:
    """Our board's name first, then the feed list's, then the row's own."""
    url = row.get("url") or ""
    own = row.get("company") or ""
    company = (
        names.get(board_key(url))
        or feed_names.get((row.get("ats_type"), own.strip().lower()))
        or own
    )
    period = row.get("salary_period")
    return make_posting(
        source="stapply",
        company=company if identity.normalize_company(company) else "",
        title=row.get("title") or "",
        vertical=vertical,
        url=url,
        location_raw=located(row.get("location"), row.get("country_iso")),
        jd_text=parse.html_to_text(row.get("description")),
        posted_date=parse.iso_date(row.get("posted_at")),
        remote_flag=parse.as_bool(row.get("is_remote")),
        salary_min=parse.yearly_salary(row.get("salary_min"), period),
        salary_max=parse.yearly_salary(row.get("salary_max"), period),
        salary_currency=row.get("salary_currency") or "",
        employment_type=row.get("employment_type") or "",
    )


class StapplyLane(Lane):
    name = "stapply"

    def __init__(self) -> None:
        self.stats = LaneStats()
        self.rows: list[dict] = []
        self.failure: BaseException | None = None

    # -----------------------------------------------------------------
    # before the other lanes: the file
    # -----------------------------------------------------------------

    def prepare(self, crawl) -> None:
        """Read tonight's file into `self.rows` and set `crawl.covered`. A
        failure is kept and re-raised by `run`, so the report shows it as
        this lane's crash and the board lanes crawl everything."""
        try:
            manifest = http_client.fetch_json(MANIFEST_URL, deadline_ts=crawl.deadline_ts or None)
            src = crawl.cfg.sources[self.name]
            age = snapshot_age_hours(manifest.get("generated_at"))
            if age is None or age > src.max_snapshot_age_hours:
                self.stats.errors.append(
                    f"snapshot {manifest.get('generated_at')!r} is "
                    f"{'undated' if age is None else f'{age:.0f}h old'} "
                    f"(max {src.max_snapshot_age_hours}h); skipped, every board crawled"
                )
                return
            whole = manifest["all"]
            with tempfile.TemporaryDirectory(prefix="stapply-") as tmp:
                feed_names = self._company_names(manifest, Path(tmp), crawl)
                path = Path(tmp) / "all.parquet"
                http_client.download(whole["parquet"], path, deadline_ts=crawl.deadline_ts or None)
                if whole.get("parquet_sha256") and sha256_of(path) != whole["parquet_sha256"]:
                    raise CareersError(f"checksum mismatch: {whole['parquet']}")
                self.read(path, crawl, feed_names)
            self.stats.notes.append(f"snapshot {manifest.get('generated_at')} ({age:.0f}h old)")
        except Exception as e:  # noqa: BLE001 -- surfaced by run()
            self.failure = e

    def _company_names(self, manifest: dict, tmp: Path, crawl) -> dict:
        """The feed's company list, or {} with a note: without it rows keep
        the feed's own company text, which is no reason to lose the night."""
        entry = manifest.get("companies") or {}
        try:
            path = tmp / "companies.parquet"
            http_client.download(entry["parquet"], path, deadline_ts=crawl.deadline_ts or None)
            if entry.get("parquet_sha256") and sha256_of(path) != entry["parquet_sha256"]:
                raise CareersError("checksum mismatch")
            return company_names(pd.read_parquet(path, columns=["ats", "name", "slug", "url"]))
        except Exception as e:  # noqa: BLE001 -- names only
            self.stats.notes.append(
                f"company list not read ({type(e).__name__}: {e}); feed company text kept"
            )
            return {}

    def read(self, path: Path, crawl, feed_names: dict | None = None) -> None:
        """Gate every title, map the rows that pass, collect covered boards."""
        names = board_names(crawl.boards_dir)
        pf = pq.ParquetFile(path)
        cols = [c for c in COLUMNS if c in pf.schema_arrow.names]
        gate: dict[str, title_gate.Verdict] = {}
        covered: set[str] = set()
        for i in range(pf.metadata.num_row_groups):
            if crawl.deadline_reached():
                self.stats.truncated = True
                break
            light = pf.read_row_group(i, columns=["title", "ats_type", "url"])
            titles = light.column("title").to_pylist()
            self.stats.listed += len(titles)
            for url, ats in zip(
                light.column("url").to_pylist(), light.column("ats_type").to_pylist(), strict=True
            ):
                if ats in _BOARD_ATS and (key := board_key(url or "")):
                    covered.add(key)
            keep, verticals = [], []
            for t in titles:
                v = gate.get(t)
                if v is None:
                    v = gate[t] = title_gate.lane_for(t or "", "", self.name, crawl.vcfg)
                keep.append(v.passes)
                if v.passes:
                    verticals.append(v.lane)
                else:
                    self.stats.drop(v.reason, t or "")
                if v.lane:
                    self.stats.classified += 1
            if not any(keep):
                continue
            full = pf.read_row_group(i, columns=cols).filter(pa.array(keep)).to_pylist()
            for row, vertical in zip(full, verticals, strict=True):
                posting = posting_from(row, vertical, names, feed_names or {})
                crawl.record_listing(
                    self.name, posting["title"], vertical, True, posting["posting_key"]
                )
                self.rows.append(posting)
        if not self.stats.truncated:
            crawl.covered = frozenset(covered)

    # -----------------------------------------------------------------
    # with the other lanes: the store
    # -----------------------------------------------------------------

    def run(self, crawl, tally=None) -> LaneStats:
        if self.failure is not None:
            raise self.failure
        stats = self.stats
        pages = _PageReader(pacing_for(crawl.cfg, self.name), crawl, stats)
        src = crawl.cfg.sources[self.name]
        failed = FailedReads(
            crawl.failed_reads,
            crawl.today.date(),
            src.page_read_max_failures,
            src.page_read_retry_days,
        )
        if failed.problem:
            stats.notes.append(failed.problem)
        skipped = 0
        named = 0
        site_names: dict[str, str] = {}
        for posting in self.rows:
            stats.passed += 1
            key = posting["posting_key"]
            known = crawl.known.get(key)
            stored = (known or {}).get("company", "")
            if (
                stored
                and not parse.looks_like_slug(stored)
                and (
                    parse.looks_like_slug(posting["company"])
                    or oracle.site_url(posting["url"], posting["company"])
                )
            ):
                posting = {**posting, "company": stored}
            stats.known += known is not None
            stats.new += known is None
            needs_jd = known is None and len(posting["jd_text"].strip()) < parse.MIN_JD_CHARS
            needs_name = parse.looks_like_slug(posting["company"])
            employer = ""
            # Whether tonight read this new posting's page for its JD.
            tried = False
            site = oracle.site_url(posting["url"], posting["company"])
            if site:
                # Named by its Oracle tenant code: the site's page names the employer.
                if site not in site_names:
                    page = pages.read(site)
                    site_names[site] = oracle.site_name(page)
                    if page is not None:
                        pages.result(site, bool(site_names[site]))
                employer = site_names[site]
                needs_name = False
            api = oracle.detail_url(posting["url"]) if needs_jd else ""
            if api:
                # Its job page is built in the browser: read the JD from the API.
                payload = pages.read(api, as_json=True)
                jd = oracle.jd_text(payload) if payload is not None else ""
                if jd:
                    posting = {**posting, "jd_text": jd}
                if payload is not None:
                    pages.result(posting["url"], bool(jd))
                needs_jd = False
            reader = posting_api(posting["url"], crawl.deadline_ts or None) if needs_jd else None
            if reader is not None:
                # A host with a JSON API: its JD is read there, else from the page.
                found = pages.call(*reader)
                if found:
                    posting = {**posting, "jd_text": found}
                    needs_jd = False
            if needs_jd and failed.skip(key):
                # Its page failed night after night: left for a later retry.
                skipped += 1
            elif needs_jd or needs_name:
                page = pages.read(posting["url"])
                tried = needs_jd and page is not None
                jd = parse.jobposting_text(page) if needs_jd else ""
                if needs_name:
                    employer = parse.jobposting_employer(page)
                    if parse.looks_like_slug(employer):
                        employer = ""
                if jd:
                    posting = {**posting, "jd_text": jd}
                if page is not None:
                    pages.result(posting["url"], bool(jd or employer))
            if employer:
                named += 1
                posting = {**posting, "company": employer}
                if known is None:
                    posting["company_normalized"] = identity.normalize_company(employer)
            has_jd = len(posting["jd_text"].strip()) >= parse.MIN_JD_CHARS
            if known is not None:
                if has_jd or employer:
                    crawl.batch.upsert(posting)
                else:
                    crawl.batch.touch(key)
                continue
            if not has_jd:
                stats.short_jd += 1
                if tried:
                    failed.failed(key)
                continue
            failed.read(key)
            crawl.known[key] = {"updated_at": "", "company": posting["company"]}
            crawl.batch.upsert(posting)
        self.rows = []
        if skipped:
            stats.notes.append(
                f"{skipped} page reads skipped: failed {src.page_read_max_failures} nights, "
                f"read again every {src.page_read_retry_days}d"
            )
        try:
            failed.save(crawl.cfg.forget_after_days)
        except OSError as e:
            stats.notes.append(f"{crawl.failed_reads.name} not saved ({type(e).__name__}: {e})")
        if named:
            stats.notes.append(f"{named} company names read from job pages")
        if pages.given_up():
            stats.notes.append(
                f"nothing read from the first {MAX_HOST_FAILURES} pages, not read "
                f"again tonight: {', '.join(pages.given_up())}"
            )
        return stats


def posting_api(url: str, deadline_ts: float | None) -> tuple[str, Callable[[], str]] | None:
    """(the API's give-up key, a call returning the posting's JD) for a url whose
    host has a JSON API we read, else None."""
    api = arbeitsagentur.detail_url(url)
    if api:
        return "api:arbeitsagentur", lambda: arbeitsagentur.jd_text(
            http_client.fetch_json(api, deadline_ts=deadline_ts, headers=arbeitsagentur.HEADERS)
        )
    return None


def site_of(url: str) -> str:
    """The site a page belongs to, across its subdomains: "obyteshr.bamboohr.com"
    and "prospect.bamboohr.com" are both bamboohr.com."""
    return ".".join((urlsplit(url).hostname or "").split(".")[-2:])


class _PageReader:
    """One page read at a time, paced, with a site given up for the night
    after MAX_HOST_FAILURES reads and no success."""

    def __init__(self, pacing: float, crawl, stats: LaneStats):
        self.pacer = http_client.Pacer(pacing, since_last=True)
        self.crawl, self.stats = crawl, stats
        self.failures: dict[str, int] = {}
        self.worked: set[str] = set()

    def read(self, url: str, as_json: bool = False):
        """The page body (decoded JSON when `as_json`), "" for a failed read,
        None when not read."""
        site = site_of(url)
        if self.crawl.deadline_reached() or (
            site not in self.worked and self.failures.get(site, 0) >= MAX_HOST_FAILURES
        ):
            return None
        self.pacer.wait()
        try:
            if as_json:
                return http_client.fetch_json(url, deadline_ts=self.crawl.deadline_ts or None)
            status, body = http_client.fetch_page(url)
        except Exception as e:  # noqa: BLE001 -- one page, not the lane
            self.stats.errors.append(f"{url}: {type(e).__name__}: {e}")
            return ""
        return body if status == 200 else ""

    def call(self, site: str, fetch: Callable[[], str]) -> str | None:
        """`fetch()`'s JD, paced; "" when it gave none, None when not called (the
        deadline, or `site` given up tonight). A 404 is that posting's answer,
        so it never counts toward giving the site up."""
        if self.crawl.deadline_reached() or (
            site not in self.worked and self.failures.get(site, 0) >= MAX_HOST_FAILURES
        ):
            return None
        self.pacer.wait()
        try:
            jd = fetch() or ""
        except Exception as e:  # noqa: BLE001 -- one posting, not the lane
            jd = ""
            if not (isinstance(e, CareersError) and e.status == 404):
                self.stats.errors.append(f"{site}: {type(e).__name__}: {e}")
                self.failures[site] = self.failures.get(site, 0) + 1
        else:
            self.worked.add(site)
        self.stats.detail_ok += bool(jd)
        self.stats.detail_failed += not jd
        return jd

    def result(self, url: str, ok: bool) -> None:
        site = site_of(url)
        if ok:
            self.stats.detail_ok += 1
            self.worked.add(site)
        else:
            self.stats.detail_failed += 1
            self.failures[site] = self.failures.get(site, 0) + 1

    def given_up(self) -> list[str]:
        return sorted(
            s for s, n in self.failures.items() if n >= MAX_HOST_FAILURES and s not in self.worked
        )
