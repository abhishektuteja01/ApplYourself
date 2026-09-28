"""The jobs store and the live view built from it.

`jobs/jobs.parquet` holds one row per posting, keyed by `posting_key`, with
the `job_id` it was given and the dates it was first and last seen. A night
only adds to it: known postings are touched, new ones get an id. Nothing is
re-derived from raw files.

`jobs/clean.parquet` is the view downstream reads: one row per live `job_id`,
exactly CLEAN_COLUMNS.

Deterministic and LLM-free (R7). Reads pipeline/*/state.yaml, never writes it
(R10).
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

from src import paths
from src.ats_registry import board_slug, is_applyable
from src.discovery import normalize as N
from src.discovery.config import JOBSPY_SOURCES
from src.parquet_io import locked, write_parquet
from src.state_io import ACTIVE_STATES

log = logging.getLogger(__name__)

STORE = paths.STORE
CLEAN = paths.CLEAN

STORE_COLUMNS: list[str] = [
    "posting_key", "job_id", "source", "board",
    "company", "company_normalized", "title", "title_normalized", "vertical",
    "location_raw", "url", "jd_text", "posted_date", "remote_flag",
    "salary_min", "salary_max", "salary_currency", "employment_type", "seniority_raw",
    "found_by_term", "found_by_remote", "updated_at",
    "first_seen", "last_seen", "last_score",
]
_STRING_COLUMNS = [
    "posting_key", "job_id", "source", "board", "company", "company_normalized",
    "title", "title_normalized", "vertical", "location_raw", "url", "jd_text",
    "salary_currency", "employment_type", "seniority_raw", "found_by_term", "updated_at",
]
_DATE_COLUMNS = ["posted_date", "first_seen", "last_seen"]
# Fields a fresh copy of a known posting may update. job_id and first_seen
# never move.
_REFRESH_FIELDS = [
    "company", "title", "vertical", "location_raw", "url", "jd_text", "posted_date",
    "remote_flag", "salary_min", "salary_max", "salary_currency", "employment_type",
    "seniority_raw", "updated_at", "board",
]

# Closed schema, fixed order: what every downstream reader of clean.parquet
# expects.
CLEAN_COLUMNS: list[str] = [
    "job_id", "source", "company", "company_normalized",
    "title", "title_normalized", "location", "posted_date",
    "url", "jd_text", "salary_min", "salary_max", "salary_currency",
    "employment_type", "seniority_raw", "vertical", "application_status",
]



# ---------------------------------------------------------------------
# load / save
# ---------------------------------------------------------------------

def empty_store() -> pd.DataFrame:
    return coerce_store(pd.DataFrame(columns=STORE_COLUMNS))


def coerce_store(df: pd.DataFrame) -> pd.DataFrame:
    df = df.reindex(columns=STORE_COLUMNS).copy()
    for col in _STRING_COLUMNS:
        df[col] = df[col].where(df[col].notna(), "").astype(str)
    for col in _DATE_COLUMNS:
        df[col] = N.naive_datetime(df[col]).dt.normalize()
    for col in ("salary_min", "salary_max", "last_score"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ("remote_flag", "found_by_remote"):
        df[col] = df[col].where(df[col].notna(), False).astype(bool)
    return df.reset_index(drop=True)


def load_store(path: Path | None = None) -> pd.DataFrame:
    path = path or STORE
    if not Path(path).exists():
        return empty_store()
    return coerce_store(pd.read_parquet(path))


def save_store(df: pd.DataFrame, path: Path | None = None) -> None:
    path = path or STORE
    df = coerce_store(df).sort_values("posting_key", kind="stable")
    write_parquet(df.reset_index(drop=True), path)


# ---------------------------------------------------------------------
# a night's changes, collected from the lane threads
# ---------------------------------------------------------------------

def make_posting(**fields) -> dict:
    """A store-shaped row from a source. `url` is required; the key, the
    normalized names and the board are derived here, once."""
    row = {c: "" for c in _STRING_COLUMNS}
    row.update({"posted_date": None, "remote_flag": False, "found_by_remote": False,
                "salary_min": None, "salary_max": None, "last_score": None})
    # "_"-prefixed fields ride along for the lane (a posting id to fetch the
    # JD with) and never reach the store: coerce_store keeps STORE_COLUMNS only.
    row.update({k: v for k, v in fields.items()
                if k in STORE_COLUMNS or k == "key_url" or k.startswith("_")})
    for col in _STRING_COLUMNS:
        if row[col] is None:
            row[col] = ""
    key_url = row.pop("key_url", "") or row["url"]
    row["posting_key"] = row["posting_key"] or N.posting_key(key_url)
    if not row["company"].strip():
        row["company"] = N.company_from_url(row["url"]) or N.company_from_url(key_url)
    row["company_normalized"] = N.normalize_company(row["company"])
    row["title_normalized"] = N.normalize_title(row["title"])
    if not row["board"]:
        hit = board_slug(key_url)
        row["board"] = f"{row['source']}|{hit.lower()}" if hit else ""
    return row


@dataclass
class Batch:
    """New or refreshed postings and touched keys, added from any thread."""

    rows: dict[str, dict] = field(default_factory=dict)
    touched: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def upsert(self, row: dict) -> None:
        if not row.get("posting_key"):
            return
        with self._lock:
            # First copy wins inside one night: query order is config order.
            self.rows.setdefault(row["posting_key"], row)

    def touch(self, key: str) -> None:
        if key:
            with self._lock:
                self.touched.add(key)

    def take(self) -> "Batch":
        with self._lock:
            out = Batch(rows=self.rows, touched=self.touched)
            self.rows, self.touched = {}, set()
            return out

    def __len__(self) -> int:
        return len(self.rows) + len(self.touched)


# ---------------------------------------------------------------------
# job_id assignment
# ---------------------------------------------------------------------

TITLE_MIN = 90
COMPANY_MIN = 85
JD_MIN = 70
JD_WINDOW = 4000
_SQUASH_RE = re.compile(r"[^a-z0-9]")
# Two titles that disagree on these tokens are different roles, however close
# the rest of the title and the JD are.
LEVEL_TOKENS = frozenset({
    "i", "ii", "iii", "iv", "intern", "coop", "trainee", "apprentice",
    "junior", "entry", "graduate", "associate", "senior", "staff", "principal",
    "distinguished", "fellow", "lead", "manager", "director", "head", "vp",
    "chief", "president",
})
_LEVEL_SYNONYMS = {
    "jr": "junior", "sr": "senior", "mgr": "manager", "mgmt": "manager",
    "management": "manager", "interns": "intern", "internship": "intern",
    "internships": "intern", "grad": "graduate", "apprenticeship": "apprentice",
    "1": "i", "2": "ii", "3": "iii", "4": "iv", "vice": "vp",
}
_CO_OP_RE = re.compile(r"\bco op\b")


def level_tokens(title_normalized: str) -> frozenset[str]:
    tokens = (_LEVEL_SYNONYMS.get(t, t)
              for t in _CO_OP_RE.sub("coop", title_normalized or "").split())
    return frozenset(LEVEL_TOKENS.intersection(tokens))


def squash(company_normalized: str) -> str:
    return _SQUASH_RE.sub("", (company_normalized or "").lower())


def same_job_elsewhere(new: dict, old: dict) -> bool:
    """The one fuzzy rule: the same role found by a different source. Never
    two postings of one board, which lists each of its jobs once."""
    if new["source"] == old["source"]:
        return False
    if new["board"] and new["board"] == old["board"]:
        return False
    if fuzz.WRatio(new["title_normalized"], old["title_normalized"]) < TITLE_MIN:
        return False
    if level_tokens(new["title_normalized"]) != level_tokens(old["title_normalized"]):
        return False
    a, b = squash(new["company_normalized"]), squash(old["company_normalized"])
    if not (a and a == b) and fuzz.token_set_ratio(
            new["company_normalized"], old["company_normalized"]) < COMPANY_MIN:
        return False
    jd_a, jd_b = new["jd_text"][:JD_WINDOW], old["jd_text"][:JD_WINDOW]
    return bool(jd_a and jd_b) and fuzz.ratio(jd_a, jd_b) >= JD_MIN


class _Index:
    """Lookups over the store for id assignment, kept current as rows are
    added so two new postings in one night agree with each other."""

    def __init__(self, store: pd.DataFrame, tracked: frozenset[str]):
        self.tracked = tracked
        self.by_key: dict[str, str] = {}
        self.by_pair: dict[str, str] = {}
        self.pair_of_id: dict[str, set[str]] = {}
        self.by_squash: dict[str, list[dict]] = {}
        self.by_title: dict[str, list[dict]] = {}
        # Tracked first, then oldest, then smallest id: which id a pair keeps
        # when the store holds more than one.
        order = store.assign(_t=~store["job_id"].isin(tracked)).sort_values(
            ["_t", "first_seen", "job_id"], kind="stable")
        for rec in order.to_dict("records"):
            self.add(rec)

    def add(self, rec: dict) -> None:
        jid = rec["job_id"]
        self.by_key.setdefault(rec["posting_key"], jid)
        url_key = N.posting_key(rec.get("url", ""))
        if url_key:
            self.by_key.setdefault(url_key, jid)
        pair = f"{rec['company_normalized']}|{rec['title_normalized']}"
        self.by_pair.setdefault(pair, jid)
        self.pair_of_id.setdefault(jid, set()).add(pair)
        cand = {k: rec.get(k, "") for k in (
            "job_id", "source", "board", "company_normalized", "title_normalized", "jd_text")}
        s = squash(rec["company_normalized"])
        if s:
            self.by_squash.setdefault(s, []).append(cand)
        if rec["title_normalized"]:
            self.by_title.setdefault(rec["title_normalized"], []).append(cand)

    def candidates(self, row: dict) -> list[dict]:
        s = squash(row["company_normalized"])
        seen, out = set(), []
        pools = [self.by_squash.get(s, []), self.by_title.get(row["title_normalized"], [])]
        if len(s) >= 4:
            pools += [v for k, v in self.by_squash.items()
                      if k != s and len(k) >= 4 and (k.startswith(s) or s.startswith(k))]
        for pool in pools:
            for cand in pool:
                marker = id(cand)
                if marker not in seen:
                    seen.add(marker)
                    out.append(cand)
        return out

    def assign(self, row: dict) -> tuple[str, str]:
        """(job_id, rule) for a posting the store has not seen."""
        for key in (row["posting_key"], N.posting_key(row["url"])):
            if key and key in self.by_key:
                return self.by_key[key], "url"
        pair = f"{row['company_normalized']}|{row['title_normalized']}"
        if pair in self.by_pair:
            return self.by_pair[pair], "pair"
        matches = sorted(c["job_id"] for c in self.candidates(row) if same_job_elsewhere(row, c))
        if matches:
            tracked = [m for m in matches if m in self.tracked]
            return (tracked or matches)[0], "fuzzy"
        jid = N.compute_job_id(row["company_normalized"], row["title_normalized"])
        if jid in self.pair_of_id and pair not in self.pair_of_id[jid]:
            return "", "collision"
        return jid, "hash"


@dataclass
class ApplyStats:
    inserted: int = 0
    refreshed: int = 0
    touched: int = 0
    reposted: int = 0
    by_rule: dict[str, int] = field(default_factory=dict)
    new_job_ids: list[str] = field(default_factory=list)
    collisions: list[str] = field(default_factory=list)
    dropped_blank: int = 0

    def merge(self, other: "ApplyStats") -> None:
        self.inserted += other.inserted
        self.refreshed += other.refreshed
        self.touched += other.touched
        self.reposted += other.reposted
        for k, v in other.by_rule.items():
            self.by_rule[k] = self.by_rule.get(k, 0) + v
        self.new_job_ids = sorted(set(self.new_job_ids) | set(other.new_job_ids))
        self.collisions += other.collisions
        self.dropped_blank += other.dropped_blank


def apply_batch(store: pd.DataFrame, batch: Batch, today: pd.Timestamp,
                tracked: frozenset[str], live_days: int) -> tuple[pd.DataFrame, ApplyStats]:
    """The store with `batch` folded in. Known keys are refreshed or touched;
    new keys get an id by the first rule that matches: a known url, a known
    company+title pair, the same role from another source, else the hash."""
    today = pd.Timestamp(today).normalize()
    stats = ApplyStats()
    store = coerce_store(store)
    pos = {k: i for i, k in enumerate(store["posting_key"])}
    known_ids = set(store["job_id"])
    gap = pd.Timedelta(days=live_days)

    def seen_again(i: int) -> None:
        # Absent longer than the live window: a repost, so its clock restarts.
        last = store.at[i, "last_seen"]
        if pd.notna(last) and today - last > gap:
            store.at[i, "first_seen"] = today
            stats.reposted += 1
        store.at[i, "last_seen"] = max(today, last) if pd.notna(last) else today

    refresh = {k: r for k, r in batch.rows.items() if k in pos}
    for key in sorted(batch.touched - set(refresh)):
        if key in pos:
            seen_again(pos[key])
            stats.touched += 1
    for key in sorted(refresh):
        i, row = pos[key], refresh[key]
        for col in _REFRESH_FIELDS:
            value = row.get(col)
            if col == "jd_text" and not str(value or "").strip():
                continue
            if value is None or value is pd.NaT or value == "":
                continue
            if col == "posted_date":
                value = pd.Timestamp(value).normalize()
            store.at[i, col] = value
        store.at[i, "company_normalized"] = N.normalize_company(store.at[i, "company"])
        store.at[i, "title_normalized"] = N.normalize_title(store.at[i, "title"])
        seen_again(i)
        stats.refreshed += 1

    new_rows = [batch.rows[k] for k in sorted(batch.rows) if k not in pos]
    if not new_rows:
        return store, stats
    index = _Index(store, tracked)
    added = []
    for row in new_rows:
        if not row["company_normalized"] or not row["title_normalized"]:
            stats.dropped_blank += 1
            continue
        jid, rule = index.assign(row)
        if rule == "collision":
            stats.collisions.append(
                f"{row['company_normalized']} | {row['title_normalized']} ({row['posting_key']})")
            continue
        rec = {**row, "job_id": jid, "first_seen": today, "last_seen": today}
        index.add(rec)
        added.append(rec)
        stats.by_rule[rule] = stats.by_rule.get(rule, 0) + 1
        if jid not in known_ids:
            known_ids.add(jid)
            stats.new_job_ids.append(jid)
    stats.inserted = len(added)
    if added:
        fresh = coerce_store(pd.DataFrame(added))
        store = fresh if store.empty else pd.concat([store, fresh], ignore_index=True)
    stats.new_job_ids.sort()
    return store, stats


def flush(batch: Batch, today: pd.Timestamp, tracked: frozenset[str], live_days: int,
          path: Path | None = None) -> ApplyStats:
    """Empty `batch` into the store: read, fold in, write, under the store's
    lock, so a concurrent `ingest-url` waits rather than being overwritten."""
    path = path or STORE
    pending = batch.take()
    with locked(path):
        store, stats = apply_batch(load_store(path), pending, today, tracked, live_days)
        save_store(store, path)
    return stats


def remember_scores(store: pd.DataFrame, scores: dict[str, float]) -> pd.DataFrame:
    """Copy each job's current fit_score onto its postings. /score deletes the
    score of a job that leaves the view, so the store is what remembers it,
    and what keeps an expired low scorer from coming back to be judged again."""
    fresh = store["job_id"].map(scores)
    store = store.copy()
    store["last_score"] = pd.to_numeric(fresh, errors="coerce").combine_first(store["last_score"])
    return store


def forget(store: pd.DataFrame, today: pd.Timestamp, forget_after_days: int,
           tracked: frozenset[str]) -> tuple[pd.DataFrame, int]:
    """Drop untracked postings not seen for `forget_after_days`."""
    cutoff = pd.Timestamp(today).normalize() - pd.Timedelta(days=forget_after_days)
    old = (store["last_seen"] < cutoff) & ~store["job_id"].isin(tracked)
    return store[~old].reset_index(drop=True), int(old.sum())


# ---------------------------------------------------------------------
# the live view: clean.parquet
# ---------------------------------------------------------------------

@dataclass
class ViewInputs:
    today: pd.Timestamp
    state_index: dict
    scores: dict[str, float]
    live_days: int
    search_max_age_days: int
    score_threshold: float
    high_days: int
    low_days: int


def read_scores(scored_path: Path) -> dict[str, float]:
    if not Path(scored_path).exists():
        return {}
    try:
        scored = pd.read_parquet(scored_path, columns=["job_id", "fit_score"])
    except (OSError, ValueError, KeyError) as e:
        log.warning("could not read %s: %s", scored_path, e)
        return {}
    return dict(zip(scored["job_id"], scored["fit_score"]))


def inputs_from(cfg, today, state_index: dict, scored_path: Path) -> ViewInputs:
    scores = read_scores(scored_path)
    return ViewInputs(
        today=pd.Timestamp(today).normalize(), state_index=state_index, scores=scores,
        live_days=cfg.live_days, search_max_age_days=cfg.search_max_age_days,
        score_threshold=cfg.retention_score_threshold,
        high_days=cfg.retention_high_days, low_days=cfg.retention_low_days)


def build_view(store: pd.DataFrame, allowlist, inp: ViewInputs,
               vcfg=None) -> tuple[pd.DataFrame, dict]:
    """One row per job_id downstream should see, and the funnel that got
    there. Every count is a subset of the one before it, plus the active
    tracked roles added back at the end."""
    today = inp.today
    tracked = frozenset(inp.state_index)
    active = frozenset(j for j, d in inp.state_index.items() if d.get("state") in ACTIVE_STATES)
    df = coerce_store(store)
    funnel = {"store_postings": len(df)}

    live = df["last_seen"] >= today - pd.Timedelta(days=inp.live_days)
    df_live = df[live]
    funnel["live"] = len(df_live)

    # Only search rows are cut on age; a board listing is its own liveness
    # signal. A search row with no posted date is kept: retention bounds it.
    posted = df_live["posted_date"]
    fresh = (~df_live["source"].isin(JOBSPY_SOURCES) | posted.isna()
             | (posted >= today - pd.Timedelta(days=inp.search_max_age_days))
             | df_live["job_id"].isin(tracked))
    df_live = df_live[fresh]
    funnel["after_search_age"] = len(df_live)

    gate = [N.title_passes(t, v, s, vcfg) for t, v, s in
            zip(df_live["title"], df_live["vertical"], df_live["source"])]
    df_live = df_live[pd.Series(gate, index=df_live.index, dtype=bool)]
    funnel["after_title_gate"] = len(df_live)

    checks = [allowlist.check(raw, v, t) for raw, v, t in
              zip(df_live["location_raw"], df_live["vertical"], df_live["title"])]
    keep = pd.Series([c.keep for c in checks], index=df_live.index, dtype=bool)
    df_live = df_live.assign(location=[c.location for c in checks],
                             _resolved=[c.resolved for c in checks])[keep]
    funnel["after_location"] = len(df_live)

    long_enough = (df_live["jd_text"].str.strip().str.len() >= N.MIN_JD_CHARS) \
        | df_live["job_id"].isin(tracked)
    df_live = df_live[long_enough]
    funnel["after_short_jd"] = len(df_live)

    view = _one_per_job(df_live)
    funnel["jobs"] = len(view)

    first_seen = df.groupby("job_id")["first_seen"].min()
    remembered = df.groupby("job_id")["last_score"].max()
    lifetimes = []
    for jid in view["job_id"]:
        score = inp.scores.get(jid)
        if score is None or pd.isna(score):
            score = remembered.get(jid)
        high = score is None or pd.isna(score) or score >= inp.score_threshold
        lifetimes.append(inp.high_days if high else inp.low_days)
    expires = view["job_id"].map(first_seen) + pd.to_timedelta(lifetimes, unit="D")
    within = (today <= expires) | view["job_id"].isin(tracked) | (view["source"] == "manual")
    view = view[within.to_numpy()]
    funnel["after_retention"] = len(view)

    missing = sorted(active - set(view["job_id"]))
    if missing:
        rest = df[df["job_id"].isin(missing)].assign(_resolved=False)
        rest = rest.assign(location=rest["location_raw"])
        back = _one_per_job(rest)
        view = pd.concat([view, back], ignore_index=True) if len(back) else view
    funnel["active_added"] = len(view) - funnel["after_retention"]

    state_map = {j: d.get("state", "") for j, d in inp.state_index.items()
                 if isinstance(d.get("state"), str)}
    view = view.assign(application_status=view["job_id"].map(state_map).fillna(""))
    view = view[CLEAN_COLUMNS].sort_values("job_id", kind="stable").reset_index(drop=True)
    funnel["clean"] = len(view)
    return view, funnel


def _one_per_job(df: pd.DataFrame) -> pd.DataFrame:
    """The representative posting of each job_id: an applyable url, then a
    resolved location, then the newest sighting, then the longest JD."""
    if df.empty:
        return df.drop(columns=["_resolved"], errors="ignore")
    work = df.assign(
        _apply=[not is_applyable(u) for u in df["url"]],
        _unres=~df["_resolved"].astype(bool),
        _jd=df["jd_text"].str.len(),
    ).sort_values(["job_id", "_apply", "_unres", "last_seen", "_jd", "posting_key"],
                  ascending=[True, True, True, False, False, True], kind="stable")
    out = work.drop_duplicates("job_id", keep="first")
    return out.drop(columns=["_apply", "_unres", "_jd", "_resolved"])


def write_view(view: pd.DataFrame, path: Path | None = None) -> None:
    path = path or CLEAN
    with locked(path):
        write_parquet(view, path)
