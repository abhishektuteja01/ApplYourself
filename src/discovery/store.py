"""The jobs store: jobs/jobs.parquet.

One row per posting, keyed by `posting_key`, with the `job_id` it was given and
the dates it was first and last seen. A night only adds to it: known postings
are touched, new ones get an id (dedupe.py). Nothing is re-derived from raw files.

Deterministic and LLM-free (R7). Reads pipeline/*/state.yaml, never writes it
(R10).
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from src.discovery import parse
from src.discovery.dedupe import NO_TRIM, Trim, _Index
from src.shared import identity, paths, schemas
from src.shared.job_boards import board_key
from src.shared.parquet_io import locked, read_parquet, schema_version, write_parquet

log = logging.getLogger(__name__)


STORE_COLUMNS: list[str] = [
    "posting_key",
    "job_id",
    "source",
    "board",
    "company",
    "company_normalized",
    "title",
    "title_normalized",
    "vertical",
    "location_raw",
    "url",
    "jd_text",
    "posted_date",
    "remote_flag",
    "salary_min",
    "salary_max",
    "salary_currency",
    "employment_type",
    "seniority_raw",
    "found_by_term",
    "found_by_remote",
    "updated_at",
    "first_seen",
    "last_seen",
    "last_score",
    "dedupe_key",
]
_STRING_COLUMNS = [
    "posting_key",
    "job_id",
    "source",
    "board",
    "company",
    "company_normalized",
    "title",
    "title_normalized",
    "vertical",
    "location_raw",
    "url",
    "jd_text",
    "salary_currency",
    "employment_type",
    "seniority_raw",
    "found_by_term",
    "updated_at",
    "dedupe_key",
]
_DATE_COLUMNS = ["posted_date", "first_seen", "last_seen"]
# Fields a fresh copy of a known posting may update. job_id, first_seen and
# the id pair (company_normalized, title) never move: a moved pair makes the
# next copy of the same role look like a hash collision. The shown company
# name may improve ("whatfix101" -> "Whatfix").
_REFRESH_FIELDS = [
    "company",
    "vertical",
    "location_raw",
    "url",
    "jd_text",
    "posted_date",
    "remote_flag",
    "salary_min",
    "salary_max",
    "salary_currency",
    "employment_type",
    "seniority_raw",
    "updated_at",
    "board",
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
        df[col] = parse.naive_datetime(df[col]).dt.normalize()
    for col in ("salary_min", "salary_max", "last_score"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ("remote_flag", "found_by_remote"):
        df[col] = df[col].where(df[col].notna(), False).astype(bool)
    fill_dedupe_keys(df)
    return df.reset_index(drop=True)


def fill_dedupe_keys(df: pd.DataFrame) -> None:
    """Compute dedupe_key where blank (a store written before the column)."""
    blank = df["dedupe_key"] == ""
    if blank.any():
        df.loc[blank, "dedupe_key"] = [
            identity.dedupe_key(c, t)
            for c, t in zip(
                df.loc[blank, "company_normalized"], df.loc[blank, "title_normalized"], strict=True
            )
        ]


def load_store(path: Path | None = None) -> pd.DataFrame:
    path = path or paths.STORE
    if not Path(path).exists():
        return empty_store()
    return coerce_store(read_parquet(path, schemas.STORE_SCHEMA_VERSION))


def save_store(df: pd.DataFrame, path: Path | None = None) -> None:
    path = path or paths.STORE
    df = coerce_store(df).sort_values("posting_key", kind="stable")
    write_parquet(df.reset_index(drop=True), path, schema_version=schemas.STORE_SCHEMA_VERSION)


# ---------------------------------------------------------------------
# a night's changes, collected from the lane threads
# ---------------------------------------------------------------------


def make_posting(**fields) -> dict:
    """A store-shaped row from a source. `url` is required; the key, the
    normalized names and the board are derived here, once."""
    row = {c: "" for c in _STRING_COLUMNS}
    row.update(
        {
            "posted_date": None,
            "remote_flag": False,
            "found_by_remote": False,
            "salary_min": None,
            "salary_max": None,
            "last_score": None,
        }
    )
    # "_"-prefixed fields ride along for the lane (a posting id to fetch the
    # JD with) and never reach the store: coerce_store keeps STORE_COLUMNS only.
    row.update(
        {
            k: v
            for k, v in fields.items()
            if k in STORE_COLUMNS or k == "key_url" or k.startswith("_")
        }
    )
    for col in _STRING_COLUMNS:
        if row[col] is None:
            row[col] = ""
    key_url = row.pop("key_url", "") or row["url"]
    row["posting_key"] = row["posting_key"] or identity.posting_key(key_url)
    if not row["company"].strip():
        row["company"] = identity.company_from_url(row["url"]) or identity.company_from_url(key_url)
    row["company_normalized"] = identity.normalize_company(row["company"])
    row["title_normalized"] = identity.normalize_title(row["title"])
    if not row["board"]:
        # The url's own ATS, so one board is one board whichever lane found it.
        row["board"] = board_key(key_url, tenant_only=True)
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

    def take(self) -> Batch:
        with self._lock:
            out = Batch(rows=self.rows, touched=self.touched)
            self.rows, self.touched = {}, set()
            return out

    def __len__(self) -> int:
        return len(self.rows) + len(self.touched)


@dataclass
class ApplyStats:
    inserted: int = 0
    refreshed: int = 0
    touched: int = 0
    reposted: int = 0
    by_rule: dict[str, int] = field(default_factory=dict)
    new_job_ids: list[str] = field(default_factory=list)
    dropped_blank: int = 0
    maybe_duplicates: list[dict] = field(default_factory=list)

    def merge(self, other: ApplyStats) -> None:
        self.inserted += other.inserted
        self.refreshed += other.refreshed
        self.touched += other.touched
        self.reposted += other.reposted
        for k, v in other.by_rule.items():
            self.by_rule[k] = self.by_rule.get(k, 0) + v
        self.new_job_ids = sorted(set(self.new_job_ids) | set(other.new_job_ids))
        self.dropped_blank += other.dropped_blank
        seen = {m["pair"] for m in self.maybe_duplicates}
        self.maybe_duplicates += [m for m in other.maybe_duplicates if m["pair"] not in seen]


def apply_batch(
    store: pd.DataFrame,
    batch: Batch,
    today: pd.Timestamp,
    tracked: frozenset[str],
    live_days: int,
    trim: Trim = NO_TRIM,
    known: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, ApplyStats]:
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
        seen_again(i)
        stats.refreshed += 1

    new_rows = [batch.rows[k] for k in sorted(batch.rows) if k not in pos]
    if not new_rows:
        return store, stats
    index = _Index(store, tracked, trim, known)
    added, maybe_pairs = [], set()
    for row in new_rows:
        if not row["company_normalized"] or not row["title_normalized"]:
            stats.dropped_blank += 1
            continue
        row["dedupe_key"] = identity.dedupe_key(row["company_normalized"], row["title_normalized"])
        jid, rule, maybes = index.assign(row)
        rec = {**row, "job_id": jid, "first_seen": today, "last_seen": today}
        index.add(rec)
        added.append(rec)
        stats.by_rule[rule] = stats.by_rule.get(rule, 0) + 1
        for other, m in sorted(maybes.items()):
            key = "|".join(sorted((jid, other)))
            if key in maybe_pairs:
                continue
            maybe_pairs.add(key)
            c = m["cand"]
            stats.maybe_duplicates.append(
                {
                    "pair": key,
                    "job_id": jid,
                    "other": other,
                    "company": row["company_normalized"],
                    "other_company": c["company_normalized"],
                    "title": row["title_normalized"],
                    "jd_score": round(m["score"], 1),
                    "reason": m["reason"],
                    "date": str(today.date()),
                }
            )
        if jid not in known_ids:
            known_ids.add(jid)
            stats.new_job_ids.append(jid)
    stats.inserted = len(added)
    if added:
        fresh = coerce_store(pd.DataFrame(added))
        store = fresh if store.empty else pd.concat([store, fresh], ignore_index=True)
    stats.new_job_ids.sort()
    return store, stats


def flush(
    batch: Batch,
    today: pd.Timestamp,
    tracked: frozenset[str],
    live_days: int,
    path: Path | None = None,
    trim: Trim = NO_TRIM,
) -> ApplyStats:
    """Empty `batch` into the store: read, fold in, write, under the store's
    lock, so a concurrent `ingest-url` waits rather than being overwritten.
    New maybe duplicates are appended to the store's maybe_duplicates.jsonl.
    The job_ids table beside it, once built (`discover-ids --apply`), is read
    for matching and gains the new ids."""
    path = Path(path or paths.STORE)
    ids_path = path.with_name(paths.JOB_IDS.name)
    pending = batch.take()
    with locked(path):
        known = load_job_ids(ids_path) if ids_path.exists() else None
        store, stats = apply_batch(
            load_store(path), pending, today, tracked, live_days, trim, known
        )
        save_store(store, path)
        if known is not None:
            table = merge_job_ids(known, job_id_rows(store))
            if len(table) > len(known):
                save_job_ids(table, ids_path)
        append_maybe_duplicates(stats.maybe_duplicates, path.with_name(paths.MAYBE_DUPES.name))
    return stats


# ---------------------------------------------------------------------
# job_ids: every (job_id, pair) the store ever held, kept after `forget`
# ---------------------------------------------------------------------

JOB_IDS_COLUMNS = ["job_id", "company_normalized", "title_normalized", "dedupe_key", "first_seen"]
_JOB_IDS_KEY = ["job_id", "company_normalized", "title_normalized"]


def coerce_job_ids(df: pd.DataFrame) -> pd.DataFrame:
    df = df.reindex(columns=JOB_IDS_COLUMNS).copy()
    for col in JOB_IDS_COLUMNS[:-1]:
        df[col] = df[col].where(df[col].notna(), "").astype(str)
    df["first_seen"] = parse.naive_datetime(df["first_seen"]).dt.normalize()
    fill_dedupe_keys(df)
    return df.reset_index(drop=True)


def load_job_ids(path: Path | None = None) -> pd.DataFrame:
    path = Path(path or paths.JOB_IDS)
    if not path.exists():
        return coerce_job_ids(pd.DataFrame(columns=JOB_IDS_COLUMNS))
    schemas.check(schema_version(path), schemas.JOB_IDS_SCHEMA_VERSION, path)
    return coerce_job_ids(pd.read_parquet(path))


def save_job_ids(df: pd.DataFrame, path: Path | None = None) -> None:
    df = coerce_job_ids(df).sort_values(_JOB_IDS_KEY, kind="stable")
    write_parquet(
        df.reset_index(drop=True),
        Path(path or paths.JOB_IDS),
        schema_version=schemas.JOB_IDS_SCHEMA_VERSION,
    )


def job_id_rows(store: pd.DataFrame) -> pd.DataFrame:
    """One table row per (job_id, pair) in `store`, earliest first_seen."""
    rows = store.sort_values("first_seen", kind="stable").drop_duplicates(_JOB_IDS_KEY)
    return coerce_job_ids(rows[JOB_IDS_COLUMNS])


def merge_job_ids(known: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """`known` with `new`'s unseen (job_id, pair) rows added; nothing is dropped
    or changed."""
    new = coerce_job_ids(new).drop_duplicates(_JOB_IDS_KEY)
    if known.empty:
        return new
    seen = pd.MultiIndex.from_frame(known[_JOB_IDS_KEY])
    fresh = new[~pd.MultiIndex.from_frame(new[_JOB_IDS_KEY]).isin(seen)]
    return pd.concat([known, fresh], ignore_index=True) if len(fresh) else known


def load_maybe_duplicates(path: Path | None = None) -> list[dict]:
    """Every recorded maybe-duplicate pair; unreadable lines are skipped."""
    path = Path(path or paths.MAYBE_DUPES)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("pair"):
            schemas.check(rec.get("schema_version"), schemas.MAYBE_DUPES_SCHEMA_VERSION, path)
            out.append(rec)
    return out


def append_maybe_duplicates(rows: list[dict], path: Path | None = None) -> list[dict]:
    """Append the pairs `path` does not hold yet; returns what was written."""
    path = Path(path or paths.MAYBE_DUPES)
    seen = {r["pair"] for r in load_maybe_duplicates(path)}
    new = [r for r in rows if r["pair"] not in seen]
    if new:
        with path.open("a", encoding="utf-8") as fh:
            for r in new:
                rec = {"schema_version": schemas.MAYBE_DUPES_SCHEMA_VERSION, **r}
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return new


def forget(
    store: pd.DataFrame, today: pd.Timestamp, forget_after_days: int, tracked: frozenset[str]
) -> tuple[pd.DataFrame, int]:
    """Drop untracked postings not seen for `forget_after_days`."""
    cutoff = pd.Timestamp(today).normalize() - pd.Timedelta(days=forget_after_days)
    old = (store["last_seen"] < cutoff) & ~store["job_id"].isin(tracked)
    return store[~old].reset_index(drop=True), int(old.sum())
