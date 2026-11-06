"""`discover-ids`: build jobs/job_ids.parquet and the store's dedupe_key column
from what holds ids today: jobs.parquet, pipeline/events.jsonl, scored.parquet.

`--dry-run` checks every id maps back to itself and prints the counts; it
exits 1 on any conflict. `--apply` writes the table and the store. No id is
ever changed.

Deterministic and LLM-free (R7). Reads pipeline/events.jsonl, never writes it
(R10).
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from src.discovery import dedupe
from src.discovery import store as S
from src.shared import identity, paths
from src.shared.parquet_io import locked
from src.state_io import events_path_for, read_events


def _event_rows(pipeline_dir: Path, held: set[str]) -> pd.DataFrame:
    """A pair for each tracked id the store does not hold, from its `created` event."""
    rows = {}
    for ev in read_events(events_path_for(pipeline_dir)):
        jid, data = ev.get("job_id"), ev.get("data") or {}
        if ev.get("kind") != "created" or not jid or jid in held or jid in rows:
            continue
        c = identity.normalize_company(data.get("company"))
        t = identity.normalize_title(data.get("title"))
        if c and t:
            rows[jid] = {"job_id": jid, "company_normalized": c, "title_normalized": t}
    return S.coerce_job_ids(pd.DataFrame(list(rows.values()), columns=S.JOB_IDS_COLUMNS))


def _ids_in(pipeline_dir: Path, scored: Path) -> set[str]:
    ids = {ev.get("job_id") for ev in read_events(events_path_for(pipeline_dir))}
    if scored.exists():
        ids |= set(pd.read_parquet(scored, columns=["job_id"])["job_id"])
    return {i for i in ids if isinstance(i, str) and i}


def build(store: pd.DataFrame, known: pd.DataFrame, pipeline_dir: Path, jobs_dir: Path) -> dict:
    """The new table, the conflicts and the counts. Pure: writes nothing."""
    store = store.copy()
    before = store["dedupe_key"].copy()
    store["dedupe_key"] = [
        identity.dedupe_key(c, t)
        for c, t in zip(store["company_normalized"], store["title_normalized"])
    ]
    from_store = S.job_id_rows(store)
    from_events = _event_rows(pipeline_dir, set(store["job_id"]))
    table = S.merge_job_ids(S.merge_job_ids(known, from_store), from_events)
    table["dedupe_key"] = [
        identity.dedupe_key(c, t)
        for c, t in zip(table["company_normalized"], table["title_normalized"])
    ]

    # The lookup flush will use, built from the table alone.
    index = dedupe._Index(S.empty_store(), frozenset(), known=table)
    conflicts = []
    for rec in pd.concat([from_store, from_events, known]).to_dict("records"):
        pair = f"{rec['company_normalized']}|{rec['title_normalized']}"
        if rec["job_id"] not in index.by_pair.get(pair, []):
            conflicts.append(f"lost: {rec['job_id']} {pair}")
    blank = table[(table["company_normalized"] == "") | (table["title_normalized"] == "")]
    conflicts += [f"blank pair: {j}" for j in blank["job_id"]]
    unmapped = sorted(_ids_in(pipeline_dir, jobs_dir / paths.SCORED.name) - set(table["job_id"]))

    pair_ids = table.groupby(["company_normalized", "title_normalized"])["job_id"].nunique()
    id_pairs = table.groupby("job_id").size()
    own_hash = {
        j
        for j, c, t in zip(table["job_id"], table["company_normalized"], table["title_normalized"])
        if j == identity.compute_job_id(c, t)
    }
    salted = {
        j
        for j, c, t in zip(table["job_id"], table["company_normalized"], table["title_normalized"])
        if j not in own_hash and j in dedupe._hash_ids(c, t)
    }
    flagged = set()
    for rec in table.to_dict("records"):
        for other in dedupe.dedupe_matches(index, rec):
            flagged.add("|".join(sorted((rec["job_id"], other))))
    recorded = {r["pair"] for r in S.load_maybe_duplicates(jobs_dir / paths.MAYBE_DUPES.name)}
    counts = {
        "store_rows": len(store),
        "store_ids": int(store["job_id"].nunique()),
        "table_rows": len(table),
        "table_ids": int(table["job_id"].nunique()),
        "table_rows_before": len(known),
        "from_store": len(from_store),
        "from_events": len(from_events),
        "own_hash_ids": len(own_hash),
        "salted_ids": len(salted),
        "inherited_ids": int(table["job_id"].nunique()) - len(own_hash | salted),
        "pairs_with_2plus_ids": int((pair_ids > 1).sum()),
        "ids_with_2plus_pairs": int((id_pairs > 1).sum()),
        "dedupe_keys_changed": int((before != store["dedupe_key"]).sum()),
        "dedupe_maybe_pairs": len(flagged),
        "dedupe_maybe_pairs_new": len(flagged - recorded),
        "unmapped_ids": len(unmapped),
    }
    return {
        "table": table,
        "store": store,
        "conflicts": conflicts,
        "unmapped": unmapped,
        "counts": counts,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="discover-ids",
        description="Build jobs/job_ids.parquet and the store's dedupe_key column. "
        "Never changes an id.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="check and print; write nothing")
    mode.add_argument("--apply", action="store_true", help="write the table and the store")
    parser.add_argument("--jobs-dir", type=Path, default=paths.JOBS)
    parser.add_argument("--pipeline-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    store_path = args.jobs_dir / paths.STORE.name
    ids_path = args.jobs_dir / paths.JOB_IDS.name
    pipeline_dir = args.pipeline_dir or paths.PIPELINE

    # A dry run takes no lock, so it writes nothing at all.
    with locked(store_path) if args.apply else contextlib.nullcontext():
        out = build(S.load_store(store_path), S.load_job_ids(ids_path), pipeline_dir, args.jobs_dir)
        stored = "dedupe_key" in pq.read_schema(store_path).names if store_path.exists() else False
        print(f"{'dedupe_key_stored':<24} {stored}")
        for k, v in out["counts"].items():
            print(f"{k:<24} {v}")
        if out["unmapped"]:
            print(f"unmapped (no company/title anywhere): {' '.join(out['unmapped'][:20])}")
        for c in out["conflicts"][:50]:
            print(f"CONFLICT {c}", file=sys.stderr)
        if out["conflicts"]:
            print(f"{len(out['conflicts'])} conflicts; nothing written", file=sys.stderr)
            return 1
        if args.apply:
            S.save_store(out["store"], store_path)
            with locked(ids_path):
                S.save_job_ids(out["table"], ids_path)
            print(f"wrote {ids_path} and dedupe_key in {store_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
