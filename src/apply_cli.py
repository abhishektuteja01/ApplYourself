"""Deterministic CLI for /apply (R7: no LLM calls; R10: never writes state).

  plan <job_id> [--json] [--url URL] [--out-dir DIR]
                                    fetch the board's rendered form and its
                                    question schema, resolve every field against
                                    profile/application_answers.yaml, and print
                                    the plan. No browser, no submission, nothing
                                    written anywhere.

`run` — the queue, the rate limiter and the run report — lands with fill.py.
Until then `plan` is the whole surface, and it is read-only by construction.

Run directly: `uv run python -m src.apply_cli plan <job_id>` or `uv run apply
plan <job_id>`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from src import paths, state_io
from src.apply.answers import Answers, AnswersError, load_answers
from src.apply.greenhouse import ApplyUrlError, PostingExpired, load_board, parse_posting
from src.apply.plan import Plan, PlanError, plan_for_board
from src.apply.reconcile import ReconcileError
from src.apply.schema import SchemaError
from src.apply.domscan import DomScanError
from src.discovery.sources.ats.http import CareersError

CLEAN = paths.CLEAN
PIPELINE = paths.PIPELINE
APPLICATIONS = paths.APPLICATIONS


class ApplyCliError(Exception):
    """A per-role failure with a message meant for the user."""


def resolve_url(job_id: str, state: dict | None) -> str:
    """The posting URL to apply through.

    clean.parquet and state.yaml disagree often enough to matter — dedupe now
    prefers an applyable url, so clean is usually the better one, but a role can
    also drop out of the 14-day window entirely, and one live role has an
    Avature url in state and a LinkedIn one in clean. So: take whichever of the
    two is a posting this module can parse, clean first, and report both when
    neither is.
    """
    candidates: list[tuple[str, str]] = []
    if CLEAN.exists():
        clean = pd.read_parquet(CLEAN, columns=["job_id", "url"])
        row = clean.loc[clean["job_id"] == job_id]
        if not row.empty:
            candidates.append(("clean.parquet", str(row.iloc[0]["url"] or "")))
    if state:
        candidates.append(("state.yaml", str(state.get("url") or "")))

    if not candidates:
        raise ApplyCliError(
            f"{job_id} is in neither clean.parquet nor pipeline/{job_id}/state.yaml"
        )
    for _, url in candidates:
        try:
            parse_posting(url)
        except ApplyUrlError:
            continue
        return url

    seen = "; ".join(f"{where}: {url or '(empty)'}" for where, url in candidates)
    raise ApplyCliError(
        f"{job_id} has no Greenhouse posting URL to apply through ({seen}). "
        "Phase 1 submits to Greenhouse only — apply to this one by hand."
    )


def resolve_out_dir(job_id: str, state: dict | None) -> Path:
    """The role's most recent /tailor output dir, which holds both artifacts."""
    dirs = list((state or {}).get("tailored_dirs") or [])
    if not dirs:
        raise ApplyCliError(
            f"{job_id} has no tailored_dirs[] — run /tailor before /apply"
        )
    out_dir = APPLICATIONS / dirs[-1]
    if not out_dir.is_dir():
        raise ApplyCliError(
            f"{job_id}: tailored_dirs[] names {dirs[-1]}, which is not a "
            f"directory under {APPLICATIONS.name}/"
        )
    return out_dir


def build(job_id: str, url: str | None = None,
          out_dir: Path | None = None) -> tuple[Plan, "Answers"]:
    """Everything `plan` does, minus the printing.

    Returns the answer config alongside the plan: `fill` needs it to re-resolve
    a parked select once the browser can read its real options.
    """
    state = state_io.load_state(state_io.state_path_for(PIPELINE, job_id))
    posting_url = url or resolve_url(job_id, state)
    target = Path(out_dir) if out_dir else resolve_out_dir(job_id, state)
    answers = load_answers()
    board = load_board(posting_url)
    return plan_for_board(board, answers, target, job_id=job_id), answers


# ------------------------------------------------------------------ rendering


def _value(value) -> str:
    if isinstance(value, bool):
        return "checked" if value else "unchecked"
    if isinstance(value, tuple):
        return ", ".join(value)
    return str(value)


def as_dict(plan: Plan) -> dict:
    return {
        "job_id": plan.job_id,
        "board": plan.board,
        "token": plan.token,
        "form_url": plan.form_url,
        "company": plan.company,
        "title": plan.title,
        "out_dir": str(plan.out_dir),
        "submit_selector": plan.submit_selector,
        "submit_disabled": plan.submit_disabled,
        "parked": plan.parked,
        "submittable": plan.submittable,
        "fields": [
            {"id": f.id, "label": f.label, "kind": f.kind, "section": f.section,
             "required": f.required, "multi": f.multi, "tier": f.tier,
             "value": list(f.value) if isinstance(f.value, tuple) else f.value,
             "assert_selected": f.needs_selection_assert}
            for f in plan.fields
        ],
        "files": [
            {"id": f.id, "label": f.label, "required": f.required, "path": str(f.path)}
            for f in plan.files
        ],
        "unmapped": [
            {"id": u.id, "label": u.label, "required": u.required, "kind": u.kind,
             "section": u.section, "tier": u.tier, "reason": u.reason,
             "options": list(u.options)}
            for u in plan.unmapped
        ],
        "draftable": [
            {"id": d.id, "label": d.label, "kind": d.kind, "section": d.section,
             "options": list(d.options)}
            for d in plan.draftable
        ],
        "skipped": [
            {"id": s.id, "label": s.label, "tier": s.tier, "reason": s.reason}
            for s in plan.skipped
        ],
        "api_only": list(plan.api_only),
    }


def render(plan: Plan) -> str:
    lines = [
        f"{plan.company or '(unknown company)'} — {plan.title or '(unknown title)'}",
        f"job_id {plan.job_id}   board {plan.board}   token {plan.token}",
        f"form    {plan.form_url}",
        f"tailor  {plan.out_dir}",
        "",
        f"FILL ({len(plan.fields)})",
    ]
    for f in plan.fields:
        flag = "*" if f.required else " "
        assertion = "  [assert selection]" if f.needs_selection_assert else ""
        lines.append(f"  {flag} {f.id:<28} [{f.tier}] {_value(f.value)}{assertion}")
        lines.append(f"      {f.label}")

    lines.append("")
    lines.append(f"ATTACH ({len(plan.files)})")
    for f in plan.files:
        flag = "*" if f.required else " "
        lines.append(f"  {flag} {f.id:<28} {f.path.name}")

    lines.append("")
    lines.append(f"DRAFTABLE, optional and unmatched ({len(plan.draftable)})")
    for d in plan.draftable:
        lines.append(f"    {d.id:<28} {d.label}")
        if d.options:
            lines.append(f"      offers: {', '.join(d.options)}")

    lines.append("")
    lines.append(f"SKIPPED, optional ({len(plan.skipped)})")
    for s in plan.skipped:
        lines.append(f"    {s.id:<28} [{s.tier}] {s.reason}")

    lines.append("")
    lines.append(f"UNMAPPED ({len(plan.unmapped)})")
    for u in plan.unmapped:
        flag = "*" if u.required else " "
        lines.append(f"  {flag} {u.id:<28} [{u.tier}] {u.reason}")
        lines.append(f"      {u.label}")
        if u.options:
            lines.append(f"      offers: {', '.join(u.options)}")

    lines.append("")
    if plan.parked:
        required = len(plan.required_parked)
        lines.append(
            f"PARKED: {len(plan.unmapped)} unanswered question(s), {required} required. "
            "Nothing would be submitted."
        )
    else:
        lines.append("READY: every rendered field resolved. --submit would click.")
    if plan.api_only:
        lines.append(f"(declared by the API, rendered nowhere: {', '.join(plan.api_only)})")
    return "\n".join(lines)


# ------------------------------------------------------------------ commands


def _cmd_plan(args: argparse.Namespace) -> int:
    try:
        plan, _ = build(args.job_id, url=args.url, out_dir=args.out_dir)
    except PostingExpired as exc:
        print(f"EXPIRED: {exc}", file=sys.stderr)
        return 2
    except (ApplyCliError, ApplyUrlError, AnswersError, PlanError, CareersError,
            SchemaError, DomScanError, ReconcileError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(as_dict(plan), indent=2) if args.json else render(plan))
    return 0


def _cmd_fill(args: argparse.Namespace) -> int:
    """Fill a real form and stop. No submit path exists yet."""
    try:
        plan, answers = build(args.job_id, url=args.url, out_dir=args.out_dir)
    except PostingExpired as exc:
        print(f"EXPIRED: {exc}", file=sys.stderr)
        return 2
    except (ApplyCliError, ApplyUrlError, AnswersError, PlanError, CareersError,
            SchemaError, DomScanError, ReconcileError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if plan.parked and not args.force:
        print(render(plan))
        print("\nRefusing to open a browser for a parked role. --force to fill "
              "what does resolve and see the rest in the form.", file=sys.stderr)
        return 1

    from src.apply.fill import fill

    def review(result) -> None:
        """Report while the window is still open, then hold it if someone is
        watching. Not a tty (a background run) means nothing to hold for."""
        print(render_fill(result, plan))
        if not args.no_pause and not args.headless and sys.stdin.isatty():
            input("\nreview the form in the browser, then press Enter to close: ")

    result = fill(plan, answers, headless=args.headless, after=review)
    return 0 if result.ok else 1


def render_fill(result, plan: Plan) -> str:
    done = sum(1 for o in result.outcomes if o.action != "failed")
    lines = [f"\nFILLED {done} / {len(result.outcomes)} — {plan.form_url}"]
    for outcome in result.outcomes:
        mark = "ok  " if outcome.action != "failed" else "FAIL"
        was = f"   (was {outcome.before!r})" if outcome.was_prefilled else ""
        lines.append(f"  {mark} {outcome.id:<26} {outcome.after or outcome.note}{was}")
    if result.observed_options:
        lines.append("\nOptions read off the opened widgets:")
        for field_id, options in result.observed_options.items():
            lines.append(f"  {field_id}: {list(options)}")
    if result.recovered:
        lines.append(
            f"\nRecovered at fill time (parked, then resolved once the widget's "
            f"real options could be read): {list(result.recovered)}"
        )
    if result.prefilled:
        lines.append(f"\nPrefilled before we wrote (upload parse?): {list(result.prefilled)}")
    if result.failures:
        lines.append(f"\n{len(result.failures)} FAILURE(S):")
        lines.extend(f"  - {f}" for f in result.failures)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        prog="apply", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="print the fill plan for one role")
    p.add_argument("job_id")
    p.add_argument("--json", action="store_true")
    p.add_argument("--url", default=None,
                   help="posting URL override, when the stored one is stale")
    p.add_argument("--out-dir", default=None, type=Path,
                   help="/tailor output dir override")
    p.set_defaults(func=_cmd_plan)

    f = sub.add_parser("fill", help="fill one real form and stop; never submits")
    f.add_argument("job_id")
    f.add_argument("--url", default=None)
    f.add_argument("--out-dir", default=None, type=Path)
    f.add_argument("--force", action="store_true",
                   help="fill even though the role parks")
    f.add_argument("--headless", action="store_true")
    f.add_argument("--no-pause", action="store_true",
                   help="close the browser without waiting for review")
    f.set_defaults(func=_cmd_fill)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
