"""Deterministic CLI for /track (R7: no LLM calls; R10: sole state writer).

  <job_id> <state> [--note "..."] [--if S]
                              move a role through the 11-state machine,
                              creating pipeline/<job_id>/state.yaml from
                              clean/scored parquet on first transition.
                              --if S: only when the role is currently in
                              state S; otherwise a no-op, exit 0
  outreach-draft <job_id> --channel C --to "N" --draft-file F
                 [--contact-id ID] [--medium M]
                              append a draft entry to outreach[]
  outreach-sent <job_id> --channel C --to "N"
                              flip the latest matching draft outreach[]
                              entry to sent
  ensure <job_id>             create state.yaml at `saved` if absent; no-op if
                              present. Bootstrap only, never a transition --
                              `app new` calls it so a re-tailor cannot rewind
                              live state.

Run as `uv run track <job_id> <state>`, `uv run track outreach-sent ...` or
`uv run track ensure <job_id>`.
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

from src.apply import ledger
from src.shared import paths
from src.state_io import (
    VALID_STATES,
    append_outreach_draft,
    ensure_state,
    load_state,
    mark_outreach_sent,
    state_path_for,
    transition,
)

CLEAN = paths.CLEAN
STORE = paths.STORE
SCORED = paths.SCORED

NEXT_ACTION = {
    "saved": "ready for `/tailor {job_id}` once you've eyeballed the JD",
    "tailored": "`/apply {job_id}`, or submit it yourself and then `/track {job_id} applied`",
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
    if job_id in clean.index:
        row = clean.loc[job_id].to_dict()
    else:
        # A role retention dropped from the view is still in the store.
        store = pd.read_parquet(STORE) if STORE.exists() else pd.DataFrame(columns=["job_id"])
        hits = store[store["job_id"] == job_id]
        if hits.empty:
            raise SystemExit(f"ERROR: job_id {job_id} not in clean.parquet or the jobs store -- check the id")
        row = hits.sort_values("last_seen").iloc[-1].to_dict()
        row["location"] = row.get("location_raw", "")
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
    p = state_path_for(paths.PIPELINE, job_id)
    # Gate on content, not existence: an empty state.yaml carries no state and
    # is rebuilt from the parquets rather than transitioned in place.
    try:
        existing = load_state(p)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    if args.if_state is not None:
        current = (existing or {}).get("state")
        if current != args.if_state:
            print(f"OK: {job_id} is {current or 'untracked'}, not {args.if_state} "
                  f"-- left unchanged")
            return 0
    initial = None if existing is not None else _gather_initial_fields(job_id)
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


def _sent_before(job_id: str) -> dict | None:
    """The submit ledger's latest line for this role, unless the board turned
    the application down."""
    v, rec = ledger.verdict(paths.APPLICATIONS / "apply_runs" / ledger.LEDGER_NAME, job_id)
    return None if v in (ledger.Verdict.NOT_SENT, ledger.Verdict.REJECTED) else rec


def _cmd_ensure(args: argparse.Namespace) -> int:
    job_id = args.job_id
    p = state_path_for(paths.PIPELINE, job_id)
    try:
        existing = load_state(p)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    if existing is None:
        sent = _sent_before(job_id)
        if sent is not None:
            # The role's history is gone but the board may have it. Registering
            # it fresh at `saved` is how 68 roles were applied to twice.
            if ledger.verdict_of(sent) is ledger.Verdict.UNKNOWN:
                advice = ("The click may not have reached the board: check it, then run "
                          f"`uv run track {job_id} applied` if it did, or delete its "
                          "lines from the ledger if it did not")
            else:
                advice = (f"Restore it with: uv run track {job_id} applied --note "
                          "\"restored from the submit ledger\"")
            print(f"ERROR: {job_id} has no state.yaml, but the submit ledger has a "
                  f"`{sent.get('status', '')}` line from {sent.get('at', '?')}. {advice}",
                  file=sys.stderr)
            return 1
    initial = None if existing is not None else _gather_initial_fields(job_id)
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
    p = state_path_for(paths.PIPELINE, job_id)
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


def _cmd_outreach_draft(args: argparse.Namespace) -> int:
    p = state_path_for(paths.PIPELINE, args.job_id)
    if not p.exists():
        print(f"ERROR: pipeline/{args.job_id}/state.yaml missing -- run /track "
              f"{args.job_id} saved first", file=sys.stderr)
        return 1
    try:
        data = append_outreach_draft(p, channel=args.channel, to_name=args.to,
                                     draft_file=args.draft_file,
                                     contact_id=args.contact_id, medium=args.medium)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"OK: outreach[] now has {len(data['outreach'])} entry/entries  (job_id={args.job_id})")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if argv and argv[0] == "ensure":
        parser = argparse.ArgumentParser(prog="track ensure")
        parser.add_argument("job_id")
        args = parser.parse_args(argv[1:])
        return _cmd_ensure(args)

    if argv and argv[0] == "outreach-sent":
        parser = argparse.ArgumentParser(prog="track outreach-sent")
        parser.add_argument("job_id")
        parser.add_argument("--channel", required=True, choices=["recruiter", "referral", "alumni"])
        parser.add_argument("--to", required=True)
        args = parser.parse_args(argv[1:])
        return _cmd_outreach_sent(args)

    if argv and argv[0] == "outreach-draft":
        parser = argparse.ArgumentParser(prog="track outreach-draft")
        parser.add_argument("job_id")
        parser.add_argument("--channel", required=True, choices=["recruiter", "referral", "alumni"])
        parser.add_argument("--to", required=True)
        parser.add_argument("--draft-file", required=True)
        parser.add_argument("--contact-id", default=None)
        parser.add_argument("--medium", default=None)
        args = parser.parse_args(argv[1:])
        return _cmd_outreach_draft(args)

    parser = argparse.ArgumentParser(prog="track", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("job_id")
    parser.add_argument("state", choices=sorted(VALID_STATES))
    parser.add_argument("--note", default=None)
    parser.add_argument("--if", dest="if_state", choices=sorted(VALID_STATES), default=None)
    args = parser.parse_args(argv)
    return _cmd_transition(args)


if __name__ == "__main__":
    sys.exit(main())
