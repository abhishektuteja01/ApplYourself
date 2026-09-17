"""job_id stability across a company-name merge.

Two deterministic pieces (R7 — no LLM anywhere here):

1. The company alias ledger, `jobs/company_aliases.parquet`. When two spellings
   of one company merge, one spelling feeds the job_id hash. If the winner
   changes between runs the job_id changes and every
   pipeline/<job_id>/state.yaml and applications/<dir> for that role is
   orphaned. The ledger pins the choice: **append-only**, an existing
   variant -> canonical mapping is never rewritten.

2. Sticky-id selection. Within a merge group the id that is already tracked
   survives, so the id follows the role you are working. Reads
   pipeline/*/state.yaml, never writes it (R10 — /track is the sole writer).
"""
from __future__ import annotations

from collections.abc import Container, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src.discovery.sources.ats.registry import detect_source
from src.parquet_io import write_parquet

ALIAS_COLUMNS = (
    "variant_normalized",
    "canonical_normalized",
    "first_seen",
    "source_of_truth",
)

#: A company's own ATS is authoritative for how it spells its name.
SOURCE_RANK = {"board": 2, "manual": 1, "aggregator": 0}

#: Later state wins the sticky id. The four the migration actually sees are
#: applied > tailored > saved > skip; the rest keep the pipeline's own order.
_STATE_ORDER = (
    "skip", "ghosted", "withdrawn", "rejected", "saved", "tailored",
    "applied", "recruiter_contact", "screen", "interview", "offer",
)
STATE_PRECEDENCE = {s: i for i, s in enumerate(_STATE_ORDER)}


@dataclass(frozen=True)
class AliasCandidate:
    """One spelling of a company seen in a merge group."""
    variant_normalized: str
    source_of_truth: str


def source_of_truth(url: str | None) -> str:
    """`board` when the url names a recognized ATS, else `aggregator`."""
    return "board" if detect_source(url or "") else "aggregator"


def empty_ledger() -> pd.DataFrame:
    return pd.DataFrame({
        "variant_normalized": pd.Series(dtype=str),
        "canonical_normalized": pd.Series(dtype=str),
        "first_seen": pd.Series(dtype="datetime64[ns]"),
        "source_of_truth": pd.Series(dtype=str),
    })


def load_ledger(path: Path) -> pd.DataFrame:
    if Path(path).exists():
        return pd.read_parquet(path)
    return empty_ledger()


def canonical_map(ledger: pd.DataFrame) -> dict[str, str]:
    """{variant -> canonical} for the rows already pinned."""
    if ledger.empty:
        return {}
    return dict(zip(ledger["variant_normalized"], ledger["canonical_normalized"]))


def choose_canonical(candidates: Sequence[AliasCandidate]) -> str:
    """The spelling that feeds the hash for this group: a board display name
    beats an aggregator name, tie -> first seen (input order) wins."""
    if not candidates:
        return ""
    best = candidates[0]
    for cand in candidates[1:]:
        if SOURCE_RANK.get(cand.source_of_truth, 0) > SOURCE_RANK.get(best.source_of_truth, 0):
            best = cand
    return best.variant_normalized


def append_aliases(
    ledger: pd.DataFrame,
    mappings: Iterable[tuple[str, str, str]],
    first_seen: pd.Timestamp,
) -> pd.DataFrame:
    """Fold (variant, canonical, source_of_truth) triples into the ledger.

    Append-only: a variant already present keeps the canonical it was first
    pinned to, whatever the caller now thinks is best. Blank variants and
    self-mappings that add nothing are skipped.
    """
    first_seen = pd.Timestamp(first_seen).normalize()
    known = set(ledger["variant_normalized"]) if len(ledger) else set()
    rows = []
    for variant, canonical, origin in mappings:
        if not variant or not canonical or variant in known:
            continue
        known.add(variant)
        rows.append({
            "variant_normalized": variant,
            "canonical_normalized": canonical,
            "first_seen": first_seen,
            "source_of_truth": origin,
        })
    if not rows:
        return ledger.copy()
    new = pd.DataFrame(rows)
    out = new if ledger.empty else pd.concat([ledger, new], ignore_index=True)
    return out.astype({
        "variant_normalized": str,
        "canonical_normalized": str,
        "source_of_truth": str,
    })


def write_ledger(ledger: pd.DataFrame, path: Path) -> None:
    write_parquet(ledger[list(ALIAS_COLUMNS)], Path(path))


# ---------------------------------------------------------------------
# Sticky ids
# ---------------------------------------------------------------------

def _tracked_rank(state: Mapping) -> tuple[int, str]:
    return (
        STATE_PRECEDENCE.get(state.get("state"), -1),
        str(state.get("last_touch") or ""),
    )


def select_sticky_id(
    job_ids: Sequence[str],
    state_index: Mapping[str, Mapping],
    seen_ids: Container[str] = (),
) -> str:
    """The job_id a merge group keeps. `job_ids` is the group in the existing
    survivor tie-break order, best first.

    1. A tracked member (has pipeline/<job_id>/state.yaml) beats an untracked
       one. 2. Several tracked -> state precedence, then most recent
       last_touch. 3. Else a member already in seen.parquet. 4. Else the
       caller's own survivor.
    """
    if not job_ids:
        return ""
    tracked = [j for j in job_ids if j in state_index]
    if len(tracked) == 1:
        return tracked[0]
    if tracked:
        # max keeps the first maximal element, so the pre-sort makes a full
        # tie resolve to the lowest job_id rather than to dict order.
        return max(sorted(tracked), key=lambda j: _tracked_rank(state_index[j]))
    for job_id in job_ids:
        if job_id in seen_ids:
            return job_id
    return job_ids[0]
