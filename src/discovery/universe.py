import logging
from dataclasses import dataclass
from pathlib import Path
import yaml
import pandas as pd
import csv
from datetime import timedelta

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_COMPANIES_PATH = REPO_ROOT / "profile" / "companies.yaml"
CSV_DIR = REPO_ROOT / "data" / "universe"
HEALTH_PATH = REPO_ROOT / "jobs" / "universe_health.parquet"
_SCHEMA_VERSION = 1

# Delay import to avoid circular dependency since registry might import us
# Or hardcode here if simple
try:
    from src.discovery.sources.ats.registry import ATS_SOURCE_NAMES
except ImportError:
    ATS_SOURCE_NAMES = {"greenhouse", "lever", "ashby"}

@dataclass(frozen=True)
class UniverseCompany:
    name: str
    ats: str
    slug: str
    priority: bool = False

def update_health(ats: str, slug: str, success: bool, rows: int = 0):
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
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
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

    HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(HEALTH_PATH)

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
        except Exception as e:
            log.warning(f"universe: error reading {csv_path}: {e}")

    # 2. Load Watchlist
    if DEFAULT_COMPANIES_PATH.exists():
        try:
            data = yaml.safe_load(DEFAULT_COMPANIES_PATH.read_text())
            if isinstance(data, dict) and data.get("schema_version") == _SCHEMA_VERSION:
                raw = data.get("companies")
                if isinstance(raw, list):
                    for entry in raw:
                        if not isinstance(entry, dict): continue
                        entry_ats = (entry.get("ats") or "").strip().lower()
                        if entry_ats not in ATS_SOURCE_NAMES:
                            log.warning(f"universe: unsupported ats {entry_ats!r} in watchlist")
                            continue
                        if entry_ats == ats:
                            name = (entry.get("name") or "").strip()
                            slug = (entry.get("slug") or "").strip()
                            if name and slug:
                                companies_dict[slug] = UniverseCompany(name=name, ats=ats, slug=slug, priority=True)
        except Exception as e:
            log.warning(f"universe: error reading watchlist: {e}")

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
        except Exception as e:
            log.warning(f"universe: error reading health ledger: {e}")

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
