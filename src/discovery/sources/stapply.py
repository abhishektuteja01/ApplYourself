"""The stapply feed: one nightly parquet of public job postings from many ATSs
and job sites (storage.stapply.ai), read in place of crawling them.

`prepare` runs before the other lanes: it reads the manifest, skips a
snapshot older than `max_snapshot_age_hours`, downloads the file, title-gates
every row one row group at a time, maps the passing rows, and records which
of our boards the file carried so the board lanes skip them. `run` then
saves the rows, fetching a JD from the posting's own page only for a new row
the file has none for. The file is deleted once read.

Deterministic and LLM-free (R7).
"""
from __future__ import annotations

import hashlib
import logging
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pyarrow as pa
import pyarrow.parquet as pq

from src import ats_http
from src.ats_registry import ATS_SOURCE_NAMES
from src.ats_http import CareersError
from src.discovery import normalize as N
from src.discovery.location import CC_TO_COUNTRY, parse_location
from src.discovery.sources.boards import LaneStats, board_key_of, load_boards
from src.discovery.store import make_posting

log = logging.getLogger(__name__)

MANIFEST_URL = "https://storage.stapply.ai/jobhive/v1/manifest.json"
# The columns a posting is built from; the rest of the file is never read.
COLUMNS = ["url", "title", "company", "ats_type", "location", "country_iso", "is_remote",
           "salary_min", "salary_max", "salary_currency", "salary_period",
           "employment_type", "description", "posted_at"]
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
        when = when.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - when).total_seconds() / 3600


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(ats_http.DOWNLOAD_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def board_names(boards_dir=None) -> dict[str, str]:
    """{Board.key: name} for every board we list: the feed names a known
    board's company by its slug ("ddome"), we by its name ("DataDome")."""
    return {b.key: b.name for b in load_boards(boards_dir=boards_dir)}


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


def posting_from(row: dict, vertical: str, names: dict[str, str]) -> dict:
    url = row.get("url") or ""
    company = names.get(board_key_of(url)) or (row.get("company") or "")
    period = row.get("salary_period")
    return make_posting(
        source="stapply", company=company if N.normalize_company(company) else "",
        title=row.get("title") or "", vertical=vertical, url=url,
        location_raw=located(row.get("location"), row.get("country_iso")),
        jd_text=N.html_to_text(row.get("description")),
        posted_date=N.iso_date(row.get("posted_at")),
        remote_flag=N.as_bool(row.get("is_remote")),
        salary_min=N.yearly_salary(row.get("salary_min"), period),
        salary_max=N.yearly_salary(row.get("salary_max"), period),
        salary_currency=row.get("salary_currency") or "",
        employment_type=row.get("employment_type") or "",
    )


class StapplyLane:
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
            manifest = ats_http.fetch_json(MANIFEST_URL, deadline_ts=crawl.deadline_ts or None)
            src = crawl.cfg.sources[self.name]
            age = snapshot_age_hours(manifest.get("generated_at"))
            if age is None or age > src.max_snapshot_age_hours:
                self.stats.errors.append(
                    f"snapshot {manifest.get('generated_at')!r} is "
                    f"{'undated' if age is None else f'{age:.0f}h old'} "
                    f"(max {src.max_snapshot_age_hours}h); skipped, every board crawled")
                return
            whole = manifest["all"]
            with tempfile.TemporaryDirectory(prefix="stapply-") as tmp:
                path = Path(tmp) / "all.parquet"
                ats_http.download(whole["parquet"], path, deadline_ts=crawl.deadline_ts or None)
                if whole.get("parquet_sha256") and sha256_of(path) != whole["parquet_sha256"]:
                    raise CareersError(f"checksum mismatch: {whole['parquet']}")
                self.read(path, crawl)
            self.stats.notes.append(f"snapshot {manifest.get('generated_at')} ({age:.0f}h old)")
        except Exception as e:  # noqa: BLE001 -- surfaced by run()
            self.failure = e

    def read(self, path: Path, crawl) -> None:
        """Gate every title, map the rows that pass, collect covered boards."""
        names = board_names(crawl.boards_dir)
        pf = pq.ParquetFile(path)
        cols = [c for c in COLUMNS if c in pf.schema_arrow.names]
        gate: dict[str, tuple[str, bool]] = {}
        covered: set[str] = set()
        for i in range(pf.metadata.num_row_groups):
            if crawl.deadline_reached():
                self.stats.truncated = True
                break
            light = pf.read_row_group(i, columns=["title", "ats_type", "url"])
            titles = light.column("title").to_pylist()
            self.stats.listed += len(titles)
            for url, ats in zip(light.column("url").to_pylist(),
                                light.column("ats_type").to_pylist()):
                if ats in _BOARD_ATS and (key := board_key_of(url or "")):
                    covered.add(key)
            keep, verticals = [], []
            for t in titles:
                v = gate.get(t)
                if v is None:
                    v = gate[t] = N.gate_reason(t or "", self.name, crawl.vcfg)
                keep.append(v[1])
                if v[1]:
                    verticals.append(v[0])
                if v[0]:
                    self.stats.classified += 1
            if not any(keep):
                continue
            full = pf.read_row_group(i, columns=cols).filter(pa.array(keep)).to_pylist()
            for row, vertical in zip(full, verticals):
                posting = posting_from(row, vertical, names)
                crawl.record_listing(self.name, posting["title"], vertical, True,
                                     posting["posting_key"])
                self.rows.append(posting)
        if not self.stats.truncated:
            crawl.covered = frozenset(covered)

    # -----------------------------------------------------------------
    # with the other lanes: the store
    # -----------------------------------------------------------------

    def run(self, crawl) -> LaneStats:
        if self.failure is not None:
            raise self.failure
        stats = self.stats
        pacing = max(1.0, crawl.cfg.sources[self.name].pacing_seconds)
        last_fetch = [0.0]
        failures: dict[str, int] = {}
        worked: set[str] = set()
        for posting in self.rows:
            stats.passed += 1
            key = posting["posting_key"]
            has_jd = len(posting["jd_text"].strip()) >= N.MIN_JD_CHARS
            if key in crawl.known:
                stats.known += 1
                crawl.batch.upsert(posting) if has_jd else crawl.batch.touch(key)
                continue
            stats.new += 1
            if not has_jd:
                host = urlsplit(posting["url"]).hostname or ""
                given_up = host not in worked and failures.get(host, 0) >= MAX_HOST_FAILURES
                if crawl.deadline_reached() or given_up:
                    stats.short_jd += 1
                    continue
                wait = last_fetch[0] + pacing - time.time()
                if wait > 0:
                    time.sleep(wait)
                last_fetch[0] = time.time()
                posting = self._with_page_jd(posting, stats)
                if len(posting["jd_text"].strip()) < N.MIN_JD_CHARS:
                    failures[host] = failures.get(host, 0) + 1
                    stats.short_jd += 1
                    continue
                worked.add(host)
            crawl.known[key] = {"updated_at": ""}
            crawl.batch.upsert(posting)
        self.rows = []
        given_up = sorted(h for h, n in failures.items()
                          if n >= MAX_HOST_FAILURES and h not in worked)
        if given_up:
            stats.notes.append(f"no JD on the first {MAX_HOST_FAILURES} pages, not read again "
                               f"tonight: {', '.join(given_up)}")
        return stats

    @staticmethod
    def _with_page_jd(posting: dict, stats: LaneStats) -> dict:
        """The posting with the JD from its page's JobPosting block, when the
        page has one."""
        try:
            status, body = ats_http.fetch_page(posting["url"])
        except Exception as e:  # noqa: BLE001 -- one page, not the lane
            stats.detail_failed += 1
            stats.errors.append(f"{posting['url']}: {type(e).__name__}: {e}")
            return posting
        text = N.jobposting_text(body) if status == 200 else ""
        if not text:
            stats.detail_failed += 1
            return posting
        stats.detail_ok += 1
        return {**posting, "jd_text": text}
