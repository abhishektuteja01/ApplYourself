from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
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
HEALTH_PATH = REPO_ROOT / "jobs" / "universe_health.parquet"
_SCHEMA_VERSION = 1

@dataclass(frozen=True)
class UniverseCompany:
    name: str
    ats: str
    slug: str
    priority: bool = False

def update_health(ats: str, slug: str, success: bool, rows: int = 0):
    """success=False counts a strike toward pruning; call it only for a board
    that is permanently dead, never for a transient fetch failure."""
    if HEALTH_PATH.exists():
        df = pd.read_parquet(HEALTH_PATH)
    else:
        df = pd.DataFrame(columns=["ats", "slug", "consecutive_404s", "last_ok", "last_yield", "pruned_at"])
    
    mask = (df["ats"] == ats) & (df["slug"] == slug)
    today = pd.Timestamp.today().normalize()
    
    if not mask.any():
        row = {
            "ats": ats, "slug": slug, "consecutive_404s": 0,
            "last_ok": pd.NaT, "last_yield": 0, "pruned_at": pd.NaT
        }
        # Concatenating onto an all-empty frame is deprecated in pandas and
        # errors under filterwarnings; on the first-ever call there is nothing
        # to concatenate to.
        new = pd.DataFrame([row])
        df = new if df.empty else pd.concat([df, new], ignore_index=True)
        mask = (df["ats"] == ats) & (df["slug"] == slug)
    
    idx = df.index[mask][0]
    
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

    write_parquet(df, HEALTH_PATH)

def load(ats: str) -> list[UniverseCompany]:
    companies_dict = {}
    
    # 1. Load CSV
    csv_path = CSV_DIR / f"{ats}.csv"
    if csv_path.exists():
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
                        companies_dict[slug] = UniverseCompany(name=name, ats=ats, slug=slug, priority=False)
        except (OSError, ValueError, KeyError, yaml.YAMLError) as e:
            log.warning("universe: error reading %s: %s", csv_path, e)

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
    if HEALTH_PATH.exists():
        try:
            df = pd.read_parquet(HEALTH_PATH)
            df_ats = df[df["ats"] == ats]
            for _, row in df_ats.iterrows():
                health_dict[row["slug"]] = {
                    "last_yield": row["last_yield"] if pd.notna(row["last_yield"]) else 0,
                    "pruned_at": row["pruned_at"] if pd.notna(row["pruned_at"]) else None
                }
        except (OSError, ValueError, KeyError, yaml.YAMLError) as e:
            log.warning("universe: error reading health ledger: %s", e)

    valid_companies = []
    for slug, co in companies_dict.items():
        h = health_dict.get(slug, {})
        pruned_at = h.get("pruned_at")
        if pd.notna(pruned_at) and pruned_at is not None:
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
