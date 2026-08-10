"""Deterministic CLI for /apply (R7: no LLM calls; R10: never writes state).

  plan <job_id> [--json] [--url URL] [--out-dir DIR]
                                    fetch the board's rendered form and its
                                    question schema, resolve every field against
                                    profile/application_answers.yaml, and print
                                    the plan. No browser, no submission, nothing
                                    written anywhere.
  fill <job_id> [--force] [--headless] [--no-pause]
                                    fill one real form and stop; never submits.
  run [--limit N] [--rate 4m] [--jitter 60s] [--submit] [--job-id X]
                                    walk the eligible queue (state == tailored,
                                    tailored_dirs[] and cover_letters[] both
                                    non-empty). Default is fill-and-stop;
                                    --submit is required to click. Writes
                                    applications/apply_runs/<timestamp>.md.

Run directly: `uv run python -m src.apply_cli plan <job_id>` or `uv run apply
plan <job_id>`.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from pathlib import Path

import pandas as pd

from src import paths, state_io, track_cli
from src.apply import ashby, lever
from src.apply.ashby import AshbyClientRendered
from src.apply.answers import Answers, AnswersError, load_answers
from src.apply.fill import (SubmitGuardError, blocking_questions, fill, has_driver,
                             run_one)
from src.apply.greenhouse import ApplyUrlError, PostingExpired, load_board, parse_posting
from src.apply.plan import Plan, PlanError, plan_for_board
from src.apply.reconcile import ReconcileError
from src.apply.schema import SchemaError
from src.apply.domscan import DomScanError
from src.discovery.sources.ats.http import CareersError

# One posting URL parser per ATS this module can submit to. `detect_ats` tries
# each in turn — cheap, since they're pure regex matches, no network.
# Both live in `src.apply.detect` so `shortlist.py` can ask "can /apply submit
# to this?" without importing this CLI. Re-exported here because callers and
# tests already reach for `apply_cli.detect_ats`.
from src.apply.detect import _ATS_PARSERS, detect_ats, is_auto_submittable  # noqa: E402

CLEAN = paths.CLEAN
PIPELINE = paths.PIPELINE
APPLICATIONS = paths.APPLICATIONS
APPLY_RUNS = APPLICATIONS / "apply_runs"


class ApplyCliError(Exception):
    """A per-role failure with a message meant for the user."""


class ManualApplyOnly(ApplyCliError):
    """The role is real and reachable, but no submit path exists for its board
    — Workday, LinkedIn, a company careers page, or Ashby (scanned, no driver).

    Distinct from a failure on purpose. These roles have to be applied to by
    hand, and §13 requires the run report to say so in its own category rather
    than burying them in `failed`, where they read as breakage and drive the
    exit code."""


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
        if detect_ats(url) is not None:
            return url

    seen = "; ".join(f"{where}: {url or '(empty)'}" for where, url in candidates)
    raise ManualApplyOnly(
        f"{job_id} has no Greenhouse, Lever or Ashby posting URL to apply through "
        f"({seen}). Not one of the boards /apply submits to — apply by hand."
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


def load_overrides(path: Path, job_id: str | None = None,
                    ) -> dict[str, tuple[str | tuple[str, ...], str]]:
    """`/apply`'s per-run, per-field answers for Tier C questions (§15) —
    `{"job_id": "...", "<field_id>": {"value": "...", "tier": "C1"}}`, `value`
    a string or a list for a multi-select. Parsing only; no judgment lives
    here (R7) — the command file decided every value before this ever runs.

    The `job_id` key binds the file to one role. Tier C2 answers are
    company-specific by construction ("why do you want to work at X"), so a
    file drafted for one role and passed to another sends the wrong company's
    prose under the user's name — silently, since nothing about the value
    looks wrong. It stays optional so a hand-written override file still
    works, but when present it is enforced.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApplyCliError(f"{path}: not readable JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise ApplyCliError(f"{path}: top level must be a JSON object")

    declared = raw.pop("job_id", None)
    if declared is not None and job_id is not None and declared != job_id:
        raise ApplyCliError(
            f"{path} was drafted for job_id {declared!r} but this run is "
            f"{job_id!r}. Company-specific answers do not transfer between "
            f"roles — re-run /apply for {job_id}."
        )

    overrides: dict[str, tuple[str | tuple[str, ...], str]] = {}
    for field_id, entry in raw.items():
        if not isinstance(entry, dict):
            raise ApplyCliError(f"{path}: {field_id!r} must be an object")
        tier = entry.get("tier")
        if tier not in ("C1", "C2"):
            raise ApplyCliError(f"{path}: {field_id!r} has tier {tier!r}, want C1 or C2")
        value = entry.get("value")
        if isinstance(value, list):
            value = tuple(value)
        elif not isinstance(value, str):
            raise ApplyCliError(f"{path}: {field_id!r} value must be a string or a list")
        overrides[field_id] = (value, tier)
    return overrides


def build(job_id: str, url: str | None = None, out_dir: Path | None = None,
          answers_path: Path | None = None) -> tuple[Plan, "Answers"]:
    """Everything `plan` does, minus the printing.

    Returns the answer config alongside the plan: `fill` needs it to re-resolve
    a parked select once the browser can read its real options.
    """
    state = state_io.load_state(state_io.state_path_for(PIPELINE, job_id))
    posting_url = url or resolve_url(job_id, state)
    target = Path(out_dir) if out_dir else resolve_out_dir(job_id, state)
    answers = load_answers()
    overrides = load_overrides(Path(answers_path), job_id) if answers_path else None

    ats = detect_ats(posting_url)
    if ats is None:
        raise ApplyCliError(f"{posting_url}: not a Greenhouse, Lever or Ashby posting URL")
    if ats == "lever":
        board = lever.load_board(posting_url)
        return plan_for_board(board, answers, target, job_id=job_id, overrides=overrides,
                              ats="lever", requires_captcha=board.requires_captcha), answers
    if ats == "ashby":
        # Scan-only board: build() and `apply plan` work fully; fill()/run()
        # refuse loudly, since fill.py has no Ashby driver yet (§12a).
        board = ashby.load_board(posting_url)
        return plan_for_board(board, answers, target, job_id=job_id, overrides=overrides,
                              ats="ashby"), answers
    board = load_board(posting_url)
    return plan_for_board(board, answers, target, job_id=job_id, overrides=overrides), answers


# Every exception `build()` can raise that names a per-role problem rather
# than a bug in this CLI. `PostingExpired` is handled separately — it is an
# ordinary outcome (§14: 6/45 live postings), not a failure.
BUILD_ERRORS = (ApplyCliError, ApplyUrlError, AnswersError, PlanError, CareersError,
                 SchemaError, DomScanError, ReconcileError)


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
        plan, _ = build(args.job_id, url=args.url, out_dir=args.out_dir,
                        answers_path=args.answers)
    except PostingExpired as exc:
        print(f"EXPIRED: {exc}", file=sys.stderr)
        return 2
    except BUILD_ERRORS as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(as_dict(plan), indent=2) if args.json else render(plan))
    return 0


def _cmd_fill(args: argparse.Namespace) -> int:
    """Fill a real form and stop. No submit path exists yet."""
    try:
        plan, answers = build(args.job_id, url=args.url, out_dir=args.out_dir,
                              answers_path=args.answers)
    except PostingExpired as exc:
        print(f"EXPIRED: {exc}", file=sys.stderr)
        return 2
    except BUILD_ERRORS as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if plan.parked and not args.force:
        print(render(plan))
        print("\nRefusing to open a browser for a parked role. --force to fill "
              "what does resolve and see the rest in the form.", file=sys.stderr)
        return 1

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


# ------------------------------------------------------------------ queue


def eligible_queue(pipeline_dir: Path | None = None) -> list[str]:
    """`state == tailored` with a resume and a cover letter both on file.

    §8's later, reasoned statement — not just `tailored_dirs[]` — because
    §7b puts `company_answers.md` inside the cover-letter output dir, and
    `/apply`'s Tier C2 questions resolve from that file or park. Sorted by
    job_id: the only ordering signal every eligible role is guaranteed to
    carry, so the queue is reproducible run to run.

    Defaults to the module-level `PIPELINE`, looked up at call time — a
    default *parameter value* would freeze the original `paths.PIPELINE`
    object and miss a test's `monkeypatch.setattr(apply_cli, "PIPELINE", ...)`.
    """
    idx = state_io.load_state_index(pipeline_dir if pipeline_dir is not None else PIPELINE)
    return sorted(
        job_id for job_id, st in idx.items()
        if st.get("state") == "tailored"
        and st.get("tailored_dirs")
        and st.get("cover_letters")
    )


_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(s|m|h)?$")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, None: 1}


def parse_duration(spec: str) -> float:
    """'4m' / '60s' / '90' -> seconds. Bare digits are seconds."""
    match = _DURATION.match(spec.strip())
    if not match:
        raise ApplyCliError(f"not a duration: {spec!r} (want e.g. '4m', '60s', '90')")
    value, unit = match.groups()
    return float(value) * _DURATION_UNITS[unit]


@dataclass(frozen=True)
class RunOutcome:
    job_id: str
    company: str = ""
    title: str = ""
    category: str = "failed"
    """submitted | submitted_unconfirmed | submitted_untracked | manual |
    parked | ready | failed | expired"""

    detail: str = ""
    unmapped: tuple[str, ...] = dc_field(default_factory=tuple)


def _track_applied(job_id: str, plan) -> tuple[int, str]:
    """Write the `applied` transition through /track (R10). Returns
    `(returncode, note)` — never raises, because by the time this runs the
    application is already on the board and losing that fact is worse than
    any state-write error."""
    try:
        rc = track_cli.main([job_id, "applied", "--note",
                             f"auto-submitted via /apply ({plan.board})"])
    except Exception as exc:  # noqa: BLE001
        return 1, f" ({type(exc).__name__}: {exc})"
    return int(rc or 0), ""


def _run_role(job_id: str, *, submit: bool, headless: bool,
              answers_path: Path | None = None) -> RunOutcome:
    """One role, start to finish. Never raises — every failure mode this CLI
    knows about comes back as a category on the outcome, so one bad role
    cannot stop the queue.

    `answers_path` only ever applies to a single-role run (`--job-id`, §10's
    per-run Tier C overrides from `/apply`) — the queue path never passes one,
    since an override answers exactly one role's questions.

    Calls `build`/`run_one`/`track_cli.main` as plain module-level names (not
    default-parameter values) so a test can monkeypatch `apply_cli.build`,
    `apply_cli.run_one` or `apply_cli.track_cli` the same way the rest of this
    module already stubs `load_board`/`load_answers` — a default bound at def
    time would freeze the original object and monkeypatching would silently
    miss it.
    """
    try:
        plan, answers = build(job_id, answers_path=answers_path)
    except PostingExpired as exc:
        return RunOutcome(job_id, category="expired", detail=str(exc))
    except (ManualApplyOnly, AshbyClientRendered) as exc:
        # AshbyClientRendered is a manual-apply fact, not breakage: the form
        # only exists after JS runs, so there is no plan to build.
        return RunOutcome(job_id, category="manual", detail=str(exc))
    except BUILD_ERRORS as exc:
        return RunOutcome(job_id, category="failed", detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - see the docstring's "never raises"
        return RunOutcome(job_id, category="failed",
                           detail=f"unexpected {type(exc).__name__}: {exc}")

    if submit and headless and plan.requires_captcha:
        # Lever renders hCaptcha on every form and `submit()` blocks for a
        # human to solve it. Headless there means the wait can only ever time
        # out — 10 minutes per role, after the click has already landed.
        return RunOutcome(
            job_id, plan.company, plan.title, "failed",
            detail=f"{plan.ats} renders a captcha and --headless leaves nobody to "
                   f"solve it; re-run this role without --headless",
        )

    if not has_driver(plan.ats):
        # Ashby: the form scans and plans, but no fill driver exists. Reported
        # rather than attempted, so the browser never opens for a board that
        # cannot be filled.
        return RunOutcome(job_id, plan.company, plan.title, "manual",
                           detail=f"{plan.ats}: form scanned and planned, but no "
                                  f"browser driver exists yet — apply by hand")

    # `sink` holds the FillResult from before the browser opens, so a crash
    # anywhere after the submit click — including in browser teardown — still
    # tells us an application went out. Reading only the return value lost
    # that, and a lost `submitted` means the role stays `tailored` and the
    # next run applies to the same board again.
    sink: list = []
    try:
        result = run_one(plan, answers, headless=headless, submit_after=submit,
                          sink=sink)
    except BaseException as exc:  # noqa: BLE001 - Ctrl-C must not lose a submit
        landed = sink[0] if sink else None
        if landed is not None and landed.submitted:
            _track_applied(job_id, plan)
            return RunOutcome(
                job_id, plan.company, plan.title, "submitted_unconfirmed",
                detail=f"SUBMITTED, then {type(exc).__name__}: {exc} — the click "
                       f"landed; verify by hand",
            )
        if isinstance(exc, Exception):
            # A playwright timeout or driver error must not escape:
            # `run_queue` would abort mid-walk and every role already
            # submitted would lose its only record.
            return RunOutcome(job_id, plan.company, plan.title, "failed",
                               detail=f"{type(exc).__name__}: {exc}")
        raise

    if result.submitted:
        rc, exc_note = _track_applied(job_id, plan)
        if rc:
            # The application is on the board but the state write refused or
            # blew up. Loudest category there is: the role still reads
            # `tailored`, so the next run would apply to it a second time.
            return RunOutcome(
                job_id, plan.company, plan.title, "submitted_untracked",
                detail=f"SUBMITTED but track_cli exited {rc}{exc_note} — state.yaml "
                       f"still says tailored; fix by hand before the next run",
            )
        if result.confirmed:
            return RunOutcome(job_id, plan.company, plan.title, "submitted")
        return RunOutcome(
            job_id, plan.company, plan.title, "submitted_unconfirmed",
            detail="clicked and transitioned to applied, but the board showed no "
                   "confirmation this code recognises — verify by hand",
        )

    blocking = blocking_questions(plan, result)
    if blocking:
        return RunOutcome(job_id, plan.company, plan.title, "parked",
                           detail="required question(s) unresolved", unmapped=blocking)
    if result.failures:
        return RunOutcome(job_id, plan.company, plan.title, "failed",
                           detail="; ".join(result.failures))
    if result.submit_error:
        # Not a park (nothing is unmapped) and not a fill failure — the guard
        # refused for a form-shape reason instead (no submit button, or it
        # stayed disabled). Distinct enough from either to say so.
        return RunOutcome(job_id, plan.company, plan.title, "failed",
                           detail=result.submit_error)

    return RunOutcome(job_id, plan.company, plan.title, "ready",
                       detail="every field resolved; rerun with --submit to click")


def run_queue(job_ids: list[str], *, submit: bool, headless: bool = False,
              rate: float = 240.0, jitter: float = 60.0,
              sleeper=time.sleep, jitter_fn=random.uniform,
              answers_path: Path | None = None,
              on_outcome=None,
              collect_into: list | None = None) -> list[RunOutcome]:
    """Walk the queue, sleeping `rate + U(0, jitter)` seconds between roles.

    `sleeper`/`jitter_fn` are injectable so tests never actually sleep or
    depend on randomness. `answers_path` (see `_run_role`) is only meaningful
    when `job_ids` names a single role.

    `on_outcome` is called with the accumulated list after every role, and
    `collect_into` lets the caller own that list. Both exist for one reason:
    the run report is the only record of what went out, so it has to survive a
    crash mid-walk. A caller that owns the list still has every completed
    outcome even if this function never returns.
    """
    outcomes = [] if collect_into is None else collect_into
    for i, job_id in enumerate(job_ids):
        if i > 0:
            sleeper(rate + jitter_fn(0, jitter))
        outcomes.append(_run_role(job_id, submit=submit, headless=headless,
                                   answers_path=answers_path))
        if on_outcome is not None:
            on_outcome(outcomes)
    return outcomes


# ------------------------------------------------------------------ report


_CATEGORY_ORDER = ("submitted", "submitted_unconfirmed", "submitted_untracked",
                    "parked", "ready", "manual", "failed", "expired")
_CATEGORY_TITLE = {
    "submitted": "Submitted",
    "submitted_unconfirmed": "Submitted (no confirmation seen — verify by hand)",
    "submitted_untracked": "SUBMITTED BUT NOT TRACKED — fix state.yaml by hand",
    "parked": "Parked",
    "ready": "Ready (not submitted)",
    "manual": "Manual-apply (no submit path for this board)",
    "failed": "Failed",
    "expired": "Expired postings",
}

# Categories that make `apply run` exit non-zero. `manual` is deliberately not
# one: a board with no submit path is a fact about the board, not a failure.
_EXIT_NONZERO = ("failed", "submitted_untracked")


def render_report(outcomes: list[RunOutcome], started_at: datetime) -> str:
    lines = [
        f"# apply run — {started_at.isoformat(timespec='seconds')}",
        "",
        f"{len(outcomes)} role(s) attempted.",
        "",
    ]
    for category in _CATEGORY_ORDER:
        rows = [o for o in outcomes if o.category == category]
        if not rows:
            continue
        lines.append(f"## {_CATEGORY_TITLE[category]} ({len(rows)})")
        lines.append("")
        for o in rows:
            who = f"{o.company} — {o.title}".strip(" —") or o.job_id
            lines.append(f"- `{o.job_id}` {who}")
            if o.detail:
                lines.append(f"  - {o.detail}")
            if o.unmapped:
                lines.append(f"  - unmapped: {', '.join(o.unmapped)}")
        lines.append("")
    return "\n".join(lines)


def write_report(outcomes: list[RunOutcome], started_at: datetime,
                  out_dir: Path | None = None, path: Path | None = None) -> Path:
    if path is not None:
        out_dir = path.parent
    else:
        out_dir = out_dir if out_dir is not None else APPLY_RUNS
    out_dir.mkdir(parents=True, exist_ok=True)
    path = path or reserve_report_path(started_at, out_dir)
    path.write_text(render_report(outcomes, started_at), encoding="utf-8")
    return path


def reserve_report_path(started_at: datetime, out_dir: Path | None = None) -> Path:
    """A report path no existing run owns.

    Two runs starting in the same second used to land on the same filename and
    the second silently overwrote the first — and this file is the only record
    of what went out. Reserved once per run and then re-written per role, so
    the crash-survival writes keep hitting the same file rather than racing
    each other into new ones.
    """
    out_dir = out_dir if out_dir is not None else APPLY_RUNS
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = started_at.strftime("%Y-%m-%dT%H-%M-%S")
    path = out_dir / f"{stem}.md"
    n = 2
    while path.exists():
        path = out_dir / f"{stem}.{n}.md"
        n += 1
    return path


def _cmd_run(args: argparse.Namespace) -> int:
    if args.answers and not args.job_id:
        print("ERROR: --answers only applies to a single role — pass --job-id",
              file=sys.stderr)
        return 1

    if args.job_id:
        queue = eligible_queue()
        if args.job_id not in queue:
            print(
                f"ERROR: {args.job_id} is not eligible — needs state == tailored "
                "with both tailored_dirs[] and cover_letters[] non-empty",
                file=sys.stderr,
            )
            return 1
        queue = [args.job_id]
    else:
        queue = eligible_queue()
        if args.limit is not None:
            queue = queue[:args.limit]

    if not queue:
        print("Nothing eligible: no role at state=tailored with both "
              "tailored_dirs[] and cover_letters[] non-empty.")
        return 0

    try:
        rate = parse_duration(args.rate)
        jitter = parse_duration(args.jitter)
    except ApplyCliError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    started = datetime.now()
    # The caller owns the list and the report is rewritten after every role, so
    # a crash — or a Ctrl-C — mid-walk still leaves a complete record of every
    # application that already went out.
    outcomes: list[RunOutcome] = []
    path = reserve_report_path(started)
    write_report(outcomes, started, path=path)
    try:
        run_queue(queue, submit=args.submit, headless=args.headless,
                   rate=rate, jitter=jitter, answers_path=args.answers,
                   collect_into=outcomes,
                   on_outcome=lambda done: write_report(done, started, path=path))
    finally:
        write_report(outcomes, started, path=path)

    print(render_report(outcomes, started))
    print(f"Report written to {path}")
    return 1 if any(o.category in _EXIT_NONZERO for o in outcomes) else 0


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
    p.add_argument("--answers", default=None, type=Path,
                   help="/apply's per-run Tier C overrides JSON (§15)")
    p.set_defaults(func=_cmd_plan)

    f = sub.add_parser("fill", help="fill one real form and stop; never submits")
    f.add_argument("job_id")
    f.add_argument("--url", default=None)
    f.add_argument("--out-dir", default=None, type=Path)
    f.add_argument("--answers", default=None, type=Path,
                   help="/apply's per-run Tier C overrides JSON (§15)")
    f.add_argument("--force", action="store_true",
                   help="fill even though the role parks")
    f.add_argument("--headless", action="store_true")
    f.add_argument("--no-pause", action="store_true",
                   help="close the browser without waiting for review")
    f.set_defaults(func=_cmd_fill)

    r = sub.add_parser("run", help="walk the eligible queue")
    r.add_argument("--limit", type=int, default=None)
    r.add_argument("--rate", default="4m", help="delay between roles, e.g. '4m'")
    r.add_argument("--jitter", default="60s", help="added on top of --rate, uniformly")
    r.add_argument("--submit", action="store_true",
                   help="click submit on every role that resolves; default is fill-and-stop")
    r.add_argument("--job-id", default=None, help="run one specific role instead of the queue")
    r.add_argument("--answers", default=None, type=Path,
                   help="/apply's per-run Tier C overrides JSON (§15); requires --job-id")
    r.add_argument("--headless", action="store_true")
    r.set_defaults(func=_cmd_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
