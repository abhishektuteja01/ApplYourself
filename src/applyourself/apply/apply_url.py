"""Where a role can be applied to: the employer's own posting behind an
aggregator listing, from stored data only.

`resolve()` reads, in order, the `apply_url` that `apply resolve-urls` stored
in `jobs/apply_urls.json`, the role's own posting urls, then the board links
its JD carries, and makes no network call. Each role ends `submittable` (a
board `/apply` drives), `manual` (a real posting, applied to by hand) or
`no_url` (an aggregator listing with no employer posting known), with the
reason. `apply resolve-urls --apply` is the one network step: it looks for the
title on the company's own board in `data/universe/boards.csv`.

Deterministic and LLM-free (R7).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

from applyourself.shared import clock, paths
from applyourself.shared.files import atomic_write_text
from applyourself.shared.identity import normalize_company, normalize_title
from applyourself.shared.job_boards import SUBMITTABLE_SOURCES, detect_source
from applyourself.shared.parquet_io import locked

SUBMITTABLE, MANUAL, NO_URL = "submittable", "manual", "no_url"
STATUS = {MANUAL: "Manual apply", NO_URL: "No apply URL"}
_LINK_RE = re.compile(r"https?://[^\s<>\"'()\[\]\\]+")
_AGGREGATORS = ("linkedin", "indeed")  # job sites whose page is a listing, not a posting
MATCHER = 3  # bump when the title match changes: older stored misses are looked up again


@dataclass(frozen=True)
class Resolved:
    url: str
    kind: str
    reason: str

    @property
    def status(self) -> str:
        return STATUS.get(self.kind, "")


def _aggregator(url: str) -> str:
    """The job site whose own listing this url is, or ""."""
    try:
        labels = (urlsplit(url).hostname or "").lower().split(".")
    except ValueError:
        return ""
    return next((a for a in _AGGREGATORS if a in labels), "")


def jd_link(jd_text: str | None) -> str:
    """The first link in a JD that is a posting on a board `/apply` drives."""
    for link in _LINK_RE.findall(jd_text or ""):
        link = link.rstrip(".,;:")
        if detect_source(link) in SUBMITTABLE_SOURCES:
            return link
    return ""


def load(path=None) -> dict[str, dict]:
    """job_id -> {apply_url, via, checked}; {} when nothing is stored."""
    path = path or paths.APPLY_URLS
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve(
    urls: Iterable[str | None], jd_text: str | None = "", stored: dict | None = None
) -> Resolved:
    """The url to apply through and why, from what is already on disk."""
    stored = stored or {}
    found = str(stored.get("apply_url") or "")
    if detect_source(found) in SUBMITTABLE_SOURCES:
        return Resolved(found, SUBMITTABLE, f"found {stored.get('via', 'by resolve-urls')}")
    known = [str(u or "").strip() for u in urls if str(u or "").strip()]
    for url in known:
        if detect_source(url) in SUBMITTABLE_SOURCES:
            return Resolved(url, SUBMITTABLE, "")
    link = jd_link(jd_text)
    if link:
        return Resolved(link, SUBMITTABLE, "link in the JD")
    checked = f"; not on {stored['via']} ({stored.get('checked', '')})" if stored.get("via") else ""
    for url in known:
        ats = detect_source(url)
        if ats:
            return Resolved(url, MANUAL, f"{ats} posting; /apply has no driver for it{checked}")
    for url in known:
        if not _aggregator(url):
            return Resolved(url, MANUAL, f"careers page, no known board{checked}")
    where = _aggregator(known[0]) if known else ""
    why = f"{where} listing; the employer's posting is unknown" if where else "no posting url"
    return Resolved("", NO_URL, why + checked)


def role_candidates(job_id: str, state: dict | None) -> tuple[list[tuple[str, str]], str]:
    """(where, url) for every url on disk for this role, and its JD text:
    clean.parquet's row, then state.yaml's url."""
    from applyourself.shared import schemas
    from applyourself.shared.parquet_io import read_parquet

    candidates: list[tuple[str, str]] = []
    jd = ""
    if paths.CLEAN.exists():
        clean = read_parquet(
            paths.CLEAN, schemas.CLEAN_SCHEMA_VERSION, columns=["job_id", "url", "jd_text"]
        )
        row = clean.loc[clean["job_id"] == job_id]
        if not row.empty:
            candidates.append(("clean.parquet", str(row.iloc[0]["url"] or "")))
            jd = str(row.iloc[0]["jd_text"] or "")
    if state:
        candidates.append(("state.yaml", str(state.get("url") or "")))
    return candidates, jd


def form_key(job_id: str, state: dict | None) -> str:
    """`ledger.url_key` of the form this role would be filled through, or ""
    when it has none: a retitled role keeps its form but gets a new id."""
    from applyourself.apply import ashby, greenhouse, ledger, lever
    from applyourself.apply.detect import detect_ats
    from applyourself.apply.greenhouse import ApplyUrlError

    candidates, jd = role_candidates(job_id, state)
    if not candidates:
        return ""
    found = resolve([u for _, u in candidates], jd, load().get(job_id))
    if found.kind != SUBMITTABLE:
        return ""
    parsers = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}
    module = parsers.get(detect_ats(found.url) or "")
    try:
        return ledger.url_key(module.parse_posting(found.url).form_url) if module else ""
    except ApplyUrlError:
        return ""


# ---------------------------------------------------------------------
# apply resolve-urls: the opt-in network step
# ---------------------------------------------------------------------


def boards_by_company(boards) -> dict[str, list]:
    """normalize_company(name) -> the company's boards `/apply` can submit to."""
    out: dict[str, list] = {}
    for b in boards:
        if b.ats in SUBMITTABLE_SOURCES:
            out.setdefault(normalize_company(b.name), []).append(b)
    return out


def list_board(board) -> list[dict]:
    """One board's postings, through discovery's own lane."""
    from types import SimpleNamespace
    from typing import Any, cast

    from applyourself.discovery.sources.boards.ashby import AshbyLane
    from applyourself.discovery.sources.boards.greenhouse import GreenhouseLane
    from applyourself.discovery.sources.boards.lever import LeverLane

    lane = {"greenhouse": GreenhouseLane, "lever": LeverLane, "ashby": AshbyLane}[board.ats]()
    return lane.list_board(board, cast(Any, SimpleNamespace(deadline_ts=None)), lambda: None)


def needs_lookup(stored: dict | None) -> bool:
    """Not stored yet, or a miss from an older title match."""
    return not stored or (not stored.get("apply_url") and stored.get("matcher") != MATCHER)


def _match(postings: list[dict], title: str, company: str = "") -> str:
    """The posting whose normalized title is the role's, else the one posting
    whose title differs only in words `titles_differ(loose=True)` ignores; ""
    if none or several."""
    from applyourself.discovery.dedupe import titles_differ

    exact = next((p["url"] for p in postings if normalize_title(p["title"]) == title), "")
    if exact:
        return exact
    role = {"title_normalized": title, "company_normalized": company}
    loose = [
        p["url"]
        for p in postings
        if not titles_differ(
            role,
            {
                "title_normalized": normalize_title(p["title"]),
                "location_raw": p.get("location_raw", ""),
            },
            loose=True,
        )
    ]
    loose = list(dict.fromkeys(loose))
    return loose[0] if len(loose) == 1 else ""


def find_on_boards(
    rows: list[dict],
    by_company: dict[str, list],
    fetch: Callable[[object], list[dict]] = list_board,
    pause: float = 1.0,
) -> tuple[dict[str, dict], list[str]]:
    """For each row ({job_id, company, title}), the posting on its company's
    board whose title matches (`_match`). Returns (job_id -> entry, errors); a
    checked row with no match gets an entry with an empty `apply_url` and the
    `matcher` that missed."""
    listed: dict[str, list[dict] | None] = {}
    out: dict[str, dict] = {}
    errors: list[str] = []
    today = clock.today().isoformat()
    for row in rows:
        boards = by_company.get(normalize_company(row["company"]), [])
        title = normalize_title(row["title"])
        hit, tried = "", []
        for b in boards:
            if b.key not in listed:
                if listed:
                    time.sleep(pause)
                try:
                    listed[b.key] = fetch(b)
                except Exception as e:  # noqa: BLE001 - one board's error never stops the run
                    listed[b.key] = None
                    errors.append(f"{b.key}: {e}")
            postings = listed[b.key]
            if postings is None:
                continue
            tried.append(f"{b.ats} board {b.slug}")
            hit = _match(postings, title, normalize_company(row["company"]))
            if hit:
                out[row["job_id"]] = {"apply_url": hit, "via": tried[-1], "checked": today}
                break
        if not hit and tried:
            out[row["job_id"]] = {
                "apply_url": "",
                "via": ", ".join(tried),
                "checked": today,
                "matcher": MATCHER,
            }
    return out, errors


def save(entries: dict[str, dict], path=None) -> None:
    """Merge entries into the stored file (tmp file, then rename)."""
    path = path or paths.APPLY_URLS
    with locked(path):
        data = {**load(path), **entries}
        atomic_write_text(path, json.dumps(data, indent=1, sort_keys=True) + "\n")
