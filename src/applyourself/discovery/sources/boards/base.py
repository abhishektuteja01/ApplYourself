"""The board lanes: which boards to check tonight, and the one loop that
checks them.

`data/universe/boards.csv` lists the boards as `ats,name,slug`, and
`boards_retired.csv` the spellings retired from it. `jobs/boards.parquet` holds
one row per board: when it was last checked, when it last had a job that
passed the title gate, and last check's counts. A board is due when
`every_days` have passed since its last check; due boards run oldest first.
A killed run leaves the rest due, so there is nothing to resume.

Deterministic and LLM-free (R7). Vendor code lives in the modules next to
this one; each only lists a board and fetches one posting.
"""

from __future__ import annotations

import csv
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from applyourself.discovery import parse
from applyourself.discovery.config import pacing_for
from applyourself.discovery.sources.lane import Lane, LaneStats
from applyourself.shared import paths, schemas, title_gate
from applyourself.shared.http_client import CareersError, Pacer
from applyourself.shared.job_boards import ATS_SOURCE_NAMES, key_of
from applyourself.shared.parquet_io import locked, read_parquet, write_parquet

log = logging.getLogger(__name__)


# Three 404s in a row and a board is `dead`: checked every `dead_every_days`
# instead, until it answers again.
DEAD_AFTER_404S = 3

# A 200 can decode to anything. One board's odd payload is that board's
# problem; every board failing the same way is a parser that no longer fits
# the API, and the lane stops rather than reporting an empty crawl.
PAYLOAD_SHAPE_ERRORS = (AttributeError, TypeError, KeyError, ValueError, IndexError)
ESCALATE_MIN_BOARDS = 10
ESCALATE_FAILURE_RATE = 0.9

STATE_COLUMNS = [
    "ats",
    "slug",
    "name",
    "last_checked",
    "last_found",
    "status",
    "listed",
    "passed",
    "new",
    "strikes",
]


@dataclass(frozen=True)
class Board:
    ats: str
    name: str
    slug: str

    @property
    def key(self) -> str:
        return key_of(self.ats, self.slug)


# ---------------------------------------------------------------------
# the board list
# ---------------------------------------------------------------------


def search_terms(vcfg) -> tuple[str, ...]:
    """Each lane's first search term, default lane first, duplicates once:
    what a board that filters server-side is searched with."""
    order = [vcfg.default_vertical] + [n for n in vcfg.verticals if n != vcfg.default_vertical]
    terms: list[str] = []
    for name in order:
        lane = vcfg.verticals.get(name)
        if lane is not None and lane.search_terms and lane.search_terms[0] not in terms:
            terms.append(lane.search_terms[0])
    return tuple(terms)


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def load_boards(
    ats: str | None = None, include_retired: bool = False, boards_dir: Path | None = None
) -> list[Board]:
    """Every live board, one per (ats, slug) whatever the slug's case.

    `boards.csv` is the hand list; a spelling in `boards_retired.csv` (or a
    row with `active=0`) is retired, and stays retired through a re-add. A
    board listed in two cases stays live while any spelling is; the first
    live spelling is the one used, and the last hand-list row the name."""
    boards_dir = Path(boards_dir or paths.UNIVERSE)
    spellings: dict[str, dict[str, bool]] = {}
    names: dict[str, str] = {}
    listed = [(row, False) for row in _read_csv(boards_dir / "boards.csv")]
    retired_rows = [(row, True) for row in _read_csv(boards_dir / "boards_retired.csv")]
    for row, from_retired in listed + retired_rows:
        a = (row.get("ats") or "").strip().lower()
        name = (row.get("name") or "").strip()
        slug = (row.get("slug") or "").strip()
        if a not in ATS_SOURCE_NAMES or not name or not slug:
            continue
        key = key_of(a, slug)
        retired = spellings.setdefault(key, {}).get(slug, False)
        spellings[key][slug] = retired or from_retired or (row.get("active") or "").strip() == "0"
        if from_retired:
            names.setdefault(key, name)
        else:
            names[key] = name
    boards = []
    for key, spots in spellings.items():
        live = [s for s, retired in spots.items() if not retired]
        if live or include_retired:
            boards.append(Board(key.split("|", 1)[0], names[key], (live or list(spots))[0]))
    if ats:
        boards = [b for b in boards if b.ats == ats]
    return sorted(boards, key=lambda b: b.key)


def load_state(path: Path | None = None) -> pd.DataFrame:
    path = path or paths.BOARD_STATE
    if not Path(path).exists():
        return pd.DataFrame(columns=STATE_COLUMNS)
    df = read_parquet(path, schemas.BOARD_STATE_SCHEMA_VERSION).reindex(columns=STATE_COLUMNS)
    for col in ("last_checked", "last_found"):
        df[col] = pd.to_datetime(df[col], errors="coerce")
    for col in ("listed", "passed", "new", "strikes"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    return df


def due_boards(
    boards: list[Board],
    state: pd.DataFrame,
    today: pd.Timestamp,
    every_days: int,
    dead_every_days: int,
) -> list[Board]:
    """Boards due tonight, never-checked and oldest-checked first."""
    today = pd.Timestamp(today).normalize()
    known = {
        key_of(a, str(s)): r
        for a, s, r in zip(state["ats"], state["slug"], state.to_dict("records"), strict=True)
    }
    due = []
    for b in boards:
        rec = known.get(b.key)
        last = rec["last_checked"] if rec else pd.NaT
        if pd.isna(last):
            due.append((pd.Timestamp.min, b))
            continue
        wait = dead_every_days if rec["status"] == "dead" else every_days
        if (today - last).days >= wait:
            due.append((last, b))
    due.sort(key=lambda t: (t[0], t[1].key))
    return [b for _, b in due]


@dataclass
class BoardResult:
    board: Board
    status: str  # ok | empty | 404 | error
    listed: int = 0
    passed: int = 0
    new: int = 0


def update_state(results: list[BoardResult], today: pd.Timestamp, path: Path | None = None) -> None:
    """Fold a batch of board results into boards.parquet, under its lock.
    An error leaves last_checked alone, so the board is first in line next
    run; a 404 counts a strike."""
    path = path or paths.BOARD_STATE
    if not results:
        return
    today = pd.Timestamp(today).normalize()
    with locked(path):
        state = load_state(path)
        index = {
            key_of(a, str(s)): i
            for i, (a, s) in enumerate(zip(state["ats"], state["slug"], strict=True))
        }
        new_rows = []
        for r in results:
            b = r.board
            i = index.get(b.key)
            if i is None:
                new_rows.append(
                    {
                        "ats": b.ats,
                        "slug": b.slug,
                        "name": b.name,
                        "last_checked": pd.NaT,
                        "last_found": pd.NaT,
                        "status": "",
                        "listed": 0,
                        "passed": 0,
                        "new": 0,
                        "strikes": 0,
                    }
                )
                index[b.key] = len(state) + len(new_rows) - 1
        if new_rows:
            fresh = pd.DataFrame(new_rows)
            state = fresh if state.empty else pd.concat([state, fresh], ignore_index=True)
        for r in results:
            i = index[r.board.key]
            state.at[i, "name"] = r.board.name
            if r.status == "error":
                state.at[i, "status"] = "error"
                continue
            state.at[i, "last_checked"] = today
            if r.status == "404":
                strikes = int(state.at[i, "strikes"]) + 1
                state.at[i, "strikes"] = strikes
                state.at[i, "status"] = "dead" if strikes >= DEAD_AFTER_404S else "404"
                continue
            state.at[i, "strikes"] = 0
            state.at[i, "status"] = r.status
            state.at[i, "listed"], state.at[i, "passed"], state.at[i, "new"] = (
                r.listed,
                r.passed,
                r.new,
            )
            if r.passed:
                state.at[i, "last_found"] = today
        state = state.sort_values(["ats", "slug"], kind="stable").reset_index(drop=True)
        write_parquet(state[STATE_COLUMNS], path, schema_version=schemas.BOARD_STATE_SCHEMA_VERSION)


# ---------------------------------------------------------------------
# the one loop
# ---------------------------------------------------------------------


class Crawl:
    """What a lane needs from the run: the clock, the store's known keys, and
    the places results go. Shared by every lane thread."""

    def __init__(
        self,
        cfg,
        vcfg,
        today,
        deadline_ts: float,
        known: dict[str, dict],
        batch,
        board_state: Path | None = None,
        boards_dir: Path | None = None,
    ):
        self.cfg = cfg
        self.vcfg = vcfg
        self.today = pd.Timestamp(today).normalize()
        self.deadline_ts = deadline_ts
        self.known = known
        self.batch = batch
        self.board_state = board_state or paths.BOARD_STATE
        self.boards_dir = boards_dir or paths.UNIVERSE
        self._lock = threading.Lock()
        self.board_results: list[BoardResult] = []
        self.listed: list[dict] = []
        # Boards whose postings tonight's feed already carried: not crawled.
        self.covered: frozenset[str] = frozenset()

    def deadline_reached(self) -> bool:
        return bool(self.deadline_ts) and time.time() > self.deadline_ts

    def record_board(self, result: BoardResult) -> None:
        with self._lock:
            self.board_results.append(result)

    def take_board_results(self) -> list[BoardResult]:
        with self._lock:
            out, self.board_results = self.board_results, []
            return out

    def record_listing(
        self, source: str, title: str, vertical: str, passed: bool, key: str
    ) -> None:
        """One classified listing for tonight's debug shard."""
        with self._lock:
            self.listed.append(
                {
                    "source": source,
                    "title": title,
                    "vertical": vertical,
                    "passed": passed,
                    "posting_key": key,
                }
            )

    def take_listed(self) -> list[dict]:
        with self._lock:
            out, self.listed = self.listed, []
            return out


class BoardLane(Lane):
    """One ATS. A subclass lists a board and, when the list lacks a JD,
    fetches one posting. Everything else is here, once."""

    name = ""
    # Whether a list row is as good as the stored copy (bar the JD). Workday's
    # list location is a summary ("3 Locations"), so it only touches.
    list_refreshes = True

    def list_board(self, board: Board, crawl: Crawl, pause) -> list[dict]:
        """make_posting rows for every posting the board lists. Raises
        CareersError or a payload-shape error."""
        raise NotImplementedError

    def detail(self, board: Board, posting: dict, crawl: Crawl) -> dict | None:
        """The posting with its JD filled in, or None. Only called for a
        posting the list gave no JD for."""
        return None

    def stale(self, posting: dict, known: dict) -> bool:
        """Whether a known posting's stored copy is out of date."""
        return False

    def paged_search(
        self, crawl: Crawl, pause, page_size: int, fetch_page, to_posting
    ) -> list[dict]:
        """A board that filters server-side: each search term, up to `max_pages`
        pages. A term stops on a short page or a page with nothing past the
        title gate; everything stops at the deadline. `fetch_page(term, offset)`
        returns the page's items, `to_posting(item, term)` a posting or None."""
        max_pages = crawl.cfg.sources[self.name].max_pages
        out: dict[str, dict] = {}
        first = True
        for term in search_terms(crawl.vcfg):
            for page in range(max_pages):
                if crawl.deadline_reached():
                    return list(out.values())
                if not first:
                    pause()
                first = False
                items = fetch_page(term, page * page_size)
                passing = 0
                for item in items:
                    p = to_posting(item, term)
                    if p is None or p["posting_key"] in out:
                        continue
                    out[p["posting_key"]] = p
                    passing += title_gate.lane_for(p["title"], "", self.name, crawl.vcfg).passes
                if len(items) < page_size or not passing:
                    break
        return list(out.values())

    def run(self, crawl: Crawl, tally=None) -> LaneStats:
        src = crawl.cfg.sources[self.name]
        pause = Pacer(pacing_for(crawl.cfg, self.name)).wait
        state = load_state(crawl.board_state)
        boards = due_boards(
            load_boards(self.name, boards_dir=crawl.boards_dir),
            state,
            crawl.today,
            src.every_days,
            crawl.cfg.dead_every_days,
        )
        # A covered board is not marked checked, so it is first in line on a
        # night the feed is missing.
        due = len(boards)
        boards = [b for b in boards if b.key not in crawl.covered]
        stats = LaneStats(boards_due=len(boards))
        if due > len(boards):
            stats.notes.append(f"{due - len(boards)} due boards left to the feed")
        shape_errors = 0
        for board in boards:
            if crawl.deadline_reached():
                stats.truncated = True
                break
            stats.boards_checked += 1
            try:
                pause()
                postings = self.list_board(board, crawl, pause)
            except CareersError as e:
                is_404 = e.status == 404 and e.permanent
                stats.boards_404 += is_404
                stats.boards_error += not is_404
                if not is_404:
                    stats.errors.append(f"{board.name}: {e}")
                crawl.record_board(BoardResult(board, "404" if is_404 else "error"))
                continue
            except PAYLOAD_SHAPE_ERRORS as e:
                shape_errors += 1
                stats.boards_error += 1
                stats.errors.append(
                    f"{board.name}: malformed board payload: {type(e).__name__}: {e}"
                )
                crawl.record_board(BoardResult(board, "error"))
                if (
                    stats.boards_checked >= ESCALATE_MIN_BOARDS
                    and shape_errors / stats.boards_checked >= ESCALATE_FAILURE_RATE
                ):
                    raise RuntimeError(
                        f"{self.name}: {shape_errors}/{stats.boards_checked} boards unparseable; "
                        f"the API shape has changed"
                    ) from e
                continue
            result = BoardResult(board, "ok", listed=len(postings))
            for posting in postings:
                self._one(board, posting, crawl, stats, result, pause)
            if not result.passed:
                result.status = "empty"
            stats.boards_ok += result.status == "ok"
            stats.boards_empty += result.status == "empty"
            crawl.record_board(result)
        return stats

    def _one(self, board, posting, crawl, stats, result, pause) -> None:
        stats.listed += 1
        verdict = title_gate.lane_for(posting["title"], "", self.name, crawl.vcfg)
        vertical = verdict.lane
        key = posting["posting_key"]
        if vertical:
            stats.classified += 1
            crawl.record_listing(self.name, posting["title"], vertical, verdict.passes, key)
        if not verdict.passes:
            return
        posting["vertical"] = vertical
        stats.passed += 1
        result.passed += 1
        has_jd = len(posting["jd_text"].strip()) >= parse.MIN_JD_CHARS
        known = crawl.known.get(key)
        if known is not None:
            stats.known += 1
            if has_jd:
                # The list carries the JD, so a known posting refreshes for free.
                crawl.batch.upsert(posting)
            elif self.stale(posting, known) and not crawl.deadline_reached():
                full = self._fetch(board, posting, crawl, stats, pause)
                crawl.batch.upsert(full) if full else crawl.batch.touch(key)
            elif self.list_refreshes:
                # A refresh without a JD keeps the stored JD and takes the
                # list's dates, location and updated_at.
                crawl.batch.upsert(posting)
            else:
                crawl.batch.touch(key)
            return
        stats.new += 1
        result.new += 1
        full = posting
        if not has_jd:
            if crawl.deadline_reached():
                return
            full = self._fetch(board, posting, crawl, stats, pause)
            if full is None:
                return
        if len(full["jd_text"].strip()) < parse.MIN_JD_CHARS:
            # Not stored: the next check tries again.
            stats.short_jd += 1
            return
        crawl.known[key] = {"updated_at": full.get("updated_at", "")}
        crawl.batch.upsert(full)

    def _fetch(self, board, posting, crawl, stats, pause) -> dict | None:
        try:
            pause()
            full = self.detail(board, posting, crawl)
        except (CareersError, *PAYLOAD_SHAPE_ERRORS) as e:
            stats.detail_failed += 1
            stats.errors.append(f"{board.name}: {posting['title']!r}: {e}")
            return None
        if full is None:
            stats.detail_failed += 1
            return None
        stats.detail_ok += 1
        return full


def job_items(payload, key: str) -> list[dict]:
    """Dict items under `key`. A payload of the wrong shape raises, so a
    garbage 200 is reported as a malformed board, not an empty one."""
    if not isinstance(payload, dict):
        raise TypeError(f"expected a JSON object, got {type(payload).__name__}")
    items = payload.get(key)
    if not isinstance(items, list):
        raise TypeError(f"expected a list under {key!r}, got {type(items).__name__}")
    return [i for i in items if isinstance(i, dict)]
