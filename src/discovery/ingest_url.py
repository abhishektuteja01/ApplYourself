"""`uv run ingest-url <url>`: one job description by url into the jobs store,
then a clean.parquet rebuild. The same store, id rules and view the nightly
run uses.

    uv run ingest-url <url> [--vertical NAME] [--company "..."] [--title "..."]
    uv run ingest-url <url> --dry-run

Greenhouse, Lever and Ashby posting urls are read through their public JSON
APIs. Any other url is fetched as a page and REQUIRES --company and --title:
they define the job_id hash and are never guessed. --dry-run prints the text
and writes nothing. Prints `job_id: <8-hex>` on success.

Deterministic plumbing (R7): one or two GETs, no browser.
"""
from __future__ import annotations

import argparse
import logging
import sys

import pandas as pd

from src import ats_http as http
from src import paths, verticals
from src.ats_registry import parse_posting_url
from src.discovery import normalize as N
from src.discovery import store as S
from src.discovery.config import load_config
from src.discovery.sources import ashby, greenhouse, lever
from src.state_io import load_state_index

log = logging.getLogger(__name__)


class IngestError(Exception):
    """Actionable failure; the CLI prints the message verbatim and exits 1."""


def _slug_name(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip().title()


def fetch_row(url: str, *, company: str | None = None, title: str | None = None) -> dict:
    """url -> a store row. Explicit --company/--title win: they define the id."""
    hit = parse_posting_url(url)
    try:
        if hit and hit.source == "greenhouse" and hit.slug and hit.posting_id:
            try:
                name = greenhouse.board_name(hit.slug) or _slug_name(hit.slug)
            except http.CareersError:
                name = _slug_name(hit.slug)
            row = greenhouse.fetch_posting(hit.slug, hit.posting_id, name)
        elif hit and hit.source == "lever" and hit.slug and hit.posting_id:
            row = lever.fetch_posting(hit.slug, hit.posting_id, _slug_name(hit.slug), hit.region)
        elif hit and hit.source == "ashby" and hit.slug and hit.posting_id:
            pid = hit.posting_id.lower()
            rows = [r for r in ashby.list_rest(hit.slug, _slug_name(hit.slug))
                    if pid in r["url"].lower()]
            if not rows:
                raise IngestError(f"ERROR: posting {hit.posting_id} not found on Ashby board "
                                  f"{hit.slug!r} — the role may have closed, or the URL is wrong.")
            row = rows[0]
        else:
            if not company or not title:
                raise IngestError(
                    "ERROR: this URL is not a recognized ATS posting (Greenhouse / Lever / "
                    "Ashby), so company and title cannot be extracted reliably — they define "
                    "the job_id hash and are never guessed. Re-run with --company \"...\" "
                    "--title \"...\", or --dry-run to print the page text first.")
            row = S.make_posting(source="manual", company=company, title=title, url=url,
                                 jd_text=fetch_page_text(url))
    except http.CareersError as exc:
        raise IngestError(f"ERROR: {exc}") from exc
    if row is None:
        raise IngestError(f"ERROR: could not parse the posting at {url} — check the URL.")
    if company or title:
        row = S.make_posting(**{**row, "company": company or row["company"],
                                "title": title or row["title"]})
    return row


def fetch_page_text(url: str) -> str:
    try:
        status, body = http.fetch_page(url)
    except Exception as exc:  # noqa: BLE001 -- any transport failure is one message
        raise IngestError(f"ERROR: fetch failed: {url}: {exc}") from exc
    if status != 200:
        raise IngestError(f"ERROR: HTTP {status} fetching {url} — the posting may be gone "
                          f"or behind a login.")
    return N.html_to_text(body)


def ingest(url: str, *, vertical: str | None = None, company: str | None = None,
           title: str | None = None, jobs_dir=None) -> dict:
    """Fetch, save into the store, rebuild clean.parquet, return the row."""
    from src.discovery.location import Allowlist
    from src.discovery.run import Paths, rebuild_view
    from src.discovery.sources.boards import BOARDS_DIR

    p = Paths(jobs=jobs_dir or paths.JOBS, boards_dir=BOARDS_DIR)
    if not p.store.exists():
        raise IngestError(f"ERROR: {p.store} does not exist yet. Run "
                          f"`uv run python scripts/migrate_store.py --apply` first.")
    row = fetch_row(url, company=company, title=title)
    jd = row["jd_text"].strip()
    if len(jd) < N.MIN_JD_CHARS:
        raise IngestError(
            f"ERROR: JD text extracted from {url} is {len(jd)} chars — below the "
            f"{N.MIN_JD_CHARS}-char floor. The page may be JS-rendered or the posting closed. "
            f"Paste the JD into inbox/ as a manual clip instead.")
    vcfg = verticals.get_config()
    lane = vertical or N.classify_vertical_from_title(row["title"])
    if lane not in vcfg.verticals:
        names = sorted(vcfg.verticals)
        raise IngestError(f"ERROR: vertical {vertical!r} is not configured. Configured verticals: {names}"
                          if vertical else
                          f"ERROR: the title did not classify into any configured vertical and none "
                          f"was given. Re-run with --vertical, one of: {names}")
    cfg = load_config()
    if not Allowlist(cfg).check(row["location_raw"], lane).keep:
        raise IngestError(f"ERROR: location {row['location_raw']!r} is outside location_allowlist "
                          f"in profile/discovery.yaml — widen it there, or this row cannot enter "
                          f"clean.parquet.")
    # A deliberate add: exempt from the title gate like an inbox clip.
    row.update(vertical=lane, source="manual")
    today = pd.Timestamp.today().normalize()
    state_index = load_state_index(p.pipeline)
    batch = S.Batch()
    batch.upsert(row)
    stats = S.flush(batch, today, frozenset(state_index), cfg.live_days, path=p.store)
    rebuild_view(cfg, p, today, state_index)

    store = S.load_store(p.store)
    job_id = store.set_index("posting_key").at[row["posting_key"], "job_id"]
    clean = pd.read_parquet(p.clean).set_index("job_id")
    if job_id not in clean.index:
        raise IngestError(f"ERROR: row for {row['company']!r} / {row['title']!r} did not survive "
                          f"cleaning (job_id {job_id} absent from clean.parquet).")
    kept = clean.loc[job_id]
    attached = bool(stats.refreshed) or any(k != "hash" for k in stats.by_rule)
    return {"job_id": job_id, "attached": attached,
            **{c: str(kept[c]) for c in ("company", "title", "vertical", "url", "source",
                                         "location", "jd_text")}}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    parser = argparse.ArgumentParser(prog="ingest-url", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url")
    parser.add_argument("--vertical", default=None)
    parser.add_argument("--company", default=None)
    parser.add_argument("--title", default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="print company/title/text and exit; writes nothing")
    args = parser.parse_args(argv)
    try:
        if args.dry_run:
            if parse_posting_url(args.url) is None:
                info = {"company": "", "title": "", "jd_text": fetch_page_text(args.url)}
            else:
                info = fetch_row(args.url)
            print(f"company: {info['company']}\ntitle: {info['title']}\n"
                  f"chars: {len(info['jd_text'])}\n--- text ---\n{info['jd_text']}")
            return 0
        info = ingest(args.url, vertical=args.vertical, company=args.company, title=args.title)
    except IngestError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Ingested: {info['company']} — {info['title']} [{info['vertical']}] "
          f"(source: {info['source']})")
    if info["attached"]:
        print("note: this posting joined a job the store already had; that job's id is below")
    print(f"job_id: {info['job_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
