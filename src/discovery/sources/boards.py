"""The board lanes: which boards to check tonight, and the one loop that
checks them.

`data/universe/boards.csv` (tracked) and `boards.local.csv` (gitignored,
yours) list the boards as `ats,name,slug,active`. `jobs/boards.parquet` holds
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
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from src import paths
from src.ats_http import CareersError
from src.ats_registry import ATS_SOURCE_NAMES, parse_posting_url
from src.discovery import normalize as N
from src.discovery.config import pacing_floor
from src.parquet_io import locked, write_parquet

log = logging.getLogger(__name__)

BOARDS_DIR = paths.REPO_ROOT / "data" / "universe"
BOARD_STATE = paths.BOARD_STATE

# Three 404s in a row and a board is `dead`: checked every `dead_every_days`
# instead, until it answers again.
DEAD_AFTER_404S = 3

# A 200 can decode to anything. One board's odd payload is that board's
# problem; every board failing the same way is a parser that no longer fits
# the API, and the lane stops rather than reporting an empty crawl.
PAYLOAD_SHAPE_ERRORS = (AttributeError, TypeError, KeyError, ValueError, IndexError)
ESCALATE_MIN_BOARDS = 10
ESCALATE_FAILURE_RATE = 0.9

STATE_COLUMNS = ["ats", "slug", "name", "last_checked", "last_found", "status",
                 "listed", "passed", "new", "strikes"]


@dataclass(frozen=True)
class Board:
    ats: str
    name: str
    slug: str

    @property
    def key(self) -> str:
        return f"{self.ats}|{self.slug.lower()}"


def board_key_of(url: str) -> str:
    """The `Board.key` of the board a posting url belongs to, or "". A Workday
    key carries the pod and site, as its slug does."""
    hit = parse_posting_url(url)
    if hit is None or not hit.slug:
        return ""
    if hit.source == "workday":
        return f"workday|{hit.slug}|{hit.pod}|{hit.site_id}".lower() if hit.pod and hit.site_id else ""
    return f"{hit.source}|{hit.slug}".lower()


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


def load_boards(ats: str | None = None, include_retired: bool = False,
                boards_dir: Path | None = None) -> list[Board]:
    """Every live board, one per (ats, slug) whatever the slug's case.

    `active=0` retires that exact spelling, in either file, and outlives a
    re-add of it. `active=1` in your local file is a deliberate choice and
    overrides a retirement. A board listed in two cases stays live while any
    spelling is; the live spelling is the one used. Your local file wins the
    name."""
    boards_dir = Path(boards_dir or BOARDS_DIR)
    spellings: dict[str, dict[str, dict]] = {}
    names: dict[str, str] = {}
    for local, path in ((False, boards_dir / "boards.csv"), (True, boards_dir / "boards.local.csv")):
        for row in _read_csv(path):
            a = (row.get("ats") or "").strip().lower()
            name = (row.get("name") or "").strip()
            slug = (row.get("slug") or "").strip()
            if a not in ATS_SOURCE_NAMES or not name or not slug:
                continue
            key = f"{a}|{slug.lower()}"
            active = (row.get("active") or "").strip()
            spot = spellings.setdefault(key, {}).setdefault(slug, {"retired": False, "forced": False})
            spot["retired"] |= active == "0"
            spot["forced"] |= local and active == "1"
            names[key] = name
    boards = []
    for key, spots in spellings.items():
        a = key.split("|", 1)[0]
        live = [s for s, f in spots.items() if f["forced"]] or \
               [s for s, f in spots.items() if not f["retired"]]
        if live or include_retired:
            boards.append(Board(a, names[key], (live or list(spots))[0]))
    if ats:
        boards = [b for b in boards if b.ats == ats]
    return sorted(boards, key=lambda b: b.key)


def load_state(path: Path | None = None) -> pd.DataFrame:
    path = path or BOARD_STATE
    if not Path(path).exists():
        return pd.DataFrame(columns=STATE_COLUMNS)
    df = pd.read_parquet(path).reindex(columns=STATE_COLUMNS)
    for col in ("last_checked", "last_found"):
        df[col] = pd.to_datetime(df[col], errors="coerce")
    for col in ("listed", "passed", "new", "strikes"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    return df


def due_boards(boards: list[Board], state: pd.DataFrame, today: pd.Timestamp,
               every_days: int, dead_every_days: int) -> list[Board]:
    """Boards due tonight, never-checked and oldest-checked first."""
    today = pd.Timestamp(today).normalize()
    known = {f"{a}|{str(s).lower()}": r for a, s, r in
             zip(state["ats"], state["slug"], state.to_dict("records"))}
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
    status: str          # ok | empty | 404 | error
    listed: int = 0
    passed: int = 0
    new: int = 0


def update_state(results: list[BoardResult], today: pd.Timestamp,
                 path: Path | None = None) -> None:
    """Fold a batch of board results into boards.parquet, under its lock.
    An error leaves last_checked alone, so the board is first in line next
    run; a 404 counts a strike."""
    path = path or BOARD_STATE
    if not results:
        return
    today = pd.Timestamp(today).normalize()
    with locked(path):
        state = load_state(path)
        index = {f"{a}|{str(s).lower()}": i for i, (a, s) in
                 enumerate(zip(state["ats"], state["slug"]))}
        new_rows = []
        for r in results:
            b = r.board
            i = index.get(b.key)
            if i is None:
                new_rows.append({"ats": b.ats, "slug": b.slug, "name": b.name,
                                 "last_checked": pd.NaT, "last_found": pd.NaT,
                                 "status": "", "listed": 0, "passed": 0, "new": 0,
                                 "strikes": 0})
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
            state.at[i, "listed"], state.at[i, "passed"], state.at[i, "new"] = \
                r.listed, r.passed, r.new
            if r.passed:
                state.at[i, "last_found"] = today
        state = state.sort_values(["ats", "slug"], kind="stable").reset_index(drop=True)
        write_parquet(state[STATE_COLUMNS], path)


# ---------------------------------------------------------------------
# the one loop
# ---------------------------------------------------------------------

@dataclass
class LaneStats:
    """What one lane saw, for the run report. Every count after `listed` is
    a subset of the one before it."""

    boards_due: int = 0
    boards_checked: int = 0
    boards_ok: int = 0
    boards_empty: int = 0
    boards_404: int = 0
    boards_error: int = 0
    listed: int = 0
    classified: int = 0
    passed: int = 0
    known: int = 0
    new: int = 0
    detail_ok: int = 0
    detail_failed: int = 0
    short_jd: int = 0
    errors: list[str] = field(default_factory=list)
    truncated: bool = False
    notes: list[str] = field(default_factory=list)
    capped: list[str] = field(default_factory=list)  # queries that hit results_wanted


class Crawl:
    """What a lane needs from the run: the clock, the store's known keys, and
    the places results go. Shared by every lane thread."""

    def __init__(self, cfg, vcfg, today, deadline_ts: float, known: dict[str, dict], batch,
                 board_state: Path | None = None, boards_dir: Path | None = None):
        self.cfg = cfg
        self.vcfg = vcfg
        self.today = pd.Timestamp(today).normalize()
        self.deadline_ts = deadline_ts
        self.known = known
        self.batch = batch
        self.board_state = board_state or BOARD_STATE
        self.boards_dir = boards_dir or BOARDS_DIR
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

    def record_listing(self, source: str, title: str, vertical: str, passed: bool,
                       key: str) -> None:
        """One classified listing for tonight's debug shard."""
        with self._lock:
            self.listed.append({"source": source, "title": title, "vertical": vertical,
                                "passed": passed, "posting_key": key})

    def take_listed(self) -> list[dict]:
        with self._lock:
            out, self.listed = self.listed, []
            return out


class BoardLane:
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

    def run(self, crawl: Crawl) -> LaneStats:
        src = crawl.cfg.sources[self.name]
        pacing = max(pacing_floor(self.name), src.pacing_seconds)
        state = load_state(crawl.board_state)
        boards = due_boards(load_boards(self.name, boards_dir=crawl.boards_dir), state, crawl.today,
                            src.every_days, crawl.cfg.dead_every_days)
        # A covered board is not marked checked, so it is first in line on a
        # night the feed is missing.
        due = len(boards)
        boards = [b for b in boards if b.key not in crawl.covered]
        stats = LaneStats(boards_due=len(boards))
        if due > len(boards):
            stats.notes.append(f"{due - len(boards)} due boards left to the feed")
        shape_errors = 0
        first = [True]

        def pause() -> None:
            if not first[0]:
                time.sleep(pacing)
            first[0] = False

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
                stats.errors.append(f"{board.name}: malformed board payload: {type(e).__name__}: {e}")
                crawl.record_board(BoardResult(board, "error"))
                if (stats.boards_checked >= ESCALATE_MIN_BOARDS
                        and shape_errors / stats.boards_checked >= ESCALATE_FAILURE_RATE):
                    raise RuntimeError(
                        f"{self.name}: {shape_errors}/{stats.boards_checked} boards unparseable; "
                        f"the API shape has changed") from e
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
        vertical, passes = N.gate_reason(posting["title"], self.name, crawl.vcfg)
        key = posting["posting_key"]
        if vertical:
            stats.classified += 1
            crawl.record_listing(self.name, posting["title"], vertical, passes, key)
        if not passes:
            return
        posting["vertical"] = vertical
        stats.passed += 1
        result.passed += 1
        has_jd = len(posting["jd_text"].strip()) >= N.MIN_JD_CHARS
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
        if len(full["jd_text"].strip()) < N.MIN_JD_CHARS:
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
