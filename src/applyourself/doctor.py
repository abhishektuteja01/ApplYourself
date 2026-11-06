"""`uv run doctor`: list where the role folders, state.yaml files, the event
log, the submit ledger and the jobs store disagree. Read-only: it writes
nothing and fixes nothing. It lists what needs you and counts the rest
(--all lists those too). Exit 1 when anything needs you.

  folder_no_state   an applications/<lane>/<dir>_<id> folder whose role has no state.yaml
  dir_missing       tailored_dirs[-1] names a folder that is not on disk (and not cleaned)
  cache_drift       state.yaml differs from the fold of pipeline/events.jsonl
  sent_not_applied  the ledger says the board has it; state.yaml is still before applied
  ledger_no_state   a ledger role with no state.yaml
  not_in_store      a state.yaml role the jobs store has never seen

Only a count by default: folder_no_state, not_in_store, and dir_missing for a
role past tailored (its folder is no longer needed).
"""

from __future__ import annotations

import argparse
import re
import sys

import pandas as pd

from applyourself import state_io
from applyourself.apply import ledger
from applyourself.shared import paths
from applyourself.shared.logs import setup_logging

_FOLDER = re.compile(r"_([0-9a-f]{8})(?:_v\d+)?$")
_BEFORE_APPLIED = frozenset({"saved", "tailored", "skip"})
_ACTIVE = frozenset({"saved", "tailored"})


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


def problems() -> list[tuple[str, str, str, bool]]:
    """(kind, job_id, detail, needs_you), sorted."""
    states = _states()
    out: list[tuple[str, str, str, bool]] = []
    for folder in sorted(paths.APPLICATIONS.glob("*/*")):
        m = _FOLDER.search(folder.name)
        if folder.is_dir() and m and m[1] not in states and not folder.parent.name.startswith("_"):
            rel = folder.relative_to(paths.APPLICATIONS)
            out.append(("folder_no_state", m[1], str(rel), False))
    for job_id, data in states.items():
        dirs = data.get("tailored_dirs") or []
        if dirs and not data.get("cleaned") and not (paths.APPLICATIONS / dirs[-1]).is_dir():
            out.append(("dir_missing", job_id, str(dirs[-1]), data.get("state") in _ACTIVE))
    folded = state_io.fold(state_io.read_events(state_io.events_path_for(paths.PIPELINE)))
    for job_id, data in folded.items():
        if states.get(job_id) != data:
            out.append(("cache_drift", job_id, "uv run track rebuild-states --dry-run", True))
    sent = ledger.sent_job_ids(paths.APPLY_RUNS / ledger.LEDGER_NAME)
    for job_id in sorted(sent):
        if job_id not in states:
            out.append(("ledger_no_state", job_id, "", True))
        elif states[job_id].get("state") in _BEFORE_APPLIED:
            out.append(("sent_not_applied", job_id, f"state {states[job_id]['state']}", True))
    if paths.STORE.is_file():
        seen = set(pd.read_parquet(paths.STORE, columns=["job_id"])["job_id"])
        out += [("not_in_store", j, "", False) for j in states if j not in seen]
    return sorted(out)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(
        prog="doctor", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--all", action="store_true", help="also list what does not need you")
    args = parser.parse_args(argv)
    found = problems()
    needs = [p for p in found if p[3]]
    for kind, job_id, detail, _ in found if args.all else needs:
        print(f"{kind} {job_id}" + (f" {detail}" if detail else ""))
    print(f"{len(needs)} need(s) you" if needs else "nothing needs you")
    if len(found) > len(needs):
        print(f"{len(found) - len(needs)} more that do not (--all lists them)")
    return 1 if needs else 0


if __name__ == "__main__":
    sys.exit(main())
