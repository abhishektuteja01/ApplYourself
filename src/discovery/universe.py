from __future__ import annotations

import logging
from dataclasses import dataclass
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

HEALTH_COLUMNS = ["ats", "slug", "consecutive_404s", "last_ok", "last_yield", "pruned_at"]


def health_path(ats: str):
    return HEALTH_DIR / f"universe_health_{ats}.parquet"

@dataclass(frozen=True)
class UniverseCompany:
    name: str
    ats: str
    slug: str
    priority: bool = False

class HealthLedger:
    """One ATS lane's health ledger, accumulated in memory and written once.

    A full read-modify-write per company cost ~4.3 ms and ~12,500 file rewrites
    a night. Marks are held here and applied in a single `flush()`.

    `known_slugs` is the lane's current universe. Slugs absent from it are
    dropped on flush; pass None to keep every row.
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
            df = pd.read_parquet(path)
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
                new_rows.append({
                    "ats": self.ats, "slug": slug, "consecutive_404s": 0,
                    "last_ok": pd.NaT, "last_yield": 0, "pruned_at": pd.NaT,
                })
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


def load(ats: str) -> list[UniverseCompany]:
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
                health_dict[row["slug"]] = {
                    "last_yield": row["last_yield"] if pd.notna(row["last_yield"]) else 0,
                    "pruned_at": row["pruned_at"] if pd.notna(row["pruned_at"]) else None
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
        valid_companies.append(co)

    # Priority sort: watchlist (priority=True), then last_yield > 0, then rest
    def sort_key(c: UniverseCompany):
        h = health_dict.get(c.slug, {})
        yielded = 1 if h.get("last_yield", 0) > 0 else 0
        return (c.priority, yielded)

    valid_companies.sort(key=sort_key, reverse=True)
    return valid_companies
