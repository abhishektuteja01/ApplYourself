from __future__ import annotations

import logging
from dataclasses import dataclass, replace
import yaml
import pandas as pd
import csv
from datetime import timedelta

from src.discovery.sources.ats.registry import ATS_SOURCE_NAMES
from src.parquet_io import write_parquet
from src import paths

log = logging.getLogger(__name__)

REPO_ROOT = paths.REPO_ROOT
DEFAULT_COMPANIES_PATH = REPO_ROOT / "profile" / "companies.yaml"
CSV_DIR = REPO_ROOT / "data" / "universe"
# One ledger per ATS, not one shared file: HealthLedger.flush() does a full
# read-modify-write, and the three ATS sources run concurrently. A single file
# would race into lost updates. Derived from HEALTH_DIR rather
# than fixed per-ATS constants so tests redirect all three with one patch.
HEALTH_DIR = REPO_ROOT / "jobs"
_SCHEMA_VERSION = 1

# `last_kept_at` is the hot/cold split's whole input: a board that kept a row
# recently is worth polling every run, one that has not is worth a seventh of
# one. Distinct from `last_ok`, which only says the board answered, and from
# `last_yield`, which is overwritten to 0 by the next empty poll and so cannot
# say when a board last produced anything.
HEALTH_COLUMNS = ["ats", "slug", "consecutive_404s", "last_ok", "last_yield",
                  "pruned_at", "last_kept_at"]
# A board counts as hot for this long after the last row it kept.
HOT_WINDOW_DAYS = 30
# The cold tail is polled a slice at a time: full coverage every this many runs.
COLD_ROTATION_RUNS = 3
_NEW_HEALTH_ROW = {"consecutive_404s": 0, "last_ok": pd.NaT, "last_yield": 0,
                   "pruned_at": pd.NaT, "last_kept_at": pd.NaT}


def health_path(ats: str):
    return HEALTH_DIR / f"universe_health_{ats}.parquet"

@dataclass(frozen=True)
class UniverseCompany:
    name: str
    ats: str
    slug: str
    priority: bool = False
    last_kept_at: object = None
    """When this board last kept a row, from the health ledger. `None` for a
    board never polled, or one polled only before the column existed -- both
    read as cold, which costs a board one rotation and nothing more."""

class HealthLedger:
    """One ATS lane's health ledger, accumulated in memory and written once.

    A full read-modify-write per company cost ~4.3 ms and ~12,500 file rewrites
    a night. Marks are held here and applied in a single `flush()`.

    `known_slugs` is the lane's current universe, and must come from
    `universe_slugs()`, never from `load()`: `load()` hides a board for 14 days
    after it was pruned, so a ledger built from it deletes exactly the rows
    whose `pruned_at` caused the hiding, resetting the cooldown and the strike
    count every run. Slugs absent from `known_slugs` are dropped on flush;
    pass None to keep every row.
    """

    def __init__(self, ats: str, known_slugs=None):
        self.ats = ats
        self._known = None if known_slugs is None else set(known_slugs)
        # slug -> (success, rows). One mark per company per run, last wins.
        self._marks: dict[str, tuple[bool, int]] = {}
        self._pruned = self._known is None

    def mark_ok(self, slug: str, kept: int = 0) -> None:
        self._marks[slug] = (True, kept)

    def mark_dead(self, slug: str) -> None:
        """Counts a strike toward pruning; call it only for a board that is
        permanently dead, never for a transient fetch failure."""
        self._marks[slug] = (False, 0)

    def flush(self) -> None:
        """Apply the accumulated marks and prune orphans. Idempotent: a second
        call with nothing new writes nothing, so flushing on a deadline break
        and again at the end of `fetch` still costs one write."""
        if not self._marks and self._pruned:
            return

        path = health_path(self.ats)
        if path.exists():
            # Reindexed, because a ledger written before a column existed is
            # still on disk and every read below addresses columns by name.
            df = pd.read_parquet(path).reindex(columns=HEALTH_COLUMNS)
            # A column the file predates arrives all-NaN and typed float64.
            # The counters would read NaN into `+ 1`; the date columns would
            # reject a Timestamp assignment as an incompatible dtype.
            for col in ("consecutive_404s", "last_yield"):
                df[col] = df[col].fillna(0)
            for col in ("last_ok", "pruned_at", "last_kept_at"):
                df[col] = pd.to_datetime(df[col], errors="coerce")
        elif self._marks:
            df = pd.DataFrame(columns=HEALTH_COLUMNS)
        else:
            # Nothing learned and no file: a lane that polled nothing must not
            # create an empty ledger.
            self._pruned = True
            return

        today = pd.Timestamp.today().normalize()
        changed = False

        new_rows = []
        for slug in self._marks:
            if not ((df["ats"] == self.ats) & (df["slug"] == slug)).any():
                new_rows.append({"ats": self.ats, "slug": slug, **_NEW_HEALTH_ROW})
        if new_rows:
            # Concatenating onto an all-empty frame is deprecated in pandas and
            # errors under filterwarnings.
            new = pd.DataFrame(new_rows)
            df = new if df.empty else pd.concat([df, new], ignore_index=True)

        for slug, (success, rows) in self._marks.items():
            idx = df.index[(df["ats"] == self.ats) & (df["slug"] == slug)][0]
            if success:
                df.at[idx, "consecutive_404s"] = 0
                df.at[idx, "pruned_at"] = pd.NaT
                df.at[idx, "last_ok"] = today
                df.at[idx, "last_yield"] = rows
                if rows:
                    # Only a kept row moves this. An answering board with
                    # nothing for us is exactly what the cold tail is for.
                    df.at[idx, "last_kept_at"] = today
            else:
                c = df.at[idx, "consecutive_404s"] + 1
                df.at[idx, "consecutive_404s"] = c
                if c >= 3:
                    df.at[idx, "pruned_at"] = today
            changed = True

        if self._known is not None:
            # Foreign-ats rows are left alone: this ledger only knows its own
            # lane's universe.
            keep = (df["ats"] != self.ats) | df["slug"].isin(self._known)
            dropped = int((~keep).sum())
            if dropped:
                df = df[keep].reset_index(drop=True)
                changed = True
                log.info("universe: pruned %d orphan %s health rows", dropped, self.ats)
            self._pruned = True

        self._marks.clear()
        if changed:
            write_parquet(df, path)

def _load_csv(csv_path, ats: str, out: dict) -> None:
    """Merge a name,slug CSV into out, keyed by slug. An absent file is not an
    error: <ats>.local.csv is gitignored and never exists on a fresh clone."""
    if not csv_path.exists():
        return
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames:
                for row in reader:
                    name = (row.get("name") or "").strip()
                    slug = (row.get("slug") or "").strip()
                    if not name or not slug:
                        log.warning("universe: empty name or slug in CSV row, skipping")
                        continue
                    out[slug] = UniverseCompany(name=name, ats=ats, slug=slug, priority=False)
    except (OSError, ValueError, KeyError) as e:
        log.warning("universe: error reading %s: %s", csv_path, e)


def _universe_dict(ats: str) -> dict:
    """Every company this lane's CSVs and watchlist name, keyed by slug, with
    no health filtering at all."""
    companies_dict = {}

    # 1. Load CSVs, least authoritative first: on a slug in both files the
    #    curated name wins over the bulk one.
    _load_csv(CSV_DIR / f"{ats}.local.csv", ats, companies_dict)
    _load_csv(CSV_DIR / f"{ats}.csv", ats, companies_dict)

    # 2. Load Watchlist
    if DEFAULT_COMPANIES_PATH.exists():
        try:
            data = yaml.safe_load(DEFAULT_COMPANIES_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("schema_version") == _SCHEMA_VERSION:
                raw = data.get("companies")
                if isinstance(raw, list):
                    for entry in raw:
                        if not isinstance(entry, dict): continue
                        entry_ats = (entry.get("ats") or "").strip().lower()
                        if entry_ats not in ATS_SOURCE_NAMES:
                            log.warning("universe: unsupported ats %r in watchlist", entry_ats)
                            continue
                        if entry_ats == ats:
                            name = (entry.get("name") or "").strip()
                            slug = (entry.get("slug") or "").strip()
                            if name and slug:
                                companies_dict[slug] = UniverseCompany(name=name, ats=ats, slug=slug, priority=True)
        except (OSError, ValueError, KeyError, yaml.YAMLError) as e:
            log.warning("universe: error reading watchlist: %s", e)

    return companies_dict


def universe_slugs(ats: str) -> set[str]:
    """Every slug this lane's universe names, unfiltered.

    The only correct `known_slugs` for a `HealthLedger`. `load()` is a poll
    list, not a membership test: it hides boards in their post-prune cooldown,
    and a ledger told that list deletes their health as orphan.
    """
    return set(_universe_dict(ats))


def universe_companies(ats: str) -> list[UniverseCompany]:
    """Every company this lane's universe names, unfiltered, keeping the names.

    `universe_slugs` without discarding the name -- for a reader that wants the
    whole universe rather than tonight's poll list. Never a `HealthLedger`
    input either; the caveat on `universe_slugs` applies unchanged.
    """
    return list(_universe_dict(ats).values())


def load(ats: str) -> list[UniverseCompany]:
    companies_dict = _universe_dict(ats)

    # 3. Filter and Sort via Health Ledger
    today = pd.Timestamp.today().normalize()
    health_dict = {}
    if health_path(ats).exists():
        try:
            df = pd.read_parquet(health_path(ats))
            # Redundant now that the file is per-ATS, kept as a guard against a
            # mis-split migration writing foreign rows into a lane's ledger.
            df_ats = df[df["ats"] == ats]
            for _, row in df_ats.iterrows():
                # .get, not [...]: a ledger written before the column existed
                # is still on disk and is read every night until it is rewritten.
                last_kept = row.get("last_kept_at")
                health_dict[row["slug"]] = {
                    "last_yield": row["last_yield"] if pd.notna(row["last_yield"]) else 0,
                    "pruned_at": row["pruned_at"] if pd.notna(row["pruned_at"]) else None,
                    "last_kept_at": last_kept if pd.notna(last_kept) else None,
                }
        except (OSError, ValueError, KeyError) as e:
            log.warning("universe: error reading health ledger: %s", e)

    valid_companies = []
    for slug, co in companies_dict.items():
        h = health_dict.get(slug, {})
        pruned_at = h.get("pruned_at")
        if pd.notna(pruned_at):
            if (today - pruned_at) < timedelta(days=14):
                continue  # skip, it's pruned and not old enough to retry
        valid_companies.append(replace(co, last_kept_at=h.get("last_kept_at")))

    # Priority sort: watchlist (priority=True), then last_yield > 0, then rest
    def sort_key(c: UniverseCompany):
        h = health_dict.get(c.slug, {})
        yielded = 1 if h.get("last_yield", 0) > 0 else 0
        return (c.priority, yielded)

    valid_companies.sort(key=sort_key, reverse=True)
    return valid_companies


@dataclass(frozen=True)
class RunSelection:
    """What one run polls, and the cold list its cursor advances over."""

    to_poll: list
    hot: list
    cold: list


def select_for_run(companies, cursor, today=None,
                   rotation_runs: int = COLD_ROTATION_RUNS) -> RunSelection:
    """Split `companies` into the boards worth polling tonight.

    98% of polled boards never contribute a row to `clean.parquet`, and board
    novelty is 2-3% a night, so polling all of them every night is almost all
    waste. Hot boards -- watchlist entries, plus anything that kept a row in
    the last `HOT_WINDOW_DAYS` -- are polled every run. The cold remainder is
    polled one rotating slice at a time, so it is still covered in full every
    `rotation_runs` runs.

    Watchlist companies stay in the fixed head and never rotate, matching
    `workday.py`. The `HealthLedger` is built from `universe_slugs()`, not from
    this slice and not from `companies`: either one would prune health the run
    simply did not visit.
    """
    companies = list(companies)
    if today is None:
        today = pd.Timestamp.today().normalize()
    cutoff = today - timedelta(days=HOT_WINDOW_DAYS)

    def is_hot(c) -> bool:
        if c.priority:
            return True
        return c.last_kept_at is not None and pd.Timestamp(c.last_kept_at) >= cutoff

    hot = [c for c in companies if is_hot(c)]
    cold = [c for c in companies if not is_hot(c)]
    if not cold:
        return RunSelection(to_poll=hot, hot=hot, cold=[])

    # Round up, so a cold list shorter than `rotation_runs` still advances by
    # one board a run rather than by none.
    slice_size = -(-len(cold) // max(1, rotation_runs))
    rotated = cursor.rotate(cold, key=lambda c: c.slug, attr="cold_slug")
    return RunSelection(to_poll=hot + rotated[:slice_size], hot=hot, cold=rotated)
