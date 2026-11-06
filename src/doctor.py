"""`uv run doctor`: list where the role folders, state.yaml files, the event
log, the submit ledger and the jobs store disagree. Read-only: it writes
nothing and fixes nothing. Exit 0 when they agree, 1 when it lists anything.

  folder_no_state   an applications/<lane>/<dir>_<id> folder whose role has no state.yaml
  dir_missing       tailored_dirs[-1] names a folder that is not on disk (and not cleaned)
  cache_drift       state.yaml differs from the fold of pipeline/events.jsonl
  sent_not_applied  the ledger says the board has it; state.yaml is still before applied
  ledger_no_state   a ledger role with no state.yaml
  not_in_store      a state.yaml role the jobs store has never seen
"""

from __future__ import annotations

import argparse
import re
import sys

import pandas as pd

from src import state_io
from src.apply import ledger
from src.shared import paths

_FOLDER = re.compile(r"_([0-9a-f]{8})(?:_v\d+)?$")
_BEFORE_APPLIED = frozenset({"saved", "tailored", "skip"})


def _states() -> dict[str, dict]:
    """{job_id: state.yaml} read straight from disk (state_io.iter_states may write a cache)."""
    out = {}
    for p in sorted(paths.PIPELINE.glob("*/state.yaml")):
        try:
            data = state_io.load_state(p)
        except ValueError:
            continue
        if data:
            out[p.parent.name] = data
    return out


def problems() -> list[tuple[str, str, str]]:
    """(kind, job_id, detail), sorted."""
    states = _states()
    out: list[tuple[str, str, str]] = []
    for folder in sorted(paths.APPLICATIONS.glob("*/*")):
        m = _FOLDER.search(folder.name)
        if folder.is_dir() and m and m[1] not in states and not folder.parent.name.startswith("_"):
            rel = folder.relative_to(paths.APPLICATIONS)
            out.append(("folder_no_state", m[1], str(rel)))
    for job_id, data in states.items():
        dirs = data.get("tailored_dirs") or []
        if dirs and not data.get("cleaned") and not (paths.APPLICATIONS / dirs[-1]).is_dir():
            out.append(("dir_missing", job_id, str(dirs[-1])))
    folded = state_io.fold(state_io.read_events(state_io.events_path_for(paths.PIPELINE)))
    for job_id, data in folded.items():
        if states.get(job_id) != data:
            out.append(("cache_drift", job_id, "uv run track rebuild-states --dry-run"))
    sent = ledger.sent_job_ids(paths.APPLY_RUNS / ledger.LEDGER_NAME)
    for job_id in sorted(sent):
        if job_id not in states:
            out.append(("ledger_no_state", job_id, ""))
        elif states[job_id].get("state") in _BEFORE_APPLIED:
            out.append(("sent_not_applied", job_id, f"state {states[job_id]['state']}"))
    if paths.STORE.is_file():
        seen = set(pd.read_parquet(paths.STORE, columns=["job_id"])["job_id"])
        out += [("not_in_store", j, "") for j in states if j not in seen]
    return sorted(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="doctor", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.parse_args(argv)
    found = problems()
    for kind, job_id, detail in found:
        print(f"{kind} {job_id}" + (f" {detail}" if detail else ""))
    print(f"{len(found)} mismatch(es)" if found else "no mismatches")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
