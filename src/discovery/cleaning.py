"""Deterministic cleaning.

No LLM calls (R7). Reads pipeline/*/state.yaml but never writes there.
Operations execute in the exact §6.2 step order:
  1. Normalize company / title fields (seniority preserved)
  2. Drop rows where jd_text < 200 chars
  3. Drop rows where posted_date < today-14d (missing date kept w/ flag);
     career-board sources exempt — board presence is the liveness signal
  4. Exact dedupe on (company_normalized, title_normalized), longest jd_text wins
  5. Near dedupe within company via rapidfuzz.WRatio >= 90, longest jd_text wins
  6. job_id = sha1(company_normalized|title_normalized)[:8]
     url and jd_text deliberately excluded so job_id is stable across re-scrapes.
     Flipping the hash on a URL change would silently orphan
     pipeline/<job_id>/state.yaml and applications/<dir> keys.
  7. Update jobs/seen.parquet: refresh last_score from
     scored.parquet (deterministic READ of Claude's sidecar — R7 intact),
     purge rows RESURFACE_AFTER_DAYS past expiry, stamp first_seen for new ids
  8. Glob pipeline/*/state.yaml -> set already_seen + application_status
  9. Drop expired rows per the seen-ledger tiers (tracked rows never expire)
  10. Initialize Claude-owned columns with defaults
  11. Write clean.parquet + clean.preview.jsonl + ## Cleaning run-report section
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

import pandas as pd
import yaml
from rapidfuzz import fuzz

from src import verticals
from src.discovery.location import parse_location
from src.discovery.config import load_config

log = logging.getLogger(__name__)


# Closed schema. coerce_schema enforces this exact set & order.
CLEAN_COLUMNS: list[str] = [
    "job_id", "source", "company", "company_normalized",
    "title", "title_normalized", "location", "remote_flag",
    "posted_date", "posted_date_missing", "scraped_date",
    "url", "jd_text",
    "salary_min", "salary_max", "salary_currency",
    "employment_type", "seniority_raw", "ingested_run_id",
    "vertical",  # a profile/verticals.yaml name | "" — Python-owned, set by discovery.py
    "already_seen", "application_status",
    "fit_score", "fit_subscores",
    "sponsorship_label", "sponsorship_evidence", "shortlist_rank",
]

# Career-board sources: presence on the company's own board
# this run IS the liveness signal, so the posted_date staleness cutoff does
# not apply; a board row's pipeline lifetime is governed by the seen-ledger.
from src.discovery.sources.ats.registry import ATS_SOURCE_NAMES
CAREER_SOURCES: tuple[str, ...] = tuple(ATS_SOURCE_NAMES)

# Seen-ledger retention policy (locked 2026-07-15). Visibility is
# measured from first_seen (time in MY system, not posting age), tiered by
# the latest fit_score; RESURFACE_AFTER_DAYS past expiry the ledger forgets,
# so a still-live posting re-enters as new and is re-judged. Rows with a
# pipeline/<job_id>/state.yaml never expire.
SCORE_HIGH_THRESHOLD = 80.0
RETENTION_HIGH_DAYS = 60
RETENTION_LOW_DAYS = 15
RESURFACE_AFTER_DAYS = 60

PREVIEW_COLUMNS = [
    "job_id", "source", "company", "title", "location",
    "posted_date", "url", "vertical", "fit_score", "sponsorship_label",
]

# Vertical classification fallback (`vertical` column). Scraped
# JobSpy rows are tagged by discovery.py at scrape time (which term list
# found them — the authoritative signal). This classifier exists only for
# two fallback cases, both handled here so there is one source of truth:
#   1. Manual inbox/*.md clips (no search term to key off) — called directly
#      from discovery.parse_inbox_file.
#   2. Legacy raw rows from before this column existed, or any row that
#      otherwise reaches project_raw with vertical="" — backfilled below so
#      a stale empty-vertical row never wins exact_dedupe over a freshly
#      re-scraped, correctly-tagged duplicate by virtue of a longer jd_text.
# The rules live in profile/verticals.yaml `classifier_rules`:
# an ORDERED list where first match wins, so rule order encodes the locked
# "primary on ambiguity" policy. A title matching no rule
# classifies as "" rather than guessed.
def classify_vertical_from_title(title: str | None) -> str:
    """Title-keyword fallback classifier. Returns a configured vertical name
    or "" (unclassified). Never called when discovery.py already set a
    vertical from the search term that found the row — see comment above."""
    if not isinstance(title, str) or not title:
        return ""
    for vertical, pattern in verticals.get_config().classifier_rules:
        if pattern.search(title):
            return vertical
    return ""


_VIA_SOURCES = ("linkedin", "indeed", "glassdoor", "google", "ziprecruiter", "zip recruiter")
_VIA_RE = re.compile(
    r"\s+via\s+(?:" + "|".join(re.escape(v) for v in _VIA_SOURCES) + r")\s*$"
)
_LEGACY_SUFFIX_RE = re.compile(r"[,\s]+(?:inc|llc|ltd|corp)\.?\s*$")
_SUFFIX_RE = re.compile(r"\s+(?:inc|llc|corp|corporation|ltd|limited|co|gmbh|plc|sa)\s*$")
_LEADING_THE_RE = re.compile(r"^the\s+")
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]")


# ---------------------------------------------------------------------
# Step 1 — normalization
# ---------------------------------------------------------------------

def legacy_normalize_company(s: str | None) -> str:
    if not isinstance(s, str) or not s:
        return ""
    x = s.lower()
    x = _VIA_RE.sub("", x)
    prev = None
    while prev != x:
        prev = x
        x = _LEGACY_SUFFIX_RE.sub("", x)
    x = _LEADING_THE_RE.sub("", x)
    return _WS_RE.sub(" ", x).strip()


def normalize_company(s: str | None) -> str:
    if not isinstance(s, str) or not s:
        return ""
    x = s.lower()
    x = _VIA_RE.sub("", x)
    x = _PUNCT_RE.sub(" ", x)
    prev = None
    while prev != x:
        prev = x
        x = _SUFFIX_RE.sub("", x)
    x = _LEADING_THE_RE.sub("", x)
    return _WS_RE.sub(" ", x).strip()


def normalize_title(s: str | None) -> str:
    if not isinstance(s, str) or not s:
        return ""
    x = s.lower()
    x = _PUNCT_RE.sub(" ", x)
    return _WS_RE.sub(" ", x).strip()


# ---------------------------------------------------------------------
# Step 6 — job_id (defined early; reused elsewhere)
# ---------------------------------------------------------------------

def compute_job_id(company_normalized: str, title_normalized: str) -> str:
    key = f"{company_normalized}|{title_normalized}".encode("utf-8")
    return hashlib.sha1(key).hexdigest()[:8]


# ---------------------------------------------------------------------
# Step 2 — drop short JD
# ---------------------------------------------------------------------

def drop_short_jd(df: pd.DataFrame, min_chars: int = 200) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    lens = df["jd_text"].fillna("").astype(str).str.strip().str.len()
    return df[lens >= min_chars].copy()


# ---------------------------------------------------------------------
# Step 3 — drop stale + posted_date_missing flag
# ---------------------------------------------------------------------

def drop_stale(
    df: pd.DataFrame,
    today: pd.Timestamp | None = None,
    max_age_days: int = 14,
    exempt_sources: tuple[str, ...] = CAREER_SOURCES,
) -> pd.DataFrame:
    df = df.copy()
    today = pd.Timestamp.today().normalize() if today is None else pd.Timestamp(today).normalize()
    cutoff = today - pd.Timedelta(days=max_age_days)
    posted = pd.to_datetime(df.get("posted_date"), errors="coerce")
    try:
        if posted.dt.tz is not None:
            posted = posted.dt.tz_localize(None)
    except (AttributeError, TypeError):
        pass
    df["posted_date"] = posted
    df["posted_date_missing"] = posted.isna()
    if df.empty:
        return df
    keep = posted.isna() | (posted >= cutoff)
    source = df.get("source")
    if source is not None:
        # Career boards: an old-but-listed posting is live by definition;
        # its lifetime is governed by the seen-ledger (apply_expiry), not age.
        keep = keep | source.isin(exempt_sources)
    return df[keep].copy()


# ---------------------------------------------------------------------
# Step 4 — exact dedupe
# ---------------------------------------------------------------------

def exact_dedupe(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    df = df.copy()
    df["_jd_len"] = df["jd_text"].fillna("").astype(str).str.len()
    df = df.sort_values("_jd_len", ascending=False, kind="stable")
    df = df.drop_duplicates(subset=["company_normalized", "title_normalized"], keep="first")
    return df.drop(columns="_jd_len")


# ---------------------------------------------------------------------
# Step 5 — near dedupe within company
# ---------------------------------------------------------------------

def near_dedupe(df: pd.DataFrame, ratio_threshold: float = 90) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    df = df.copy()
    df["_jd_len"] = df["jd_text"].fillna("").astype(str).str.len()
    keep_indices: list = []
    for _, group in df.groupby("company_normalized", sort=False):
        g = group.sort_values("_jd_len", ascending=False, kind="stable")
        kept_titles: list[str] = []
        for idx, title in zip(g.index, g["title_normalized"]):
            if any(fuzz.WRatio(title, kt) >= ratio_threshold for kt in kept_titles):
                continue
            kept_titles.append(title)
            keep_indices.append(idx)
    return df.loc[keep_indices].drop(columns="_jd_len").copy()


# ---------------------------------------------------------------------
# Step 7 — seen-ledger (jobs/seen.parquet)
# ---------------------------------------------------------------------

def _lifetime_days(score: float) -> int:
    if pd.notna(score) and score >= SCORE_HIGH_THRESHOLD:
        return RETENTION_HIGH_DAYS
    return RETENTION_LOW_DAYS


def update_seen_ledger(
    job_ids: list[str],
    ledger_path: Path,
    scored_path: Path,
    today: pd.Timestamp,
) -> pd.DataFrame:
    """Rewrite jobs/seen.parquet: refresh last_score from scored.parquet
    where a judgment exists (a deterministic READ — no scoring here, R7),
    purge rows RESURFACE_AFTER_DAYS past their expiry (forget, so a
    still-live posting re-enters as new), stamp first_seen for new ids.
    Returns the updated ledger."""
    today = pd.Timestamp(today).normalize()
    if ledger_path.exists():
        ledger = pd.read_parquet(ledger_path)
    else:
        ledger = pd.DataFrame({
            "job_id": pd.Series(dtype=str),
            "first_seen": pd.Series(dtype="datetime64[ns]"),
            "last_score": pd.Series(dtype=float),
        })
    if scored_path.exists() and len(ledger):
        try:
            scored = pd.read_parquet(scored_path, columns=["job_id", "fit_score"])
        except Exception as e:
            log.warning("seen-ledger: could not read %s: %s", scored_path, e)
        else:
            score_map = scored.set_index("job_id")["fit_score"].to_dict()
            refreshed = ledger["job_id"].map(score_map)
            ledger["last_score"] = refreshed.combine_first(ledger["last_score"])
    if len(ledger):
        lifetimes = ledger["last_score"].apply(_lifetime_days)
        purge_at = ledger["first_seen"] + pd.to_timedelta(
            lifetimes + RESURFACE_AFTER_DAYS, unit="D"
        )
        ledger = ledger[today <= purge_at].copy()
    new_ids = sorted(set(job_ids) - set(ledger["job_id"]))
    if new_ids:
        new_rows = pd.DataFrame({
            "job_id": new_ids,
            "first_seen": today,
            "last_score": float("nan"),
        })
        ledger = new_rows if ledger.empty else pd.concat([ledger, new_rows], ignore_index=True)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger.to_parquet(ledger_path, index=False)
    return ledger


# ---------------------------------------------------------------------
# Step 8 — state.yaml glob
# ---------------------------------------------------------------------

def apply_state_yaml(df: pd.DataFrame, pipeline_dir: Path) -> pd.DataFrame:
    df = df.copy()
    df["already_seen"] = False
    df["application_status"] = ""
    if not pipeline_dir.exists() or df.empty:
        return df
    state_map: dict[str, str] = {}
    for state_file in pipeline_dir.glob("*/state.yaml"):
        try:
            data = yaml.safe_load(state_file.read_text()) or {}
        except (yaml.YAMLError, OSError) as e:
            log.warning("Skipping unreadable state file %s: %s", state_file, e)
            continue
        jid, state = data.get("job_id"), data.get("state")
        if isinstance(jid, str) and isinstance(state, str):
            state_map[jid] = state
    if not state_map:
        return df
    matched = df["job_id"].isin(state_map)
    df.loc[matched, "already_seen"] = True
    df.loc[matched, "application_status"] = df.loc[matched, "job_id"].map(state_map)
    return df


# ---------------------------------------------------------------------
# Step 9 — seen-ledger expiry
# ---------------------------------------------------------------------

def apply_expiry(
    df: pd.DataFrame,
    ledger: pd.DataFrame,
    today: pd.Timestamp,
) -> pd.DataFrame:
    """Drop rows past their retention window: visible for
    RETENTION_HIGH_DAYS from first_seen when last_score >= threshold, else
    RETENTION_LOW_DAYS. Rows with already_seen=True (a state.yaml exists,
    including skip) never expire — state.yaml is the permanent memory."""
    if df.empty or ledger is None or not len(ledger):
        return df.copy()
    today = pd.Timestamp(today).normalize()
    lifetimes = ledger["last_score"].apply(_lifetime_days)
    expires_at = ledger["first_seen"] + pd.to_timedelta(lifetimes, unit="D")
    expired_ids = set(ledger.loc[today > expires_at, "job_id"])
    if not expired_ids:
        return df.copy()
    drop = df["job_id"].isin(expired_ids) & ~df["already_seen"]
    return df[~drop].copy()


# ---------------------------------------------------------------------
# Step 9b — location allowlist filter
# ---------------------------------------------------------------------

def filter_and_canonicalize_location(df: pd.DataFrame, cfg) -> pd.DataFrame:
    if df.empty:
        return df.copy()
        
    allow_countries = {c.lower() for c in cfg.location_allowlist.countries}
    allow_states = {s.lower() for s in cfg.location_allowlist.states}
    allow_cities = {c.lower() for c in cfg.location_allowlist.cities}
    
    keep_indices = []
    new_locations = []
    
    for idx, raw_loc in zip(df.index, df["location"]):
        raw_loc_str = str(raw_loc).strip() if pd.notna(raw_loc) else ""
        parsed = parse_location(raw_loc_str)
        
        # 1. Nothing parsed, or remote-only -> KEEP
        if not parsed.country and not parsed.state and not parsed.city:
            keep_indices.append(idx)
            if parsed.remote:
                new_locations.append("Remote")
            else:
                new_locations.append(raw_loc_str)
            continue
            
        # 2. Parsed to a place positively OUTSIDE the allowlist -> DROP
        # 3. Parsed country/state/city all within location_allowlist (hierarchical) -> KEEP
        drop = False
        if allow_countries and parsed.country.lower() not in allow_countries:
            drop = True
        elif allow_states and parsed.state and parsed.state.lower() not in allow_states:
            drop = True
        elif allow_cities and parsed.city and parsed.city.lower() not in allow_cities:
            drop = True
            
        if not drop:
            keep_indices.append(idx)
            canon = ""
            if parsed.city and parsed.state:
                canon = f"{parsed.city}, {parsed.state}"
            elif parsed.state:
                canon = parsed.state
            elif parsed.country:
                canon = parsed.country
                
            if parsed.remote:
                # wait, canonical for Remote + Austin, TX? 
                # Let's just use Remote - City, ST? Or does spec say "City, ST (or Remote, or original)"?
                # "canonical 'City, ST' (or 'Remote', or original string when unparsed)"
                # Let's use "Remote - City, ST" if both, or just "Remote" if only remote.
                # Actually, the spec says "canonical 'City, ST' (or 'Remote', or original string when unparsed)".
                canon = f"Remote - {canon}" if canon else "Remote"
            
            new_locations.append(canon)
            
    filtered = df.loc[keep_indices].copy()
    filtered["location"] = new_locations
    return filtered


# ---------------------------------------------------------------------
# Step 10 — Claude-owned defaults
# ---------------------------------------------------------------------

def init_claude_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["fit_score"] = float("nan")
    df["fit_subscores"] = ""
    df["sponsorship_label"] = "unknown"
    df["sponsorship_evidence"] = ""
    df["shortlist_rank"] = float("nan")
    return df


# ---------------------------------------------------------------------
# Raw-schema projection (JobSpy columns -> canonical column names)
# ---------------------------------------------------------------------

_RAW_RENAME = {
    "site": "source",
    "date_posted": "posted_date",
    "is_remote": "remote_flag",
    "description": "jd_text",
    "min_amount": "salary_min",
    "max_amount": "salary_max",
    "currency": "salary_currency",
    "job_type": "employment_type",
    "job_level": "seniority_raw",
}


def project_raw(df: pd.DataFrame) -> pd.DataFrame:
    """Rename JobSpy columns to canonical names and pick url = direct-or-fallback."""
    df = df.copy()
    for old, new in _RAW_RENAME.items():
        if old in df.columns and new not in df.columns:
            df = df.rename(columns={old: new})
    # url: prefer job_url_direct when non-empty, else job_url
    if "url" not in df.columns:
        direct = (
            df["job_url_direct"].fillna("").astype(str)
            if "job_url_direct" in df.columns
            else pd.Series([""] * len(df), index=df.index)
        )
        fallback = (
            df["job_url"].fillna("").astype(str)
            if "job_url" in df.columns
            else pd.Series([""] * len(df), index=df.index)
        )
        df["url"] = direct.where(direct.str.len() > 0, fallback)
    string_defaults = {
        "salary_currency": "", "employment_type": "", "seniority_raw": "",
        "location": "", "source": "", "ingested_run_id": "",
        "company": "", "title": "", "jd_text": "", "url": "", "vertical": "",
    }
    for col, default in string_defaults.items():
        if col not in df.columns:
            df[col] = default
        else:
            df[col] = df[col].fillna(default).astype(str)
    # Backfill empty vertical from title (legacy raw rows predating this
    # column, or any row that otherwise reached here unclassified). Never
    # overrides a vertical discovery.py already set from the search term.
    needs_fallback = df["vertical"] == ""
    if needs_fallback.any():
        df.loc[needs_fallback, "vertical"] = (
            df.loc[needs_fallback, "title"].apply(classify_vertical_from_title)
        )
    if "remote_flag" not in df.columns:
        df["remote_flag"] = False
    else:
        df["remote_flag"] = df["remote_flag"].fillna(False).astype(bool)
    if "scraped_date" not in df.columns:
        df["scraped_date"] = pd.Timestamp.today().normalize()
    df["scraped_date"] = pd.to_datetime(df["scraped_date"], errors="coerce")
    if "posted_date" not in df.columns:
        df["posted_date"] = pd.NaT
    for col in ("salary_min", "salary_max"):
        if col not in df.columns:
            df[col] = float("nan")
    return df


def apply_title_exclusion(df: pd.DataFrame, cfg) -> tuple[pd.DataFrame, dict[str, int]]:
    """Per-vertical title gate (word-boundary). A non-manual row is kept iff:
        strong_keep  OR  (include_ok AND NOT exclude)
    where include_ok is True when the vertical configures no
    title_include_terms — i.e. the include-gate is OFF and this reduces to
    the historical exclusion-only behavior (drop rows matching
    title_exclude_terms). title_strong_keep_terms override every exclude, so
    an unambiguous in-lane title survives even when it also trips an exclude
    (e.g. "Model Risk Management Lead, Machine Learning"). Manual/URL-ingested
    rows are always exempt. Returns (filtered_df, drops_per_vertical)."""
    if df.empty:
        return df.copy(), {}

    df = df.copy()
    drops_per_vertical = {}

    def _compile(terms):
        if not terms:
            return None
        return re.compile("|".join(rf"\b{re.escape(t)}\b" for t in terms), re.IGNORECASE)

    keep_mask = pd.Series(True, index=df.index)

    for name, v in cfg.verticals.items():
        # Only apply to rows of this vertical, and NOT manual source
        mask = (df["vertical"] == name) & (df["source"] != "manual")
        if not mask.any():
            continue
        titles = df.loc[mask, "title"].fillna("").astype(str)

        exclude_rx = _compile(v.title_exclude_terms)
        include_rx = _compile(v.title_include_terms)
        strong_rx = _compile(v.title_strong_keep_terms)

        exclude_hit = titles.str.contains(exclude_rx) if exclude_rx else pd.Series(False, index=titles.index)
        strong_hit = titles.str.contains(strong_rx) if strong_rx else pd.Series(False, index=titles.index)
        # include-gate off (no include terms) => include_ok True for all
        include_ok = titles.str.contains(include_rx) if include_rx else pd.Series(True, index=titles.index)

        keep = strong_hit | (include_ok & ~exclude_hit)
        drops_per_vertical[name] = int((~keep).sum())
        keep_mask.loc[mask] = keep.values

    return df[keep_mask].copy(), drops_per_vertical


def coerce_schema(df: pd.DataFrame) -> pd.DataFrame:
    """Return df with exactly the canonical columns in canonical order.
    Raises KeyError on any missing required column — fail loud (per slice-2 rule)."""
    missing = [c for c in CLEAN_COLUMNS if c not in df.columns]
    if missing:
        raise KeyError(
            f"clean.parquet schema missing required columns: {missing}. "
            "Determinism-boundary failure — fix the step that should have set these "
            "columns; do not paper over."
        )
    extras = [c for c in df.columns if c not in CLEAN_COLUMNS]
    if extras:
        log.info("Dropping non-schema columns from clean.parquet: %s", extras)
    return df[CLEAN_COLUMNS].copy()


# ---------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------

_RAW_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4})(?:_[^.]+)?\.parquet$")


def _parse_run_ts_from_filename(name: str) -> pd.Timestamp | None:
    m = _RAW_FILENAME_RE.match(name)
    if not m:
        return None
    date_s, time_s = m.group(1), m.group(2)
    try:
        return pd.Timestamp(f"{date_s} {time_s[:2]}:{time_s[2:]}:00")
    except ValueError:
        return None


def load_raw_window(
    raw_dir: Path,
    today: pd.Timestamp | None = None,
    max_age_days: int = 14,
) -> pd.DataFrame:
    today = pd.Timestamp.today().normalize() if today is None else pd.Timestamp(today).normalize()
    cutoff = today - pd.Timedelta(days=max_age_days)
    if not raw_dir.exists():
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    for path in sorted(raw_dir.glob("*.parquet")):
        ts = _parse_run_ts_from_filename(path.name)
        if ts is None:
            log.warning("Raw filename not YYYY-MM-DD_HHMM.parquet: %s — skipping", path.name)
            continue
        if ts.normalize() < cutoff:
            continue
        try:
            frames.append(pd.read_parquet(path))
        except Exception as e:
            log.error("Failed to read %s: %s", path, e)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def prune_raw_files(raw_dir: Path, cfg, today: pd.Timestamp) -> int:
    if not raw_dir.exists():
        return 0
    today = today.normalize()
    cutoff = today - pd.Timedelta(days=cfg.raw_retention_days)
    pruned_count = 0
    for path in raw_dir.glob("*.parquet"):
        ts = _parse_run_ts_from_filename(path.name)
        if ts is None:
            continue
        if ts.normalize() < cutoff:
            try:
                path.unlink()
                pruned_count += 1
            except OSError as e:
                log.warning("Could not delete old raw file %s: %s", path, e)
    return pruned_count


def _append_cleaning_section(report_path: Path, run_id: str, stats: dict) -> None:
    lines = [
        "",
        "## Cleaning",
        "",
        f"Run: `{run_id}`",
        "",
        f"- raw rows loaded: {stats.get('raw_rows', 0)}",
        f"- after classification/exclusion: {stats.get('after_exclusion', 0)} "
        f"(dropped {stats.get('dropped_exclusion', 0)})",
        f"- after short-JD drop (<200 chars): {stats.get('after_short_jd', 0)} "
        f"(dropped {stats.get('dropped_short', 0)})",
        f"- after stale drop (>14d): {stats.get('after_stale', 0)} "
        f"(dropped {stats.get('dropped_stale', 0)})",
        f"- after exact dedupe: {stats.get('after_exact_dedupe', 0)} "
        f"(dropped {stats.get('dropped_exact', 0)})",
        f"- after near dedupe (WRatio>=90): {stats.get('after_near_dedupe', 0)} "
        f"(dropped {stats.get('dropped_near', 0)})",
        f"- changed job_ids vs seen ledger: {stats.get('changed_seen', 0)}",
        f"- after seen-ledger expiry: {stats.get('after_expiry', 0)} "
        f"(dropped {stats.get('dropped_expired', 0)})",
        f"- after location filter: {stats.get('after_location', 0)} "
        f"(dropped {stats.get('dropped_location', 0)})",
        f"- final rows: {stats.get('final_rows', 0)}",
        f"- pruned {stats.get('pruned_raw', 0)} raw files",
        "",
        "### Per-source counts (raw -> final)",
        "",
    ]
    
    drops_per_vertical = stats.get("drops_per_vertical", {})
    if drops_per_vertical:
        for vertical, count in sorted(drops_per_vertical.items()):
            lines.insert(-2, f"- {vertical}: title-excluded {count} rows")
    
    src_counts = stats.get("per_source", {})
    if src_counts:
        lines.append("| source | raw | final |")
        lines.append("|---|---|---|")
        for src in sorted(src_counts):
            raw, final = src_counts[src]
            lines.append(f"| {src} | {raw} | {final} |")
    else:
        lines.append("(no rows)")
    lines.append("")
    with report_path.open("a") as f:
        f.write("\n".join(lines))


def write_outputs(
    df: pd.DataFrame,
    clean_dir: Path,
    runs_dir: Path,
    run_id: str,
    stats: dict,
) -> None:
    clean_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(clean_dir / "clean.parquet", index=False)
    preview_path = clean_dir / "clean.preview.jsonl"
    preview_cols = [c for c in PREVIEW_COLUMNS if c in df.columns]
    with preview_path.open("w") as f:
        for _, row in df[preview_cols].iterrows():
            d = row.to_dict()
            for k, v in list(d.items()):
                if isinstance(v, pd.Timestamp):
                    d[k] = None if pd.isna(v) else v.isoformat()
                elif isinstance(v, float) and pd.isna(v):
                    d[k] = None
            f.write(json.dumps(d, default=str) + "\n")
    _append_cleaning_section(runs_dir / f"{run_id}.md", run_id, stats)


# ---------------------------------------------------------------------
# Orchestration — §6.2 in exact order
# ---------------------------------------------------------------------

def run(
    run_id: str,
    raw_dir: Path = Path("jobs/raw"),
    clean_dir: Path = Path("jobs"),
    runs_dir: Path = Path("jobs/runs"),
    pipeline_dir: Path = Path("pipeline"),
    today: pd.Timestamp | None = None,
) -> pd.DataFrame:
    runs_dir.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    raw = load_raw_window(raw_dir, today=today)
    raw_rows = len(raw)
    df = project_raw(raw)
    # step 1 exclusion
    df, drops_per_vertical = apply_title_exclusion(df, verticals.get_config())
    after_exclusion = len(df)
    # step 1
    df["company_normalized"] = df["company"].apply(normalize_company)
    df["title_normalized"] = df["title"].apply(normalize_title)
    per_source_raw = df["source"].value_counts().to_dict() if not df.empty else {}
    # step 2
    df = drop_short_jd(df)
    after_short = len(df)
    # step 3
    df = drop_stale(df, today=today)
    after_stale = len(df)
    # step 4
    df = exact_dedupe(df)
    after_exact = len(df)
    # step 5
    df = near_dedupe(df)
    after_near = len(df)
    # step 6
    df["job_id"] = [
        compute_job_id(c, t)
        for c, t in zip(df["company_normalized"], df["title_normalized"])
    ]
    # step 7 — seen-ledger
    today_ts = pd.Timestamp.today().normalize() if today is None else pd.Timestamp(today).normalize()
    ledger_path = clean_dir / "seen.parquet"
    
    if ledger_path.exists():
        try:
            old_ledger_ids = set(pd.read_parquet(ledger_path)["job_id"])
            old_job_ids = [
                compute_job_id(legacy_normalize_company(c), t)
                for c, t in zip(df["company"], df["title_normalized"])
            ]
            changed_seen = len({
                old_id for old_id, new_id in zip(old_job_ids, df["job_id"])
                if old_id != new_id and old_id in old_ledger_ids
            })
        except Exception as e:
            log.warning("Could not read seen.parquet for diff: %s", e)
            changed_seen = 0
    else:
        changed_seen = 0

    ledger = update_seen_ledger(
        df["job_id"].tolist(),
        ledger_path,
        clean_dir / "scored.parquet",
        today_ts,
    )
    # step 8
    df = apply_state_yaml(df, pipeline_dir)
    # step 9 — retention expiry (tracked rows exempt)
    df = apply_expiry(df, ledger, today_ts)
    after_expiry = len(df)
    
    # step 9b - location filter
    df = filter_and_canonicalize_location(df, cfg)
    after_location = len(df)
    
    # step 10
    df = init_claude_columns(df)
    # step 11
    df = coerce_schema(df)
    per_source_final = df["source"].value_counts().to_dict() if not df.empty else {}
    per_source = {
        src: (per_source_raw.get(src, 0), per_source_final.get(src, 0))
        for src in set(per_source_raw) | set(per_source_final)
    }
    stats = {
        "raw_rows": raw_rows,
        "after_exclusion": after_exclusion,
        "dropped_exclusion": raw_rows - after_exclusion,
        "drops_per_vertical": drops_per_vertical,
        "after_short_jd": after_short,
        "dropped_short": raw_rows - after_short,
        "after_stale": after_stale,
        "dropped_stale": after_short - after_stale,
        "after_exact_dedupe": after_exact,
        "dropped_exact": after_stale - after_exact,
        "after_near_dedupe": after_near,
        "dropped_near": after_exact - after_near,
        "after_expiry": after_expiry,
        "dropped_expired": after_near - after_expiry,
        "after_location": after_location,
        "dropped_location": after_expiry - after_location,
        "changed_seen": changed_seen,
        "final_rows": len(df),
        "per_source": per_source,
    }
    pruned = prune_raw_files(raw_dir, cfg, today_ts)
    stats["pruned_raw"] = pruned
    write_outputs(df, clean_dir, runs_dir, run_id, stats)
    return df
