"""Deterministic CLI for /track (R7: no LLM calls; R10: sole state writer).

  transition <job_id> <state> [--note "..."]      move a role through the
                                                   11-state machine, creating
                                                   pipeline/<job_id>/state.yaml
                                                   from clean/scored parquet
                                                   on first transition
  outreach-sent <job_id> --channel C --to "N"     flip the latest matching
                                                   draft outreach[] entry to
                                                   sent
  ensure <job_id>                                 create state.yaml at `saved`
                                                   if absent; no-op if present.
                                                   Bootstrap only, never a
                                                   transition -- this is what
                                                   /tailor calls so a re-tailor
                                                   cannot rewind live state.

Run directly: `uv run python -m src.track_cli <job_id> <state> [--note "..."]`
or `uv run python -m src.track_cli outreach-sent <job_id> --channel C --to "N"`
or `uv run python -m src.track_cli ensure <job_id>`.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from src.state_io import (
    VALID_STATES,
    ensure_state,
    mark_outreach_sent,
    state_path_for,
    transition,
)

PIPELINE = Path("pipeline")
CLEAN = Path("jobs/clean.parquet")
SCORED = Path("jobs/scored.parquet")

NEXT_ACTION = {
    "saved": "ready for `/tailor {job_id}` once you've eyeballed the JD",
    "tailored": "review applications/<vertical>/<dir>/; submit manually; then `/track {job_id} applied`",
    "applied": "verify E-Verify; await response; consider `/outreach {job_id} recruiter` if you have a contact",
    "screen": "good luck; update with the outcome",
    "interview": "good luck; update with the outcome",
    "offer": "congrats. terminal -- no further /track transitions.",
    "rejected": "terminal. recorded.",
    "withdrawn": "terminal. recorded.",
    "ghosted": "terminal. recorded.",
    "skip": "suppressed from the shortlist as of now.",
    "recruiter_contact": "log notes in state.yaml.notes if useful.",
}


def _gather_initial_fields(job_id: str) -> dict:
    if not CLEAN.exists():
        raise SystemExit("ERROR: jobs/clean.parquet missing -- run discovery first")
    clean = pd.read_parquet(CLEAN).set_index("job_id")
    if job_id not in clean.index:
        raise SystemExit(f"ERROR: job_id {job_id} not in clean.parquet -- check the id")
    row = clean.loc[job_id].to_dict()
    initial = {
        "job_id": job_id,
        "company": str(row.get("company", "")),
        "title": str(row.get("title", "")),
        "source": str(row.get("source", "")),
        "url": str(row.get("url", "")),
        "location": str(row.get("location", "")),
        "vertical": str(row.get("vertical", "")),
    }
    if SCORED.exists():
        scored = pd.read_parquet(SCORED).set_index("job_id")
        if job_id in scored.index:
            srow = scored.loc[job_id]
            initial["sponsorship_label"] = str(srow.get("sponsorship_label", "unknown"))
            fs = srow.get("fit_score")
            initial["fit_score"] = int(fs) if pd.notna(fs) else None
    return initial


def _cmd_transition(args: argparse.Namespace) -> int:
    job_id, new_state, note = args.job_id, args.state, args.note
    p = state_path_for(PIPELINE, job_id)
    initial = None if p.exists() else _gather_initial_fields(job_id)
    try:
        data = transition(p, new_state, note=note, initial_fields=initial)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"OK: {job_id}  state={new_state}  vertical={data.get('vertical') or 'unknown'}  "
          f"history_len={len(data['state_history'])}")
    print(f"     file: {p}")
    action = NEXT_ACTION.get(new_state)
    if action:
        print(f"     next: {action.format(job_id=job_id)}")
    return 0


def _cmd_ensure(args: argparse.Namespace) -> int:
    job_id = args.job_id
    p = state_path_for(PIPELINE, job_id)
    initial = None if p.exists() else _gather_initial_fields(job_id)
    try:
        data, created = ensure_state(p, initial_fields=initial or {})
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    if created:
        print(f"OK: registered {job_id} at state=saved  file: {p}")
    else:
        print(f"OK: {job_id} already registered  state={data.get('state')}  "
              f"(unchanged)  file: {p}")
    return 0


def _cmd_outreach_sent(args: argparse.Namespace) -> int:
    job_id, channel, to_name = args.job_id, args.channel, args.to
    p = state_path_for(PIPELINE, job_id)
    if not p.exists():
        print(f"ERROR: pipeline/{job_id}/state.yaml missing", file=sys.stderr)
        return 1
    try:
        mark_outreach_sent(p, channel=channel, to_name=to_name)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"OK: outreach[] entry flipped to sent  (job_id={job_id}, channel={channel}, to={to_name!r})")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if argv and argv[0] == "ensure":
        parser = argparse.ArgumentParser(prog="python -m src.track_cli ensure")
        parser.add_argument("job_id")
        args = parser.parse_args(argv[1:])
        return _cmd_ensure(args)

    if argv and argv[0] == "outreach-sent":
        parser = argparse.ArgumentParser(prog="python -m src.track_cli outreach-sent")
        parser.add_argument("job_id")
        parser.add_argument("--channel", required=True, choices=["recruiter", "referral", "alumni"])
        parser.add_argument("--to", required=True)
        args = parser.parse_args(argv[1:])
        return _cmd_outreach_sent(args)

    parser = argparse.ArgumentParser(prog="python -m src.track_cli", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("job_id")
    parser.add_argument("state", choices=sorted(VALID_STATES))
    parser.add_argument("--note", default=None)
    args = parser.parse_args(argv)
    return _cmd_transition(args)


if __name__ == "__main__":
    sys.exit(main())
