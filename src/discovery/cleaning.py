"""Deterministic cleaning.

No LLM calls (R7). Reads pipeline/*/state.yaml but never writes there.
Operations execute in this exact step order. Steps 0-3b are row-local, so they
run per raw shard inside load_filtered_window rather than once on the
concatenated window; steps 5+ need the whole window and run after the concat.
  0. Per-vertical title gate (apply_title_exclusion), before everything else
  1. Normalize company / title fields (seniority preserved)
  1b. Drop rows with a blank company_normalized — job_id would key on title alone
  2. Drop rows where jd_text < 200 chars
  3. Drop rows where posted_date < today-14d (missing date kept w/ flag);
     career-board sources exempt — board presence is the liveness signal,
     capped by board_max_age_days when that config key is non-zero;
     rows with a pipeline/<job_id>/state.yaml exempt too, matching step 9
  3b. Drop rows outside the location allowlist
  4. job_id = sha1(company_normalized|title_normalized)[:8]
     url and jd_text deliberately excluded so job_id is stable across re-scrapes.
     Flipping the hash on a URL change would silently orphan
     pipeline/<job_id>/state.yaml and applications/<dir> keys. Assigned before
     dedupe because the merge group's surviving id is picked from the ids its
     own members already carry.
  5. Dedupe — one evidence-based pass (discovery/dedupe.py). Candidate pairs are
     blocked on url, board slug, squashed company key, company-name containment
     and identical title; a pair merges only on an identical url, a shared board
     tenant plus a near-identical title, or a near-identical title with a close
     company name AND similar jd_text. The survivor is the applyable url, then
     the in-allowlist location, then the longest jd_text; its id comes from
     aliases.select_sticky_id and every merged company spelling is pinned in the
     append-only jobs/company_aliases.parquet.
  6. Update jobs/seen.parquet: refresh last_score from
     scored.parquet (deterministic READ of Claude's sidecar — R7 intact),
     purge rows RESURFACE_AFTER_DAYS past expiry, stamp first_seen for new ids
  7. Glob pipeline/*/state.yaml -> set already_seen + application_status
  8. Drop expired rows per the seen-ledger tiers (tracked rows never expire;
     never-scored rows get the high tier — NaN means unjudged, not bad)
  9. Initialize Claude-owned columns with defaults
  10. coerce_schema (raises KeyError on a missing column), prune_raw_files
      (deletes raw shards older than raw_retention_days), then write
      clean.parquet + clean.preview.jsonl + ## Cleaning run-report section
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

import pandas as pd

from src import verticals
from src.discovery import aliases
from src.discovery import dedupe as dedupe_mod
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
    "location_count", "all_locations",
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
from src.discovery.sources.ats.registry import ATS_SOURCE_NAMES, is_applyable
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
    "location_count", "all_locations",
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
#      a stale empty-vertical row never wins the dedupe tie-break over a freshly
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
# Step 4 — job_id (defined early; reused elsewhere)
# ---------------------------------------------------------------------

def compute_job_id(company_normalized: str, title_normalized: str) -> str:
    key = f"{company_normalized}|{title_normalized}".encode("utf-8")
    return hashlib.sha1(key).hexdigest()[:8]


def drop_job_id_collisions(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Drop rows whose job_id is shared by a different (company, title) pair.

    8 hex chars is 32 bits, so a birthday collision is a matter of time. A
    collision would cross-wire two unrelated roles' pipeline/<job_id>/state.yaml
    and applications/<dir>, so every row involved is dropped rather than written.
    The blast radius is the colliding pair, not the run: losing the night's
    clean.parquet costs more than losing two rows.
    """
    if df.empty:
        return df, []
    pairs = df["company_normalized"].astype(str) + "|" + df["title_normalized"].astype(str)
    distinct_pairs = pairs.groupby(df["job_id"]).nunique()
    colliding = set(distinct_pairs[distinct_pairs > 1].index)
    if not colliding:
        return df, []
    hit = df["job_id"].isin(colliding)
    named = sorted(
        f"{i} {c} | {t}"
        for i, c, t in zip(
            df.loc[hit, "job_id"],
            df.loc[hit, "company_normalized"],
            df.loc[hit, "title_normalized"],
        )
    )
    log.error(
        "job_id collision: %d rows across %d ids dropped — %s",
        int(hit.sum()), len(colliding), "; ".join(named),
    )
    return df[~hit].copy(), named


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
    tracked_ids: frozenset[str] = frozenset(),
    board_max_age_days: int = 0,
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
        exempt = source.isin(exempt_sources)
        if board_max_age_days > 0:
            # Opt-in ceiling on that exemption. 0 (the default) keeps it
            # unbounded.
            board_cutoff = today - pd.Timedelta(days=board_max_age_days)
            exempt &= posted.isna() | (posted >= board_cutoff)
        keep = keep | exempt
    if tracked_ids:
        # A row with a state.yaml outlives its posted_date, the same rule
        # apply_expiry states at step 9. Without it a dated source drops the
        # role here at 14 days and steps 7-9 never get to honor it.
        # job_id is only assigned at step 6, but both its inputs exist from
        # step 1, so recompute rather than reorder the pipeline.
        keep = keep | pd.Series(
            [compute_job_id(c, t) in tracked_ids
             for c, t in zip(df["company_normalized"], df["title_normalized"])],
            index=df.index,
        )
    return df[keep].copy()


# ---------------------------------------------------------------------
# Step 3b — location allowlist filter
# ---------------------------------------------------------------------

def filter_and_canonicalize_location(df: pd.DataFrame, cfg) -> pd.DataFrame:
    if df.empty:
        out = df.copy()
        out["_loc_unresolved"] = pd.Series(dtype=bool)
        return out

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
    # A conservative keep (nothing parsed, or a multi-region ambiguity that
    # merely overlaps the allowlist) loses the dedupe tie-break to a row that
    # positively resolved inside it.
    unresolved = []

    for idx, raw_loc in zip(df.index, df["location"]):
        raw_loc_str = str(raw_loc).strip() if pd.notna(raw_loc) else ""
        parsed = parse_location(raw_loc_str)

        # 1. Nothing parsed at all -> KEEP (conservative default, unchanged).
        if not parsed.country and not parsed.state and not parsed.city and not parsed.candidate_countries:
            keep_indices.append(idx)
            unresolved.append(True)
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
                unresolved.append(True)
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
            unresolved.append(False)
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
    filtered["_loc_unresolved"] = unresolved
    return filtered


# ---------------------------------------------------------------------
# Step 5 — dedupe (the decision lives in discovery/dedupe.py)
# ---------------------------------------------------------------------

def _not_applyable(df: pd.DataFrame) -> pd.Series:
    """False for rows whose url leads to a board application form. Sorted
    ascending, so those rows win their group."""
    if "url" not in df.columns:
        return pd.Series(True, index=df.index)
    url = df["url"].fillna("").astype(str)
    return ~url.map(is_applyable)


def dedupe(
    df: pd.DataFrame,
    ledger: pd.DataFrame | None = None,
    state_index: dict | None = None,
    seen_ids=(),
) -> tuple[pd.DataFrame, list[dict]]:
    """One evidence-based pass over the whole window.

    Applyability outranks jd_text length in the survivor tie-break: an
    aggregator repost wins on appended boilerplate by a percent or two and
    costs the only url that can be submitted to.
    """
    if df.empty:
        return dedupe_mod.resolve(df)
    work = df.copy()
    work["_not_applyable"] = _not_applyable(work)
    return dedupe_mod.resolve(
        work,
        canonical=aliases.canonical_map(ledger) if ledger is not None else {},
        state_index=state_index or {},
        seen_ids=seen_ids,
    )


# ---------------------------------------------------------------------
# Step 6 — seen-ledger (jobs/seen.parquet)
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
        except (OSError, ValueError, KeyError) as e:
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
# Step 7 — state.yaml glob
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
# Step 8 — seen-ledger expiry
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
    its retention window would drop the row the user just asked for.

    A NaN last_score means never judged, not judged badly, so it gets
    RETENTION_HIGH_DAYS — visible until scoring gets to it, but still bounded
    if scoring is abandoned."""
    if df.empty or ledger is None or not len(ledger):
        return df.copy()
    today = pd.Timestamp(today).normalize()
    lifetimes = ledger["last_score"].apply(_lifetime_days).where(
        ledger["last_score"].notna(), RETENTION_HIGH_DAYS
    )
    expires_at = ledger["first_seen"] + pd.to_timedelta(lifetimes, unit="D")
    expired_ids = set(ledger.loc[today > expires_at, "job_id"])
    if not expired_ids:
        return df.copy()
    drop = df["job_id"].isin(expired_ids) & ~df["already_seen"]
    if "source" in df.columns:
        drop &= df["source"] != "manual"
    return df[~drop].copy()


# ---------------------------------------------------------------------
# Step 9 — Claude-owned defaults
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


def _iter_raw_shards(
    raw_dir: Path,
    today: pd.Timestamp | None = None,
    max_age_days: int = MAX_AGE_DAYS,
):
    """Yield one raw shard frame per in-window parquet, oldest filename first.
    Sole place the window cutoff and the filename convention are applied."""
    today = pd.Timestamp.today().normalize() if today is None else pd.Timestamp(today).normalize()
    cutoff = today - pd.Timedelta(days=max_age_days)
    if not raw_dir.exists():
        return
    for path in sorted(raw_dir.glob("*.parquet")):
        ts = _parse_run_ts_from_filename(path.name)
        if ts is None:
            log.warning("Raw filename not YYYY-MM-DD_HHMM.parquet: %s — skipping", path.name)
            continue
        if ts.normalize() < cutoff:
            continue
        try:
            yield pd.read_parquet(path)
        except (OSError, ValueError, KeyError) as e:
            log.error("Failed to read %s: %s", path, e)


def load_raw_window(
    raw_dir: Path,
    today: pd.Timestamp | None = None,
    max_age_days: int = MAX_AGE_DAYS,
) -> pd.DataFrame:
    """The whole window concatenated, unfiltered. `run` uses
    load_filtered_window instead — this stays for callers that want the raw
    frame."""
    frames = list(_iter_raw_shards(raw_dir, today=today, max_age_days=max_age_days))
    if not frames:
        return pd.DataFrame()
    return _concat_raw_frames(frames)


def drop_blank_company(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Step 1b. A blank company makes job_id a function of the title alone, so
    two rows from different employers collide and dedupe deletes one."""
    if df.empty:
        return df.copy(), 0
    blank = df["company_normalized"].fillna("").str.strip() == ""
    dropped = int(blank.sum())
    if not dropped:
        return df, 0
    for s, c, t in zip(df.loc[blank, "source"], df.loc[blank, "company"], df.loc[blank, "title"]):
        log.warning("dropping row with unusable company: source=%r company=%r title=%r", s, c, t)
    return df[~blank].copy(), dropped


def _new_window_stats() -> dict:
    return {
        "raw_rows": 0,
        "after_exclusion": 0,
        "drops_per_vertical": {},
        "per_source_raw": {},
        "dropped_blank_company": 0,
        "after_blank_company": 0,
        "after_short": 0,
        "after_stale": 0,
        "after_location": 0,
    }


def _filter_shard(
    raw: pd.DataFrame,
    stats: dict,
    cfg,
    vcfg,
    tracked_ids: frozenset[str],
    today: pd.Timestamp | None,
) -> pd.DataFrame:
    """Steps 0-3b for one shard. Every filter here is row-local, so running it
    per shard is identical to running it once on the concatenated window — and
    keeps 14 days of full shards from ever being resident at once. Each stats
    counter accumulates, so the run report's chained `dropped_*` arithmetic is
    unchanged."""
    stats["raw_rows"] += len(raw)
    df = project_raw(raw)
    # step 0
    df, drops_per_vertical = apply_title_exclusion(df, vcfg)
    for name, count in drops_per_vertical.items():
        stats["drops_per_vertical"][name] = stats["drops_per_vertical"].get(name, 0) + count
    stats["after_exclusion"] += len(df)
    # step 1
    df["company_normalized"] = df["company"].apply(normalize_company)
    df["title_normalized"] = df["title"].apply(normalize_title)
    # Counted before any drop below it, so the per-source table's raw column
    # still reconciles against after_exclusion.
    if not df.empty:
        for src, count in df["source"].value_counts().items():
            stats["per_source_raw"][src] = stats["per_source_raw"].get(src, 0) + int(count)
    # step 1b
    df, dropped_blank = drop_blank_company(df)
    stats["dropped_blank_company"] += dropped_blank
    stats["after_blank_company"] += len(df)
    # step 2
    df = drop_short_jd(df)
    stats["after_short"] += len(df)
    # step 3 — needs company_normalized/title_normalized (it recomputes job_id)
    # and tracked_ids, which is why both are resolved above the loop.
    df = drop_stale(
        df, today=today,
        tracked_ids=tracked_ids,
        board_max_age_days=cfg.board_max_age_days,
    )
    stats["after_stale"] += len(df)
    # step 3b — location filter. Runs before dedupe so the survivor of a
    # company+title group is picked among eligible rows only, and before the
    # seen-ledger so an allowlist change isn't masked by stale first_seen.
    df = filter_and_canonicalize_location(df, cfg)
    stats["after_location"] += len(df)
    return df


def load_filtered_window(
    raw_dir: Path,
    cfg,
    pipeline_dir: Path,
    today: pd.Timestamp | None = None,
    max_age_days: int = MAX_AGE_DAYS,
) -> tuple[pd.DataFrame, dict]:
    """Stream the window: read one shard, apply steps 0-3b, keep only the
    survivors. Returns (frame ready for dedupe, accumulated stats)."""
    stats = _new_window_stats()
    vcfg = verticals.get_config()
    # Hoisted above the loop: one state.yaml glob for the whole window.
    tracked_ids = frozenset(load_state_index(pipeline_dir))
    frames = [
        _filter_shard(raw, stats, cfg, vcfg, tracked_ids, today)
        for raw in _iter_raw_shards(raw_dir, today=today, max_age_days=max_age_days)
    ]
    if not frames:
        # No shard at all still has to produce the canonical column set, the
        # same way project_raw(pd.DataFrame()) did when run projected the concat.
        frames = [_filter_shard(pd.DataFrame(), _new_window_stats(), cfg, vcfg, tracked_ids, today)]
    return _concat_raw_frames(frames), stats


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
        f"- after dedupe: {stats.get('after_dedupe', 0)} "
        f"(merged {stats.get('dropped_dedupe', 0)})",
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

    # Named, because a merge is the one silent way a tracked role leaves
    # clean.parquet under an id other than its own.
    merged_rows = stats.get("merged_rows", [])
    if merged_rows:
        lines += ["### Merge groups", ""]
        lines += [f"- {row}" for row in merged_rows]
        lines.append("")

    # A collision cross-wires two unrelated roles' state.yaml and applications
    # dir, so every colliding row is dropped and every one of them is named.
    collisions = stats.get("job_id_collisions", [])
    if collisions:
        lines += ["### job_id collision", ""]
        lines += [f"- {row}" for row in collisions]
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
    # steps 0-3b, streamed one raw shard at a time. Dedupe and the seen-ledger
    # below still need the whole window, so they stay here.
    df, window_stats = load_filtered_window(raw_dir, cfg, pipeline_dir, today=today)
    raw_rows = window_stats["raw_rows"]
    after_exclusion = window_stats["after_exclusion"]
    drops_per_vertical = window_stats["drops_per_vertical"]
    per_source_raw = window_stats["per_source_raw"]
    dropped_blank_company = window_stats["dropped_blank_company"]
    after_blank_company = window_stats["after_blank_company"]
    after_short = window_stats["after_short"]
    after_stale = window_stats["after_stale"]
    after_location = window_stats["after_location"]
    today_ts = pd.Timestamp.today().normalize() if today is None else pd.Timestamp(today).normalize()
    ledger_path = clean_dir / "seen.parquet"
    alias_path = clean_dir / "company_aliases.parquet"
    # step 4 — assigned before the dedupe, which picks the surviving id from
    # the ids its own group members already carry.
    df["job_id"] = [
        compute_job_id(c, t)
        for c, t in zip(df["company_normalized"], df["title_normalized"])
    ]
    df, collisions = drop_job_id_collisions(df)
    # step 5
    alias_ledger = aliases.load_ledger(alias_path)
    seen_ids = frozenset(
        pd.read_parquet(ledger_path)["job_id"]
    ) if ledger_path.exists() else frozenset()
    df, merges = dedupe(
        df,
        ledger=alias_ledger,
        state_index=load_state_index(pipeline_dir),
        seen_ids=seen_ids,
    )
    after_dedupe = len(df)
    # A merge is the one silent way a role being actively tracked leaves
    # clean.parquet under a different id, so name every group in the report.
    merged_rows = [
        f"{m['job_id']} <- {' + '.join(m['job_ids'])} ({m['canonical']})"
        for m in merges
    ]
    aliases.write_ledger(
        aliases.append_aliases(
            alias_ledger,
            [
                (variant, m["canonical"], origin)
                for m in merges
                for variant, origin in m["origins"].items()
            ],
            today_ts,
        ),
        alias_path,
    )
    # step 6 — seen-ledger
    ledger = update_seen_ledger(
        df["job_id"].tolist(),
        ledger_path,
        clean_dir / "scored.parquet",
        today_ts,
    )
    # step 7
    df = apply_state_yaml(df, pipeline_dir)
    # step 8 — retention expiry (tracked rows exempt)
    df = apply_expiry(df, ledger, today_ts)
    after_expiry = len(df)
    # step 9
    df = init_claude_columns(df)
    # step 10
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
        "job_id_collisions": collisions,
        "after_dedupe": after_dedupe,
        # Chains off after_location like every other stage; a job_id
        # collision drop is named separately rather than counted here.
        "dropped_dedupe": after_location - after_dedupe,
        "merged_rows": merged_rows,
        "after_expiry": after_expiry,
        "dropped_expired": after_dedupe - after_expiry,
        "final_rows": len(df),
        "per_source": per_source,
    }
    pruned = prune_raw_files(raw_dir, cfg, today_ts)
    stats["pruned_raw"] = pruned
    write_outputs(df, clean_dir, runs_dir, run_id, stats)
    return df
