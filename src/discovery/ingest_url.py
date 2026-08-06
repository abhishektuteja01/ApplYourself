"""URL ingest: fetch ONE job description by URL into the normal raw archive
and rebuild jobs/clean.parquet (same cleaning path as discovery).

Deterministic plumbing: no LLM calls (R7), no browser automation
— one or two unauthenticated GETs. Recognized ATS posting URLs (Greenhouse /
Lever / Ashby) are fetched via their public JSON APIs and parsed by the
existing careers.py parsers, so company/title/JD are structured, not scraped.
Any other URL falls back to a raw HTML fetch stripped to text, and REQUIRES
explicit --company/--title (the job_id hash depends on them — never guessed).

    uv run python -m src.discovery.ingest_url <url> [--vertical NAME]
        [--company "..."] [--title "..."]

Prints the resulting job_id. tailor_cli invokes ingest() directly for the
one-command `<vertical> <url>` tailoring flow.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests

from src import verticals
from src.discovery import cleaning
from src.discovery import htmlutil
from src.discovery.sources.ats import http, greenhouse, lever, ashby
from src.discovery.schema import make_row, naive_datetime
from src.discovery.orchestrator import (
    JOBS_RAW,
    JOBS_ROOT,
    JOBS_RUNS,
    PIPELINE,
    current_run_id,
)

log = logging.getLogger(__name__)

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

_GH_PATH_RE = re.compile(r"^/(?:embed/job_app/)?([^/]+)/jobs/(\d+)")
_LEVER_PATH_RE = re.compile(r"^/([^/]+)/(" + _UUID + r")")
_ASHBY_PATH_RE = re.compile(r"^/([^/]+)/(" + _UUID + r")")


class IngestError(Exception):
    """Actionable failure; the CLI prints the message verbatim and exits 1."""


# careers.html_to_text strips tags but keeps their text content — right for
# ATS API fragments (pure prose), wrong for full pages where <script>/<style>
# bodies would leak in as "JD text" and defeat the 200-char floor.
_DROP_BLOCKS_RE = re.compile(
    r"<(script|style|noscript|template|svg)\b.*?</\1\s*>",
    re.I | re.S,
)


def _page_to_text(html: str) -> str:
    return htmlutil.html_to_text(_DROP_BLOCKS_RE.sub(" ", html))


def _slug_to_name(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip().title()


def parse_ats_url(url: str) -> tuple[str, str, str] | None:
    """Recognize a per-posting ATS URL. Returns (ats, board_slug, posting_id)
    or None (generic URL)."""
    try:
        parts = urlparse(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    path = parts.path or ""
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io"):
        m = _GH_PATH_RE.match(path)
        if m:
            return ("greenhouse", m.group(1), m.group(2))
        # embed form: .../embed/job_app?for=<slug>&token=<id>
        qs = parse_qs(parts.query)
        slug, token = qs.get("for", [""])[0], qs.get("token", [""])[0]
        if slug and token.isdigit():
            return ("greenhouse", slug, token)
        return None
    if host in ("jobs.lever.co", "jobs.eu.lever.co"):
        m = _LEVER_PATH_RE.match(path)
        if m:
            region = "eu." if host == "jobs.eu.lever.co" else ""
            # region rides along in the ats field so the fetch hits the
            # right API host; the row's `site` stays "lever" either way
            return (f"lever{':eu' if region else ''}", m.group(1), m.group(2))
        return None
    if host == "jobs.ashbyhq.com":
        m = _ASHBY_PATH_RE.match(path)
        if m:
            return ("ashby", m.group(1), m.group(2))
        return None
    return None


# ---------------------------------------------------------------------
# Per-ATS single-posting fetch -> JobSpy-shaped row (careers.py parsers)
# ---------------------------------------------------------------------

def _fetch_greenhouse(slug: str, posting_id: str) -> dict:
    posting = http.fetch_json(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{posting_id}"
    )
    if not isinstance(posting, dict) or not posting.get("title"):
        raise IngestError(f"ERROR: Greenhouse posting {posting_id} on board "
                          f"{slug!r} returned no title — check the URL.")
    # per-posting payloads omit absolute_url sometimes; parser requires one
    posting.setdefault(
        "absolute_url",
        f"https://boards.greenhouse.io/{slug}/jobs/{posting_id}",
    )
    try:
        board = http.fetch_json(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}"
        )
        company = (board or {}).get("name") or _slug_to_name(slug)
    except http.CareersError:
        company = _slug_to_name(slug)
    rows = greenhouse.greenhouse_rows({"jobs": [posting]}, company)
    if not rows:
        raise IngestError(f"ERROR: could not parse Greenhouse posting "
                          f"{posting_id} on board {slug!r}.")
    return rows[0]


def _fetch_lever(slug: str, posting_id: str, region: str = "") -> dict:
    api_host = "api.eu.lever.co" if region == "eu" else "api.lever.co"
    posting = http.fetch_json(
        f"https://{api_host}/v0/postings/{slug}/{posting_id}"
    )
    rows = lever.lever_rows(
        [posting] if isinstance(posting, dict) else [], _slug_to_name(slug)
    )
    if not rows:
        raise IngestError(f"ERROR: could not parse Lever posting {posting_id} "
                          f"on board {slug!r} — check the URL.")
    return rows[0]


def _fetch_ashby(slug: str, posting_id: str) -> dict:
    # Ashby has no public per-posting endpoint; fetch the board and match
    # the posting UUID against id/jobUrl/applyUrl.
    payload = http.fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    pid = posting_id.lower()
    matches = [
        j for j in (payload or {}).get("jobs", [])
        if isinstance(j, dict) and (
            str(j.get("id", "")).lower() == pid
            or pid in str(j.get("jobUrl", "")).lower()
            or pid in str(j.get("applyUrl", "")).lower()
        )
    ]
    if not matches:
        raise IngestError(
            f"ERROR: posting {posting_id} not found on Ashby board {slug!r} — "
            f"the role may have closed, or the URL is wrong."
        )
    rows = ashby.ashby_rows({"jobs": matches}, _slug_to_name(slug))
    if not rows:
        raise IngestError(f"ERROR: could not parse Ashby posting {posting_id} "
                          f"on board {slug!r}.")
    return rows[0]


def _fetch_generic(url: str, company: str | None, title: str | None) -> dict:
    if not company or not title:
        raise IngestError(
            "ERROR: this URL is not a recognized ATS posting (Greenhouse / "
            "Lever / Ashby), so company and title cannot be extracted "
            "reliably — they define the job_id hash and are never guessed. "
            "Re-run with --company \"...\" --title \"...\"."
        )
    try:
        resp = requests.get(url, timeout=http.REQUEST_TIMEOUT,
                            headers=http._HEADERS)
    except requests.RequestException as exc:
        raise IngestError(f"ERROR: fetch failed: {url}: {exc}") from exc
    if resp.status_code != 200:
        raise IngestError(f"ERROR: HTTP {resp.status_code} fetching {url} — "
                          f"the posting may be gone or behind a login.")
    text = _page_to_text(resp.text)
    return make_row(site="manual", company=company, title=title, job_url=url,
                    description=text)


def fetch_row(url: str, *, company: str | None = None,
              title: str | None = None) -> dict:
    """URL -> JobSpy-shaped row dict. ATS URLs use the JSON APIs; anything
    else is a raw HTML fetch requiring explicit company/title."""
    parsed = parse_ats_url(url)
    if parsed is None:
        return _fetch_generic(url, company, title)
    ats, slug, pid = parsed
    try:
        if ats == "greenhouse":
            row = _fetch_greenhouse(slug, pid)
        elif ats.startswith("lever"):
            row = _fetch_lever(slug, pid, region="eu" if ats.endswith(":eu") else "")
        else:
            row = _fetch_ashby(slug, pid)
    except http.CareersError as exc:
        raise IngestError(f"ERROR: {exc}") from exc
    # explicit overrides win — they define the job_id hash
    if company:
        row["company"] = company
    if title:
        row["title"] = title
    return row


# ---------------------------------------------------------------------
# Ingest: row -> raw archive -> cleaning rebuild -> job_id
# ---------------------------------------------------------------------

def ingest(
    url: str,
    *,
    vertical: str | None = None,
    company: str | None = None,
    title: str | None = None,
    raw_dir: Path = JOBS_RAW,
    clean_dir: Path = JOBS_ROOT,
    runs_dir: Path = JOBS_RUNS,
    pipeline_dir: Path = PIPELINE,
) -> dict:
    """Fetch the JD at url, archive it as a raw row, rebuild clean.parquet
    via the standard cleaning path, and return
    {job_id, company, title, vertical, url, source, location, jd_text}.
    Fails loud (IngestError) on every dead end — never a partial write."""
    row = fetch_row(url, company=company, title=title)

    jd = (row.get("description") or "").strip()
    if len(jd) < 200:
        raise IngestError(
            f"ERROR: JD text extracted from {url} is {len(jd)} chars — below "
            f"the 200-char cleaning floor. The page may be "
            f"JS-rendered or the posting closed. Paste the JD into inbox/ "
            f"as a manual clip instead."
        )

    cfg = verticals.get_config()
    v = vertical or cleaning.classify_vertical_from_title(row["title"])
    if v not in cfg.verticals:
        _bad_vertical(vertical, sorted(cfg.verticals))
    row["vertical"] = v

    # A URL ingest is a deliberate, user-initiated add (URL-mode /tailor), so it
    # must bypass discovery's title_exclude_terms exactly like an inbox/ manual
    # clip — otherwise a deliberately saved senior role would be dropped by the
    # seniority/adjacent title families. _fetch_generic already tags
    # site="manual"; normalize the ATS fetch paths (greenhouse/lever/ashby) here
    # too. Scoped to ingest() only — the ATS *scraper* keeps its real source.
    row["site"] = "manual"

    run_id = current_run_id()
    now = pd.Timestamp.now().normalize()
    df = pd.DataFrame([row])
    df["ingested_run_id"] = run_id
    df["scraped_date"] = now
    df["date_posted"] = naive_datetime(df["date_posted"])

    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"{run_id}.parquet"
    if raw_path.exists():
        # same-minute collision with another run: merge, never clobber
        prior = pd.read_parquet(raw_path)
        df = pd.concat([prior, df], ignore_index=True)
        if "date_posted" in df.columns:
            df["date_posted"] = naive_datetime(df["date_posted"])
    df.to_parquet(raw_path, index=False)
    log.info("archived 1 row from %s to %s", url, raw_path)

    clean_df = cleaning.run(
        run_id=run_id, raw_dir=raw_dir, clean_dir=clean_dir,
        runs_dir=runs_dir, pipeline_dir=pipeline_dir,
    )

    company_n = cleaning.normalize_company(row["company"])
    title_n = cleaning.normalize_title(row["title"])
    job_id = cleaning.compute_job_id(company_n, title_n)
    if job_id not in set(clean_df["job_id"]):
        siblings = clean_df[clean_df["company_normalized"] == company_n]
        hint = ""
        if not siblings.empty:
            listing = "; ".join(
                f"{r.job_id} {r.title!r}" for r in siblings.itertuples()
            )
            hint = (f"\nA near-duplicate title at the same company already "
                    f"exists and won dedupe — tailor that job_id instead: "
                    f"{listing}")
        raise IngestError(
            f"ERROR: row for {row['company']!r} / {row['title']!r} did not "
            f"survive cleaning (job_id {job_id} absent from "
            f"clean.parquet).{hint}"
        )
    kept = clean_df.set_index("job_id").loc[job_id]
    return {
        "job_id": job_id,
        "company": str(kept["company"]),
        "title": str(kept["title"]),
        "vertical": str(kept["vertical"]),
        "url": str(kept["url"]),
        "source": str(kept["source"]),
        "location": str(kept["location"]),
        "jd_text": str(kept["jd_text"]),
    }


def _bad_vertical(requested: str | None, configured: list[str]):
    if requested:
        raise IngestError(f"ERROR: vertical {requested!r} is not configured. "
                          f"Configured verticals: {configured}")
    raise IngestError(
        f"ERROR: the title did not classify into any configured vertical "
        f"and none was given. Re-run with --vertical, one of: {configured}"
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    parser = argparse.ArgumentParser(prog="python -m src.discovery.ingest_url",
                                     description=__doc__)
    parser.add_argument("url", help="job-posting URL")
    parser.add_argument("--vertical", default=None,
                        help="configured vertical name (else title classifier)")
    parser.add_argument("--company", default=None,
                        help="explicit company (required for non-ATS URLs)")
    parser.add_argument("--title", default=None,
                        help="explicit title (required for non-ATS URLs)")
    args = parser.parse_args(argv)
    try:
        info = ingest(args.url, vertical=args.vertical,
                      company=args.company, title=args.title)
    except IngestError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Ingested: {info['company']} — {info['title']} "
          f"[{info['vertical']}] (source: {info['source']})")
    print(f"job_id: {info['job_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
