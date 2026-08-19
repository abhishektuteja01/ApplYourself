"""Deterministic cleaning.

No LLM calls (R7). Reads pipeline/*/state.yaml but never writes there.
Operations execute in this exact step order:
  0. Per-vertical title gate (apply_title_exclusion), before everything else
  1. Normalize company / title fields (seniority preserved)
  2. Drop rows where jd_text < 200 chars
  3. Drop rows where posted_date < today-14d (missing date kept w/ flag);
     career-board sources exempt — board presence is the liveness signal
  3b. Drop rows outside the location allowlist
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
from src.discovery.config import load_config
from src.discovery.schema import naive_datetime
from src import paths
from src.parquet_io import write_parquet
from src.state_io import load_state_index

log = logging.getLogger(__name__)


# Closed schema. coerce_schema enforces this exact set & order.
CLEAN_COLUMNS: list[str] = [
    "job_id", "source", "company", "company_normalized",
    "title", "title_normalized", "location", "remote_flag",
    "posted_date", "posted_date_missing", "scraped_date",
    "url", "jd_text",
    "salary_min", "salary_max", "salary_currency",
    "employment_type", "seniority_raw", "ingested_run_id",
    "vertical",  # a profile/verticals.yaml name | "" — Python-owned, set at fetch time
    "already_seen", "application_status",
    "fit_score", "fit_subscores",
    "sponsorship_label", "sponsorship_evidence", "shortlist_rank",
]

# Career-board sources: presence on the company's own board
# this run IS the liveness signal, so the posted_date staleness cutoff does
# not apply; a board row's pipeline lifetime is governed by the seen-ledger.
from src.discovery.sources.ats.registry import ATS_SOURCE_NAMES, ATS_URL_MARKERS
CAREER_SOURCES: tuple[str, ...] = tuple(ATS_SOURCE_NAMES)

# "manual" joins them for a different reason: an inbox clip or a URL ingest is
# a deliberate user add, and ingest_url rewrites site to "manual" for every
# path including the ATS ones — so keying the exemption on the board name alone
# drops a still-live posting first published more than 14 days ago.
STALENESS_EXEMPT_SOURCES: tuple[str, ...] = CAREER_SOURCES + ("manual",)

# Seen-ledger retention. Visibility is measured from first_seen (time in the
# system, not posting age) and tiered by the latest fit_score;
# RESURFACE_AFTER_DAYS past expiry the ledger forgets, so a still-live posting
# re-enters as new and is re-judged. A row with a state.yaml never expires.
SCORE_HIGH_THRESHOLD = 80.0
RETENTION_HIGH_DAYS = 60
RETENTION_LOW_DAYS = 15
RESURFACE_AFTER_DAYS = 60

# The discovery window: drop_stale and load_raw_window both read it.
MAX_AGE_DAYS = 14
# Shorter than this is a JS shell or a truncated fetch, not a JD. ingest_url
# quotes this in its error message.
MIN_JD_CHARS = 200

PREVIEW_COLUMNS = [
    "job_id", "source", "company", "title", "location",
    "posted_date", "url", "vertical", "fit_score", "sponsorship_label",
]

# Vertical classification fallback (`vertical` column). Scraped
# JobSpy rows are tagged by sources/jobspy_source.py at scrape time (which term list
# found them — the authoritative signal). This classifier exists only for
# two fallback cases, both handled here so there is one source of truth:
#   1. Manual inbox/*.md clips (no search term to key off) — called directly
#      from inbox.parse_inbox_file.
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
    or "" (unclassified). Never called when the scrape already set a
    vertical from the search term that found the row — see comment above."""
    if not isinstance(title, str) or not title:
        return ""
    for vertical, rule in verticals.get_config().classifier_rules:
        if rule.matches(title):
            return vertical
    return ""


_VIA_SOURCES = ("linkedin", "indeed", "glassdoor", "google", "ziprecruiter", "zip recruiter")
_VIA_RE = re.compile(
    r"\s+via\s+(?:" + "|".join(re.escape(v) for v in _VIA_SOURCES) + r")\s*$"
)
_SUFFIX_RE = re.compile(r"\s+(?:inc|llc|corp|corporation|ltd|limited|co|gmbh|plc|sa)\s*$")
_LEADING_THE_RE = re.compile(r"^the\s+")
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]")


# ---------------------------------------------------------------------
# Step 1 — normalization
# ---------------------------------------------------------------------


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

def drop_short_jd(df: pd.DataFrame, min_chars: int = MIN_JD_CHARS) -> pd.DataFrame:
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
    max_age_days: int = MAX_AGE_DAYS,
    exempt_sources: tuple[str, ...] = STALENESS_EXEMPT_SOURCES,
) -> pd.DataFrame:
    df = df.copy()
    today = pd.Timestamp.today().normalize() if today is None else pd.Timestamp(today).normalize()
    cutoff = today - pd.Timedelta(days=max_age_days)
    posted = naive_datetime(
        df["posted_date"] if "posted_date" in df.columns
        else pd.Series(pd.NaT, index=df.index)
    )
    df["posted_date"] = posted
    df["posted_date_missing"] = posted.isna()
    if df.empty:
        return df
    keep = posted.isna() | (posted >= cutoff)
    source = df.get("source")
    if source is not None:
        # Career boards and manual adds: an old-but-listed posting is live by
        # definition; lifetime is governed by the seen-ledger, not age.
        keep = keep | source.isin(exempt_sources)
    return df[keep].copy()


# ---------------------------------------------------------------------
# Step 3b — location allowlist filter
# ---------------------------------------------------------------------

def filter_and_canonicalize_location(df: pd.DataFrame, cfg) -> pd.DataFrame:
    if df.empty:
        return df.copy()

    # Local import: location needs libpostal (optional `discovery` group), and
    # importing this module must stay possible without it.
    from src.discovery.location import parse_location

    # effective_countries() folds in the optional `continents` shorthand;
    # effective_states() accepts either a full name or a code, since
    # parse_location only ever returns codes.
    allow_countries = {c.lower() for c in cfg.location_allowlist.effective_countries()}
    allow_states = {s.lower() for s in cfg.location_allowlist.effective_states()}
    allow_cities = {c.lower() for c in cfg.location_allowlist.cities}

    keep_indices = []
    new_locations = []

    for idx, raw_loc in zip(df.index, df["location"]):
        raw_loc_str = str(raw_loc).strip() if pd.notna(raw_loc) else ""
        parsed = parse_location(raw_loc_str)

        # 1. Nothing parsed at all -> KEEP (conservative default, unchanged).
        if not parsed.country and not parsed.state and not parsed.city and not parsed.candidate_countries:
            keep_indices.append(idx)
            if parsed.remote:
                new_locations.append("Remote")
            else:
                new_locations.append(raw_loc_str)
            continue

        # 1b. A genuine multi-region ambiguity (location.py no longer picks
        # a winner among multiple countries itself -- that decision belongs
        # here, against whatever the user actually configured, so an
        # India-only allowlist gets the same "don't guess" protection a
        # US-only one does). Overlaps the allowlist -> KEEP unresolved for
        # review; no overlap at all -> positively excluded -> DROP.
        if parsed.candidate_countries:
            candidates_lower = {c.lower() for c in parsed.candidate_countries}
            if not allow_countries or candidates_lower & allow_countries:
                keep_indices.append(idx)
                new_locations.append("Remote" if parsed.remote else raw_loc_str)
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
                # Remote plus a parsed place keeps both: "Remote - Austin, TX".
                canon = f"Remote - {canon}" if canon else "Remote"

            new_locations.append(canon)

    filtered = df.loc[keep_indices].copy()
    filtered["location"] = new_locations
    return filtered


# ---------------------------------------------------------------------
# Step 4 — exact dedupe
# ---------------------------------------------------------------------

def _not_applyable(df: pd.DataFrame) -> pd.Series:
    """False for rows whose url leads to a board application form. Sorted
    ascending, so those rows win their group."""
    url = df["url"].fillna("").astype(str) if "url" in df.columns else pd.Series("", index=df.index)
    return ~url.str.contains("|".join(re.escape(m) for m in ATS_URL_MARKERS), case=False, regex=True)


def exact_dedupe(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    df = df.copy()
    df["_jd_len"] = df["jd_text"].fillna("").astype(str).str.len()
    # Applyability outranks jd_text length: an aggregator repost wins on
    # appended boilerplate by a percent or two and costs the only url that can
    # be submitted to. Both rows share company_normalized and title_normalized,
    # so job_id is identical either way and no tracked role is orphaned.
    df["_not_applyable"] = _not_applyable(df)
    df = df.sort_values(["_not_applyable", "_jd_len"], ascending=[True, False], kind="stable")
    df = df.drop_duplicates(subset=["company_normalized", "title_normalized"], keep="first")
    return df.drop(columns=["_jd_len", "_not_applyable"])


# ---------------------------------------------------------------------
# Step 5 — near dedupe within company
# ---------------------------------------------------------------------

# Level, seniority and track tokens, in canonical spelling. Two titles in one
# company that disagree on these are different roles however close their ratio:
# a level-numbered pair scores 98 on title alone, so ratio by itself deletes a
# real posting.
LEVEL_TOKENS = frozenset({
    "i", "ii", "iii", "iv",
    "intern", "coop", "trainee", "apprentice",
    "junior", "entry", "graduate", "associate",
    "senior", "staff", "principal", "distinguished", "fellow",
    "lead", "manager", "director", "head", "vp", "chief", "president",
})

# Compared as canonical sets, never raw tokens: the same level is routinely
# spelled two ways across boards, and treating the spellings as different
# levels would exempt a genuine duplicate from collapsing.
_LEVEL_SYNONYMS = {
    "jr": "junior", "sr": "senior",
    "mgr": "manager", "mgmt": "manager", "management": "manager",
    "interns": "intern", "internship": "intern", "internships": "intern",
    "grad": "graduate", "apprenticeship": "apprentice",
    "1": "i", "2": "ii", "3": "iii", "4": "iv",
    "vice": "vp",
}
# normalize_title turns "Co-op" into two tokens, so rejoin it before matching.
_CO_OP_RE = re.compile(r"\bco op\b")


def _level_tokens(title: str) -> frozenset[str]:
    tokens = (_LEVEL_SYNONYMS.get(t, t) for t in _CO_OP_RE.sub("coop", title).split())
    return frozenset(LEVEL_TOKENS.intersection(tokens))


def near_dedupe(df: pd.DataFrame, ratio_threshold: float = 90) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    df = df.copy()
    df["_jd_len"] = df["jd_text"].fillna("").astype(str).str.len()
    # Same tie-break as exact_dedupe, for the same reason: an aggregator repost
    # wins on boilerplate by a percent or two and costs the only url that can be
    # submitted to. Step 4 got this and step 5 did not, so a board row could win
    # the exact pass and then lose the fuzzy one — silently, since job_id
    # excludes url. Measured on the 2026-08-10 run: 3 applyable rows lost to a
    # LinkedIn survivor this way.
    df["_not_applyable"] = _not_applyable(df)
    keep_indices: list = []
    for _, group in df.groupby("company_normalized", sort=False):
        g = group.sort_values(["_not_applyable", "_jd_len"],
                               ascending=[True, False], kind="stable")
        kept: list[tuple[str, frozenset[str]]] = []
        for idx, title in zip(g.index, g["title_normalized"]):
            levels = _level_tokens(title)
            if any(levels == kept_levels and fuzz.WRatio(title, kt) >= ratio_threshold
                   for kt, kept_levels in kept):
                continue
            kept.append((title, levels))
            keep_indices.append(idx)
    return df.loc[keep_indices].drop(columns=["_jd_len", "_not_applyable"]).copy()


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
        except (OSError, ValueError, KeyError, yaml.YAMLError) as e:
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
    write_parquet(ledger, ledger_path)
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
    state_map = {jid: d["state"] for jid, d in load_state_index(pipeline_dir).items()
                 if isinstance(d.get("state"), str)}
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
    including skip) never expire — state.yaml is the permanent memory.

    source="manual" is exempt for the same reason it is exempt from
    drop_stale: an inbox clip or a URL ingest is a deliberate user add, and
    expiry is keyed on ledger first_seen, so re-adding a role last seen past
    its retention window would drop the row the user just asked for."""
    if df.empty or ledger is None or not len(ledger):
        return df.copy()
    today = pd.Timestamp(today).normalize()
    lifetimes = ledger["last_score"].apply(_lifetime_days)
    expires_at = ledger["first_seen"] + pd.to_timedelta(lifetimes, unit="D")
    expired_ids = set(ledger.loc[today > expires_at, "job_id"])
    if not expired_ids:
        return df.copy()
    drop = df["job_id"].isin(expired_ids) & ~df["already_seen"]
    if "source" in df.columns:
        drop &= df["source"] != "manual"
    return df[~drop].copy()


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
    # overrides a vertical the scrape already set from the search term.
    needs_fallback = df["vertical"] == ""
    if needs_fallback.any():
        df.loc[needs_fallback, "vertical"] = (
            df.loc[needs_fallback, "title"].apply(classify_vertical_from_title)
        )
    if "remote_flag" not in df.columns:
        df["remote_flag"] = False
    else:
        # .where, not .fillna — fillna on an object column downcasts, and the
        # trailing astype already fixes the type. NaN is truthy, so the null
        # fill has to happen before the cast, not via astype alone.
        flag = df["remote_flag"]
        df["remote_flag"] = flag.where(flag.notna(), False).astype(bool)
    if "scraped_date" not in df.columns:
        df["scraped_date"] = pd.Timestamp.today().normalize()
    df["scraped_date"] = naive_datetime(df["scraped_date"])
    if "posted_date" not in df.columns:
        df["posted_date"] = pd.NaT
    for col in ("salary_min", "salary_max"):
        if col not in df.columns:
            df[col] = float("nan")
    return df


def _term_pattern(term: str) -> str:
    r"""Escaped `term` with a word boundary only on a side that ends in a word
    character. \b needs a word/non-word transition, so a trailing \b after
    "c++" or a leading one before ".net" can never be satisfied and the term
    matches nothing. Same rule as lint.py's phrase compiler, decided per side
    so ".net" still refuses to match a bare "net"."""
    pat = re.escape(term)
    if term[:1].isalnum():
        pat = r"\b" + pat
    if term[-1:].isalnum():
        pat = pat + r"\b"
    return pat


def apply_title_exclusion(df: pd.DataFrame, cfg) -> tuple[pd.DataFrame, dict[str, int]]:
    """Per-vertical title gate (word-boundary). A non-manual row is kept iff:
        strong_keep  OR  (include_ok AND NOT exclude)
    where include_ok is True when the vertical configures no
    title_include_terms — i.e. the include-gate is OFF and this reduces to
    the historical exclusion-only behavior (drop rows matching
    title_exclude_terms). title_strong_keep_terms override every exclude, so
    an unambiguous in-lane title survives even when it also trips an exclude.
    Manual/URL-ingested
    rows are always exempt. Returns (filtered_df, drops_per_vertical)."""
    if df.empty:
        return df.copy(), {}

    df = df.copy()
    drops_per_vertical = {}

    def _compile(terms):
        if not terms:
            return None
        return re.compile("|".join(_term_pattern(t) for t in terms), re.IGNORECASE)

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
    Raises KeyError on any missing required column — fail loud."""
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
    max_age_days: int = MAX_AGE_DAYS,
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
        except (OSError, ValueError, KeyError, yaml.YAMLError) as e:
            log.error("Failed to read %s: %s", path, e)
    if not frames:
        return pd.DataFrame()
    return _concat_raw_frames(frames)


def _concat_raw_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Concat raw shards, excluding entries pandas would ignore for dtype
    inference anyway (zero-row shards, and all-NA columns of a column that is
    typed in some other shard — e.g. min_amount, which boards leave empty and
    the job sites fill). Concat realigns the dropped columns back as NaN, so the
    result is unchanged; doing it here pins the dtype to the typed shards
    instead of leaving it to a deprecated pandas behavior.
    """
    non_empty = [f for f in frames if not f.empty]
    if not non_empty:
        return pd.concat(frames, ignore_index=True)
    typed_somewhere = {
        c for f in non_empty for c in f.columns if not f[c].isna().all()
    }
    cleaned = []
    for f in non_empty:
        blank = [
            c for c in f.columns if c in typed_somewhere and f[c].isna().all()
        ]
        cleaned.append(f.drop(columns=blank) if blank else f)
    return pd.concat(cleaned, ignore_index=True)


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
        f"- after blank-company drop: {stats.get('after_blank_company', 0)} "
        f"(dropped {stats.get('dropped_blank_company', 0)})",
        f"- after short-JD drop (<{MIN_JD_CHARS} chars): {stats.get('after_short_jd', 0)} "
        f"(dropped {stats.get('dropped_short', 0)})",
        f"- after stale drop (>{MAX_AGE_DAYS}d): {stats.get('after_stale', 0)} "
        f"(dropped {stats.get('dropped_stale', 0)})",
        f"- after location filter: {stats.get('after_location', 0)} "
        f"(dropped {stats.get('dropped_location', 0)})",
        f"- after exact dedupe: {stats.get('after_exact_dedupe', 0)} "
        f"(dropped {stats.get('dropped_exact', 0)})",
        f"- after near dedupe (WRatio>=90): {stats.get('after_near_dedupe', 0)} "
        f"(dropped {stats.get('dropped_near', 0)})",
        f"- after seen-ledger expiry: {stats.get('after_expiry', 0)} "
        f"(dropped {stats.get('dropped_expired', 0)})",
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

    # Named, because a near-dup drop is the one silent way a tracked role
    # leaves clean.parquet.
    near_dropped = stats.get("near_dropped", [])
    if near_dropped:
        lines += ["### Near-dup rows dropped", ""]
        lines += [f"- {row}" for row in near_dropped]
        lines.append("")

    with report_path.open("a", encoding="utf-8") as f:
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
    write_parquet(df, clean_dir / "clean.parquet")
    preview_path = clean_dir / "clean.preview.jsonl"
    preview_cols = [c for c in PREVIEW_COLUMNS if c in df.columns]
    with preview_path.open("w", encoding="utf-8") as f:
        for _, row in df[preview_cols].iterrows():
            d = row.to_dict()
            for k, v in list(d.items()):
                # NaT is neither Timestamp nor float; null it explicitly, or
                # json's default=str writes the string "NaT".
                if v is pd.NaT or (isinstance(v, float) and pd.isna(v)):
                    d[k] = None
                elif isinstance(v, pd.Timestamp):
                    d[k] = v.isoformat()
            f.write(json.dumps(d, default=str) + "\n")
    _append_cleaning_section(runs_dir / f"{run_id}.md", run_id, stats)


# ---------------------------------------------------------------------
# Orchestration — the module-docstring steps in exact order
# ---------------------------------------------------------------------

def run(
    run_id: str,
    raw_dir: Path = paths.JOBS_RAW,
    clean_dir: Path = paths.JOBS,
    runs_dir: Path = paths.JOBS_RUNS,
    pipeline_dir: Path = paths.PIPELINE,
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
    # Counted before any drop below it, so the per-source table's raw column
    # still reconciles against after_exclusion.
    per_source_raw = df["source"].value_counts().to_dict() if not df.empty else {}
    # A blank company makes job_id a function of the title alone, so two rows
    # from different employers collide and exact_dedupe deletes one.
    blank_company = df["company_normalized"].fillna("").str.strip() == "" if not df.empty else None
    dropped_blank_company = int(blank_company.sum()) if blank_company is not None else 0
    if dropped_blank_company:
        for c, t in zip(df.loc[blank_company, "company"], df.loc[blank_company, "title"]):
            log.warning("dropping row with unusable company: company=%r title=%r", c, t)
        df = df[~blank_company].copy()
    after_blank_company = len(df)
    # step 2
    df = drop_short_jd(df)
    after_short = len(df)
    # step 3
    df = drop_stale(df, today=today)
    after_stale = len(df)
    # step 3b — location filter. Runs before dedupe so the survivor of a
    # company+title group is picked among eligible rows only, and before the
    # seen-ledger so an allowlist change isn't masked by stale first_seen.
    df = filter_and_canonicalize_location(df, cfg)
    after_location = len(df)
    # step 4
    df = exact_dedupe(df)
    after_exact = len(df)
    # step 5
    before_near = df
    df = near_dedupe(df)
    after_near = len(df)
    # A near-dup drop is the one silent way a role being actively tracked
    # leaves clean.parquet, so name the casualties in the run report.
    near_dropped = [
        f"{compute_job_id(c, t)} {c} | {t}"
        for c, t in zip(
            before_near.loc[before_near.index.difference(df.index), "company_normalized"],
            before_near.loc[before_near.index.difference(df.index), "title_normalized"],
        )
    ]
    # step 6
    df["job_id"] = [
        compute_job_id(c, t)
        for c, t in zip(df["company_normalized"], df["title_normalized"])
    ]
    # step 7 — seen-ledger
    today_ts = pd.Timestamp.today().normalize() if today is None else pd.Timestamp(today).normalize()
    ledger_path = clean_dir / "seen.parquet"

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
        "after_blank_company": after_blank_company,
        "dropped_blank_company": dropped_blank_company,
        "after_short_jd": after_short,
        # Every dropped_* chains off its predecessor, never raw_rows.
        "dropped_short": after_blank_company - after_short,
        "after_stale": after_stale,
        "dropped_stale": after_short - after_stale,
        "after_location": after_location,
        "dropped_location": after_stale - after_location,
        "after_exact_dedupe": after_exact,
        "dropped_exact": after_location - after_exact,
        "after_near_dedupe": after_near,
        "dropped_near": after_exact - after_near,
        "near_dropped": near_dropped,
        "after_expiry": after_expiry,
        "dropped_expired": after_near - after_expiry,
        "final_rows": len(df),
        "per_source": per_source,
    }
    pruned = prune_raw_files(raw_dir, cfg, today_ts)
    stats["pruned_raw"] = pruned
    write_outputs(df, clean_dir, runs_dir, run_id, stats)
    return df
