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
from applyourself.discovery.sources.boards import ashby, greenhouse, lever, workday
from applyourself.discovery.sources.boards.base import load_boards
from applyourself.discovery.sources.feed import arbeitsagentur, oracle
from applyourself.discovery.sources.feed.failed_reads import FailedReads
from applyourself.discovery.sources.lane import Lane, LaneStats
from applyourself.discovery.store import make_posting
from applyourself.shared import http_client, identity, title_gate
from applyourself.shared.countries import CC_TO_COUNTRY
from applyourself.shared.http_client import CareersError
from applyourself.shared.job_boards import board_key, parse_posting_url
from applyourself.shared.location.parser import read_location

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
    """The feed's location, with its country code's name added unless the text
    is one country for sure ("DE (DE91C)" -> "DE (DE91C), Germany")."""
    text = location.strip() if isinstance(location, str) else ""
    code = (country_iso or "").strip().upper() if isinstance(country_iso, str) else ""
    country = CC_TO_COUNTRY.get("GB" if code == "UK" else code, "")
    if not country:
        return text
    if read_location(text).sure:
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
        """Read tonight's file into `self.rows`. A failure is kept and
        re-raised by `run`, so the report shows it as this lane's crash."""
        try:
            manifest = http_client.fetch_json(MANIFEST_URL, deadline_ts=crawl.deadline_ts or None)
            src = crawl.cfg.sources[self.name]
            age = snapshot_age_hours(manifest.get("generated_at"))
            if age is None or age > src.max_snapshot_age_hours:
                self.stats.errors.append(
                    f"snapshot {manifest.get('generated_at')!r} is "
                    f"{'undated' if age is None else f'{age:.0f}h old'} "
                    f"(max {src.max_snapshot_age_hours}h); skipped"
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

    def _settle(self, held, pages, failed, crawl) -> int:
        """Tonight's 404s: gone where their API answered tonight, else a host
        failure, and a stored posting is seen as before. How many went gone."""
        gone, unsure = 0, dict[str, int]()
        for key, (site, stored) in held.items():
            if site in pages.worked:
                failed.mark_gone(key)
                gone += 1
                self.stats.liveness_gone += stored is not None
            else:
                unsure[site] = unsure.get(site, 0) + 1
                if stored is not None:
                    row = stored["row"]
                    crawl.batch.upsert(row) if row is not None else crawl.batch.touch(key)
        for site, n in sorted(unsure.items()):
            self.stats.errors.append(f"{site}: {n} 404s and no answer tonight; none marked gone")
        return gone

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
        """Gate every title and map the rows that pass."""
        names = board_names(crawl.boards_dir)
        pf = pq.ParquetFile(path)
        cols = [c for c in COLUMNS if c in pf.schema_arrow.names]
        gate: dict[str, title_gate.Verdict] = {}
        for i in range(pf.metadata.num_row_groups):
            if crawl.deadline_reached():
                self.stats.truncated = True
                break
            titles = pf.read_row_group(i, columns=["title"]).column("title").to_pylist()
            self.stats.listed += len(titles)
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
            crawl.cfg.forget_after_days,
        )
        if failed.problem:
            stats.notes.append(failed.problem)
        skipped = gone = 0
        # 404s tonight, {key: (api site, what a stored posting gets if not gone)}: a 404
        # means gone only once that API has answered tonight.
        held: dict[str, tuple[str, dict | None]] = {}
        # Tonight's API answer per posting: a repeated listing is not asked again.
        answers: dict[str, tuple[str | None, str]] = {}

        def ask(key: str, reader) -> tuple[tuple[str | None, str], bool]:
            """(the API's (JD, answer), whether it was asked just now)."""
            if key in answers:
                return answers[key], False
            out = pages.call(reader[0], reader[1])
            if out[0] is not None:
                answers[key] = out
            return out, out[0] is not None

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
            reader = posting_api(posting["url"], crawl.deadline_ts or None)
            if known is not None and key not in crawl.tracked_keys and reader and reader[2]:
                # A stored posting its API calls gone is not seen again, so it ages out.
                if failed.gone(key):
                    stats.liveness_gone_known += 1
                    gone += 1
                    continue
                (_, answer), asked = ask(key, reader)
                stats.liveness_checked += asked
                if answer == "gone":
                    held[key] = (reader[0], None)
                elif answer == "ok":
                    failed.answered(key)
            needs_jd = known is None and len(posting["jd_text"].strip()) < parse.MIN_JD_CHARS
            wanted_jd, attempted = needs_jd, False
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
                    attempted = True
                needs_jd = False
            if needs_jd and reader is not None and reader[2] and failed.gone(key):
                # Its API said gone: neither it nor the page is read again for now.
                gone += 1
                needs_jd = needs_name = False
            elif needs_jd and reader is not None:
                # A host with a JSON API: its JD is read there, else from the page.
                (found, answer), _ = ask(key, reader)
                attempted = attempted or found is not None
                if answer == "gone" and reader[2]:
                    held[key] = (reader[0], None)
                elif answer == "ok" and reader[2]:
                    failed.answered(key)
                if len((found or "").strip()) >= parse.MIN_JD_CHARS:
                    posting = {**posting, "jd_text": found or ""}
                    needs_jd = False
            if needs_jd and failed.skip(key):
                # Its page failed night after night: left for a later retry.
                skipped += 1
            elif needs_jd or needs_name:
                page = pages.read(posting["url"])
                tried = needs_jd and page is not None
                attempted = attempted or tried
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
            if wanted_jd and attempted:
                stats.detail_ok += has_jd
                stats.detail_failed += not has_jd
            if known is not None:
                stored_as = posting if has_jd or employer else None
                if key in held:
                    # Decided once the night shows whether the API answers at all.
                    held[key] = (held[key][0], {"row": stored_as})
                elif stored_as is not None:
                    crawl.batch.upsert(posting)
                else:
                    crawl.batch.touch(key)
                continue
            if not has_jd:
                stats.short_jd += 1
                if tried:
                    failed.failed(key)
                continue
            held.pop(key, None)
            failed.read(key)
            crawl.known[key] = {"updated_at": "", "company": posting["company"]}
            crawl.batch.upsert(posting)
        self.rows = []
        gone += self._settle(held, pages, failed, crawl)
        if skipped:
            stats.notes.append(
                f"{skipped} page reads skipped: failed {src.page_read_max_failures} nights, "
                f"read again every {src.page_read_retry_days}d"
            )
        if gone:
            stats.notes.append(
                f"{gone} postings their API calls gone: not read, not seen, for "
                f"{crawl.cfg.forget_after_days}d"
            )
        try:
            failed.save()
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


def posting_api(url: str, deadline_ts: float | None) -> tuple[str, Callable[[], str], bool] | None:
    """(the API's give-up key, a call returning the posting's JD, whether its
    404 means the posting is gone) for a url whose host has a JSON API we read,
    else None."""
    api = arbeitsagentur.detail_url(url)
    if api:
        return (
            "api:arbeitsagentur",
            lambda: arbeitsagentur.jd_text(
                http_client.fetch_json(api, deadline_ts=deadline_ts, headers=arbeitsagentur.HEADERS)
            ),
            True,
        )
    api = workday.detail_api_url(url)
    if api:
        return (
            "api:workday",
            lambda: workday.jd_text(http_client.fetch_json(api, deadline_ts=deadline_ts)),
            False,
        )
    hit = parse_posting_url(url)
    if hit is None or not hit.slug or not hit.posting_id:
        return None
    slug, pid = hit.slug, hit.posting_id
    if hit.source == "greenhouse":
        return (
            "api:greenhouse",
            lambda: _jd(greenhouse.fetch_posting(slug, pid, "", deadline_ts)),
            False,
        )
    if hit.source == "lever":
        return (
            "api:lever",
            lambda: _jd(lever.fetch_posting(slug, pid, "", hit.region, deadline_ts)),
            False,
        )
    if hit.source == "ashby":
        return (
            "api:ashby",
            lambda: parse.html_to_text(
                (ashby.job_posting(slug, pid, deadline_ts) or {}).get("descriptionHtml")
            ),
            False,
        )
    return None


def _jd(posting: dict | None) -> str:
    return posting["jd_text"] if posting else ""


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

    def call(self, site: str, fetch: Callable[[], str]) -> tuple[str | None, str]:
        """(`fetch()`'s JD, paced, "" when it gave none, None when not called (the
        deadline, or `site` given up tonight); "ok" for an answer, "gone" for a
        404, else ""). A 404 is that posting's answer, so it never counts toward
        giving the site up."""
        if self.crawl.deadline_reached() or (
            site not in self.worked and self.failures.get(site, 0) >= MAX_HOST_FAILURES
        ):
            return None, ""
        self.pacer.wait()
        answer = ""
        try:
            jd = fetch() or ""
        except Exception as e:  # noqa: BLE001 -- one posting, not the lane
            jd = ""
            if isinstance(e, CareersError) and e.status == 404:
                answer = "gone"
            else:
                self.stats.errors.append(f"{site}: {type(e).__name__}: {e}")
                self.failures[site] = self.failures.get(site, 0) + 1
        else:
            answer = "ok"
            self.worked.add(site)
        return jd, answer

    def result(self, url: str, ok: bool) -> None:
        site = site_of(url)
        if ok:
            self.worked.add(site)
        else:
            self.failures[site] = self.failures.get(site, 0) + 1

    def given_up(self) -> list[str]:
        return sorted(
            s for s, n in self.failures.items() if n >= MAX_HOST_FAILURES and s not in self.worked
        )
