"""Deterministic CLI for /track (R7: no LLM calls; R10: sole state writer).

  <job_id> <state> [--note "..."] [--if S] [--force]
                              move a role along state_io.ALLOWED,
                              creating pipeline/<job_id>/state.yaml from
                              clean.parquet (else the store) on first transition.
                              --if S: only when the role is currently in
                              state S; otherwise a no-op, exit 0.
                              --force --note "why": a move ALLOWED does not list
  outreach-draft <job_id> --channel C --to "N" --draft-file F
                 [--contact-id ID] [--medium M]
                              append a draft entry to outreach[]
  outreach-sent <job_id> --channel C --to "N"
                              flip the latest matching draft outreach[]
                              entry to sent
  states                      every state.yaml, with its shown_state (an old
                              `applied` shows `ghosted`), its fit_score and
                              sponsorship_label from score_store.view(), and
                              the active and closed state names, as JSON
                              (read-only)
  ensure <job_id>             create state.yaml at `saved` if absent; no-op if
                              present. Bootstrap only, never a transition --
                              `app new` calls it so a re-tailor cannot rewind
                              live state.
  stamp-schema --dry-run|--apply [--pipeline DIR]
                              add schema_version to every state.yaml that
                              lacks it and drop the dead cover_letters key;
                              --dry-run only counts. Never a transition.
  drop-score-copies --dry-run|--apply [--pipeline DIR]
                              remove the old fit_score / sponsorship_label
                              copies from every state.yaml (a fields_dropped
                              event each); scored.parquet holds the score.
                              --dry-run only counts. Never a transition.
  cleanup [--dry-run|--apply] [--pipeline DIR]
                              delete the folders of skip / withdrawn / rejected
                              / ghosted roles, archiving the resume and JD of those
                              that were applied (src/cleanup.py). Read-only
                              unless --apply. Never a transition.
  migrate-events --dry-run|--apply [--pipeline DIR] [--ledger FILE]
                              build pipeline/events.jsonl from every
                              state.yaml's history plus the submit ledger's
                              sent roles that have no state.yaml; --dry-run
                              checks the fold matches every state.yaml and
                              exits 1 on any mismatch. Run with nothing else
                              writing state.
  rebuild-states --dry-run|--apply [--pipeline DIR]
                              list (or rewrite) every state.yaml that differs
                              from the fold of pipeline/events.jsonl

Run as `uv run track <job_id> <state>`, `uv run track outreach-sent ...` or
`uv run track ensure <job_id>`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

from src.apply import cooldown, ledger
from src.shared import paths
from src.state_io import (
    ACTIVE_STATES,
    CLOSED_STATES,
    SCORE_COPIES,
    VALID_STATES,
    NotInState,
    append_outreach_draft,
    drop_score_copies,
    ensure_state,
    ghosted_after_days,
    load_all_states,
    load_state,
    logged_state,
    mark_cleaned,
    mark_outreach_sent,
    needs_stamp,
    shown_state,
    stamp_schema,
    state_path_for,
    transition,
)

NEXT_ACTION = {
    "saved": "ready for `/tailor {job_id}` once you've eyeballed the JD",
    "tailored": "`/apply {job_id}`, or submit it yourself and then `/track {job_id} applied`",
    "applied": "verify E-Verify; await response; consider `/outreach {job_id} recruiter` if you have a contact. Shows as ghosted after {ghosted_days} days with no update",
    "screen": "good luck; update with the outcome",
    "interview": "good luck; update with the outcome",
    "offer": "congrats. terminal -- no further /track transitions.",
    "rejected": "terminal. recorded.",
    "withdrawn": "terminal. recorded.",
    "skip": "suppressed from the shortlist as of now.",
}


def _gather_initial_fields(job_id: str) -> dict[str, Any]:
    if not paths.CLEAN.exists():
        raise SystemExit("ERROR: jobs/clean.parquet missing -- run discovery first")
    clean = pd.read_parquet(paths.CLEAN).set_index("job_id")
    if job_id in clean.index:
        row = clean.loc[job_id].to_dict()
    else:
        # A role retention dropped from the view is still in the store.
        store = (
            pd.read_parquet(paths.STORE)
            if paths.STORE.exists()
            else pd.DataFrame(columns=["job_id"])
        )
        hits = store[store["job_id"] == job_id]
        if hits.empty:
            raise SystemExit(
                f"ERROR: job_id {job_id} not in clean.parquet or the jobs store -- check the id"
            )
        row = hits.sort_values("last_seen").iloc[-1].to_dict()
        row["location"] = row.get("location_raw", "")
    initial: dict[str, Any] = {
        "job_id": job_id,
        "company": str(row.get("company", "")),
        "title": str(row.get("title", "")),
        "source": str(row.get("source", "")),
        "url": str(row.get("url", "")),
        "location": str(row.get("location", "")),
        "vertical": str(row.get("vertical", "")),
    }
    return initial


def _cmd_transition(args: argparse.Namespace) -> int:
    job_id, new_state, note = args.job_id, args.state, args.note
    p = state_path_for(paths.PIPELINE, job_id)
    # Gate on content, not existence: an empty state.yaml carries no state and
    # is rebuilt from the parquets rather than transitioned in place.
    try:
        existing = load_state(p) or logged_state(p)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    # --if never creates a role, so it needs no initial fields.
    initial = (
        None
        if existing is not None or args.if_state is not None
        else _gather_initial_fields(job_id)
    )
    try:
        data = transition(
            p,
            new_state,
            note=note,
            initial_fields=initial,
            if_state=args.if_state,
            force=args.force,
        )
    except NotInState as e:
        print(f"OK: {job_id} is {e.current or 'untracked'}, not {args.if_state} -- left unchanged")
        return 0
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(
        f"OK: {job_id}  state={new_state}  vertical={data.get('vertical') or 'unknown'}  "
        f"history_len={len(data['state_history'])}"
    )
    print(f"     file: {p}")
    if new_state == "applied":
        _warn_cooldown(job_id, data.get("company"))
    action = NEXT_ACTION.get(new_state)
    if action:
        print(f"     next: {action.format(job_id=job_id, ghosted_days=ghosted_after_days())}")
    return 0


def _warn_cooldown(job_id: str, company: object) -> None:
    """A hand-tracked `applied` inside the company cooldown is said, never blocked."""
    company = str(company or "")
    ledger_file = paths.APPLICATIONS / "apply_runs" / ledger.LEDGER_NAME
    day = cooldown.until(
        company, cooldown.last_applications(ledger_path=ledger_file, exclude=job_id)
    )
    if day is not None:
        print(
            f"WARNING: {company} had another application recently: {cooldown.note(day)}",
            file=sys.stderr,
        )


def _sent_before(job_id: str) -> dict[str, Any] | None:
    """The submit ledger's latest line for this role (`ledger.prior`)."""
    return ledger.prior(paths.APPLICATIONS / "apply_runs" / ledger.LEDGER_NAME, job_id)


def _cmd_ensure(args: argparse.Namespace) -> int:
    job_id = args.job_id
    p = state_path_for(paths.PIPELINE, job_id)
    try:
        existing = load_state(p) or logged_state(p)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    if existing is None:
        sent = _sent_before(job_id)
        if sent is not None:
            # The role's history is gone but the board may have it. Registering
            # it fresh at `saved` is how 68 roles were applied to twice.
            verdict = ledger.verdict_of(sent)
            if verdict is ledger.Verdict.UNKNOWN:
                advice = (
                    "The click may not have reached the board: check it, then run "
                    f"`uv run track {job_id} applied` if it did, or delete its "
                    "lines from the ledger if it did not"
                )
            elif verdict is ledger.Verdict.REJECTED:
                advice = (
                    f"The board turned it down. Restore it with: uv run track {job_id} applied "
                    f'--note "restored from the submit ledger", then uv run track {job_id} rejected'
                )
            else:
                advice = (
                    f"Restore it with: uv run track {job_id} applied --note "
                    '"restored from the submit ledger"'
                )
            print(
                f"ERROR: {job_id} has no state.yaml, but the submit ledger has a "
                f"`{sent.get('status', '')}` line from {sent.get('at', '?')}. {advice}",
                file=sys.stderr,
            )
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
        print(f"OK: {job_id} already registered  state={data.get('state')}  (unchanged)  file: {p}")
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
    print(
        f"OK: outreach[] entry flipped to sent  (job_id={job_id}, channel={channel}, to={to_name!r})"
    )
    return 0


def _cmd_outreach_draft(args: argparse.Namespace) -> int:
    p = state_path_for(paths.PIPELINE, args.job_id)
    if not p.exists():
        print(
            f"ERROR: pipeline/{args.job_id}/state.yaml missing -- run /track "
            f"{args.job_id} saved first",
            file=sys.stderr,
        )
        return 1
    try:
        data = append_outreach_draft(
            p,
            channel=args.channel,
            to_name=args.to,
            draft_file=args.draft_file,
            contact_id=args.contact_id,
            medium=args.medium,
        )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"OK: outreach[] now has {len(data['outreach'])} entry/entries  (job_id={args.job_id})")
    return 0


def _scores() -> dict[str, dict[str, Any]]:
    """{job_id: {sponsorship_label, fit_score}} from score_store.view()."""
    if not paths.SCORED.exists():
        return {}
    from src.score_store import view

    v = view(paths.SCORED)
    return {
        str(r["job_id"]): {
            "sponsorship_label": str(r["sponsorship_label"] or "unknown"),
            "fit_score": int(r["fit_score"]) if pd.notna(r["fit_score"]) else None,
        }
        for r in v[["job_id", *SCORE_COPIES]].to_dict("records")
    }


def _cmd_states() -> int:
    def jsonable(d: dict[str, Any]) -> dict[str, Any]:
        return {
            k: (v if isinstance(v, (str, int, float, bool, list, dict, type(None))) else str(v))
            for k, v in d.items()
        }

    days = ghosted_after_days()
    scores = _scores()
    out = {
        "states": [
            {
                **{k: v for k, v in jsonable(s).items() if k not in SCORE_COPIES},
                **scores.get(str(s.get("job_id")), dict.fromkeys(SCORE_COPIES)),
                "shown_state": shown_state(s, days=days),
            }
            for s in load_all_states(paths.PIPELINE)
        ],
        "ghosted_after_days": days,
        "active_states": sorted(ACTIVE_STATES),
        "closed_states": sorted(CLOSED_STATES),
    }
    print(json.dumps(out, indent=2, default=str))
    return 0


def _cmd_stamp_schema(args: argparse.Namespace) -> int:
    pipeline = args.pipeline or paths.PIPELINE
    files = sorted(pipeline.glob("*/state.yaml"))
    todo = errors = 0
    for p in files:
        try:
            data = load_state(p)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            errors += 1
            continue
        if not needs_stamp(data):
            continue
        if args.apply:
            todo += stamp_schema(p)
        else:
            todo += 1
    verb = "stamped" if args.apply else "would stamp"
    print(f"OK: {verb} {todo} of {len(files)} state.yaml files  ({errors} unreadable)")
    return 1 if errors else 0


def _cmd_drop_score_copies(args: argparse.Namespace) -> int:
    pipeline = args.pipeline or paths.PIPELINE
    files = sorted(pipeline.glob("*/state.yaml"))
    todo = errors = 0
    for p in files:
        try:
            data = load_state(p)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            errors += 1
            continue
        if data is None or not any(k in data for k in SCORE_COPIES):
            continue
        todo += 1
        if args.apply:
            drop_score_copies(p)
    verb = "dropped the score copies from" if args.apply else "would drop the score copies from"
    print(f"OK: {verb} {todo} of {len(files)} state.yaml files  ({errors} unreadable)")
    return 1 if errors else 0


def _cmd_cleanup(args: argparse.Namespace) -> int:
    from src import cleanup

    pipeline = args.pipeline or paths.PIPELINE
    actions = cleanup.plan(pipeline)

    def record(job_id: str, cleaned: dict[str, Any]) -> None:
        mark_cleaned(state_path_for(pipeline, job_id), cleaned)

    if args.apply:
        cleanup.apply(actions, record)
    print(f"OK: {cleanup.render(actions, args.apply)}")
    return 0


def _backfill_fields(
    job_ids: set[str], lines: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Initial fields for ledger-only roles: clean.parquet, else the store, else
    the ledger line itself. Each parquet is read once, in the needed columns."""
    import pyarrow.parquet as pq

    def rows(path: Path, cols: list[str]) -> dict[str, dict[str, Any]]:
        if not job_ids or not path.exists():
            return {}
        names = pq.read_schema(path).names  # type: ignore[no-untyped-call]
        cols = [c for c in cols if c in names]
        df = pd.read_parquet(path, columns=cols, filters=[("job_id", "in", sorted(job_ids))])
        if "last_seen" in df:
            df = df.sort_values("last_seen")
        return {str(r["job_id"]): {str(k): v for k, v in r.items()} for r in df.to_dict("records")}

    base = ["job_id", "company", "title", "source", "url", "vertical"]
    clean = rows(paths.CLEAN, [*base, "location"])
    store = rows(paths.STORE, [*base, "location_raw", "last_seen"])
    out = {}
    for job_id in job_ids:
        line = lines[job_id]
        row = clean.get(job_id) or store.get(job_id) or {}
        fields: dict[str, Any] = {
            "company": str(row.get("company") or line.get("company") or ""),
            "title": str(row.get("title") or line.get("title") or ""),
            "source": str(row.get("source") or ""),
            "url": str(row.get("url") or line.get("form_url") or ""),
            "location": str(row.get("location") or row.get("location_raw") or ""),
            "vertical": str(row.get("vertical") or ""),
        }
        out[job_id] = fields
    return out


def _cmd_migrate_events(args: argparse.Namespace) -> int:
    from src import state_io

    pipeline = args.pipeline or paths.PIPELINE
    ledger_file = args.ledger or paths.APPLICATIONS / "apply_runs" / ledger.LEDGER_NAME
    log_path = state_io.events_path_for(pipeline)
    if log_path.exists():
        print(f"ERROR: {log_path} already exists; nothing to migrate", file=sys.stderr)
        return 1
    records: dict[str, dict[str, Any]] = {}
    errors = 0
    for p in sorted(pipeline.glob("*/state.yaml")):
        try:
            data = load_state(p)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            errors += 1
            continue
        if data is None:
            continue
        if data.get("job_id") != p.parent.name:
            print(f"ERROR: {p} has job_id {data.get('job_id')!r}", file=sys.stderr)
            errors += 1
            continue
        records[p.parent.name] = data
    events = [e for rec in records.values() for e in state_io.events_from_record(rec)]

    latest: dict[str, dict[str, Any]] = {}
    for line in ledger.records(ledger_file):
        if line.get("job_id"):
            latest[str(line["job_id"])] = line
    verdicts = {j: ledger.verdict_of(r) for j, r in latest.items() if j not in records}
    restore = {
        j for j, v in verdicts.items() if v in (ledger.Verdict.SENT, ledger.Verdict.REJECTED)
    }
    unknown = sorted(j for j, v in verdicts.items() if v is ledger.Verdict.UNKNOWN)
    fields = _backfill_fields(restore, latest)
    for j in sorted(restore):
        events += state_io.events_from_ledger(
            j, latest[j], fields[j], rejected=verdicts[j] is ledger.Verdict.REJECTED
        )

    folded = state_io.fold(events)
    mismatches = 0
    for j, rec in records.items():
        want, got = state_io.as_cached(rec), folded.get(j) or {}
        if want != got:
            mismatches += 1
            keys = sorted(k for k in want.keys() | got.keys() if want.get(k) != got.get(k))
            print(f"MISMATCH: {j}  {', '.join(keys)}", file=sys.stderr)
    kinds: dict[str, int] = {}
    for ev in events:
        kinds[ev["kind"]] = kinds.get(ev["kind"], 0) + 1
    print(
        f"{len(records)} state.yaml files, {len(restore)} sent roles with no state.yaml in the ledger "
        f"({sum(1 for j in restore if verdicts[j] is ledger.Verdict.REJECTED)} rejected), "
        f"{len(events)} events ({', '.join(f'{n} {k}' for k, n in sorted(kinds.items()))})"
    )
    if unknown:
        print(f"left out, the click may not have reached the board: {', '.join(unknown)}")
    if mismatches or errors:
        print(
            f"ERROR: {mismatches} mismatches, {errors} unreadable; nothing written", file=sys.stderr
        )
        return 1
    if not args.apply:
        print(f"OK: the fold matches all {len(records)} state.yaml files; nothing written")
        return 0
    state_io.write_events(log_path, events)
    rebuilt = state_io.rebuild_states(pipeline, apply=True)
    print(f"OK: wrote {log_path}; rewrote {len(rebuilt)} state.yaml files from it")
    return 0


def _cmd_rebuild_states(args: argparse.Namespace) -> int:
    from src import state_io

    pipeline = args.pipeline or paths.PIPELINE
    if not state_io.events_path_for(pipeline).exists():
        print("ERROR: no pipeline/events.jsonl yet -- run `track migrate-events`", file=sys.stderr)
        return 1
    differ = state_io.rebuild_states(pipeline, apply=args.apply)
    for j in differ:
        print(f"{'rebuilt' if args.apply else 'differs'}: {j}")
    print(
        f"OK: {len(differ)} state.yaml files {'rebuilt' if args.apply else 'differ from the log'}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if argv == ["states"]:
        return _cmd_states()

    if argv and argv[0] == "ensure":
        parser = argparse.ArgumentParser(prog="track ensure")
        parser.add_argument("job_id")
        args = parser.parse_args(argv[1:])
        return _cmd_ensure(args)

    if argv and argv[0] in ("stamp-schema", "drop-score-copies"):
        parser = argparse.ArgumentParser(prog=f"track {argv[0]}")
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--dry-run", action="store_true")
        mode.add_argument("--apply", action="store_true")
        parser.add_argument("--pipeline", type=Path, default=None)
        args = parser.parse_args(argv[1:])
        if argv[0] == "drop-score-copies":
            return _cmd_drop_score_copies(args)
        return _cmd_stamp_schema(args)

    if argv and argv[0] == "cleanup":
        parser = argparse.ArgumentParser(prog="track cleanup")
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true")
        mode.add_argument("--apply", action="store_true")
        parser.add_argument("--pipeline", type=Path, default=None)
        args = parser.parse_args(argv[1:])
        return _cmd_cleanup(args)

    if argv and argv[0] in ("migrate-events", "rebuild-states"):
        parser = argparse.ArgumentParser(prog=f"track {argv[0]}")
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--dry-run", action="store_true")
        mode.add_argument("--apply", action="store_true")
        parser.add_argument("--pipeline", type=Path, default=None)
        if argv[0] == "migrate-events":
            parser.add_argument("--ledger", type=Path, default=None)
            return _cmd_migrate_events(parser.parse_args(argv[1:]))
        return _cmd_rebuild_states(parser.parse_args(argv[1:]))

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

    parser = argparse.ArgumentParser(
        prog="track", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("job_id")
    parser.add_argument("state", choices=sorted(VALID_STATES))
    parser.add_argument("--note", default=None)
    parser.add_argument("--if", dest="if_state", choices=sorted(VALID_STATES), default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    return _cmd_transition(args)


if __name__ == "__main__":
    sys.exit(main())
