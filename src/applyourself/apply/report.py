"""What `apply plan` and `apply run` print and write: the plan as JSON and text,
the fill summary, the run report and its path, and the post-fill manifest."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from applyourself.apply import (
    run_record,
)
from applyourself.apply.plan import Plan
from applyourself.shared import clock, paths, schemas

if TYPE_CHECKING:
    from applyourself.apply.fill import FillResult
    from applyourself.apply_cli import RunOutcome


def _value(value: object) -> str:
    if isinstance(value, bool):
        return "checked" if value else "unchecked"
    if isinstance(value, tuple):
        return ", ".join(value)
    return str(value)


def as_dict(plan: Plan) -> dict[str, Any]:
    return {
        "schema_version": schemas.PLAN_SCHEMA_VERSION,
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
        "work_authorization": plan.work_authorization,
        "fields": [
            {
                "id": f.id,
                "name": f.name,
                "label": f.label,
                "kind": f.kind,
                "section": f.section,
                "required": f.required,
                "multi": f.multi,
                "tier": f.tier,
                "value": list(f.value) if isinstance(f.value, tuple) else f.value,
                "assert_selected": f.needs_selection_assert,
                "description": f.description,
                "options": list(f.options),
                "source": f.source,
                "reason": f.reason,
                "replaced": f.replaced,
            }
            for f in plan.fields
        ],
        "files": [
            {
                "id": f.id,
                "name": f.name,
                "label": f.label,
                "required": f.required,
                "path": str(f.path),
            }
            for f in plan.files
        ],
        "unmapped": [
            {
                "id": u.id,
                "label": u.label,
                "required": u.required,
                "kind": u.kind,
                "section": u.section,
                "tier": u.tier,
                "reason": u.reason,
                "options": list(u.options),
                "description": u.description,
                "multi": u.multi,
                "source": u.source,
                "similar": list(u.similar),
            }
            for u in plan.unmapped
        ],
        "draftable": [
            {
                "id": d.id,
                "label": d.label,
                "kind": d.kind,
                "section": d.section,
                "options": list(d.options),
                "description": d.description,
                "required": d.required,
                "tier": d.tier,
                "reason": d.reason,
                "multi": d.multi,
                "source": d.source,
                "similar": list(d.similar),
            }
            for d in plan.draftable
        ],
        "skipped": [
            {"id": s.id, "label": s.label, "tier": s.tier, "reason": s.reason} for s in plan.skipped
        ],
        "api_only": list(plan.api_only),
        "ats": plan.ats,
        "requires_captcha": plan.requires_captcha,
        "ignored_overrides": list(plan.ignored_overrides),
    }


def _ignored_override_lines(plan: Plan) -> list[str]:
    if not plan.ignored_overrides:
        return []
    return [
        "",
        f"OVERRIDES IGNORED ({len(plan.ignored_overrides)}) — supplied, "
        "validated, and not applied:",
        *(f"  - {note}" for note in plan.ignored_overrides),
    ]


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
    for fp in plan.files:
        flag = "*" if fp.required else " "
        lines.append(f"  {flag} {fp.id:<28} {fp.path.name}")

    lines.append("")
    lines.append(f"DRAFTABLE, optional and unmatched ({len(plan.draftable)})")
    for d in plan.draftable:
        lines.append(f"    {d.id:<28} {d.label}")
        if d.description:
            lines.append(f"      note: {d.description}")
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
        if u.description:
            lines.append(f"      note: {u.description}")
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
    lines.extend(_ignored_override_lines(plan))
    return "\n".join(lines)


def _offered(options: tuple[str, ...], limit: int = 6) -> str:
    if not options:
        return ""
    shown = ", ".join(options[:limit])
    return shown if len(options) <= limit else f"{shown}, +{len(options) - limit} more"


def render_questions(plan: Plan) -> str:
    """The form grouped by who answers it: known facts, keyword guesses to
    confirm, questions the bank has never seen, and what blocks the submit."""
    lines = [
        f"{plan.company or '(unknown company)'} — {plan.title or '(unknown title)'}",
        f"job_id {plan.job_id}   {plan.ats}/{plan.board}   {len(plan.fields)} filled, "
        f"{len(plan.files)} attached, {len(plan.unmapped)} unanswered",
        "",
    ]
    guesses = [f for f in plan.fields if f.source.startswith("bank:") and "~" in f.source]
    known = [f for f in plan.fields if f not in guesses]

    lines.append(f"KNOWN — answered from the bank or your facts ({len(known)})")
    for f in known:
        flag = "*" if f.required else " "
        via = f"  via {f.source}" if f.source else ""
        lines.append(f"  {flag} [{f.tier}] {f.label}")
        lines.append(f"      -> {_value(f.value)}{via}")
        if f.options:
            lines.append(f"      of {len(f.options)} offered: {_offered(f.options)}")

    lines.append("")
    lines.append(f"GUESSED BY KEYWORD — confirm or correct ({len(guesses)})")
    for f in guesses:
        flag = "*" if f.required else " "
        lines.append(f"  {flag} [{f.id}] {f.label}")
        lines.append(f"      -> {_value(f.value)}  via {f.source}")
        if f.options:
            lines.append(f"      of {len(f.options)} offered: {_offered(f.options, limit=10)}")

    new = [*plan.draftable, *(u for u in plan.unmapped if u.tier == "C")]
    lines.append("")
    lines.append(f"NEW — not in the bank yet ({len(new)})")
    for q in new:
        flag = "*" if q.required else " "
        lines.append(f"  {flag} [{q.id}] {q.label}")
        if q.options:
            lines.append(f"      choose from: {_offered(q.options, limit=10)}")
        if q.description:
            lines.append(f"      note: {q.description}")
        if q.similar:
            lines.append(f"      closest in the bank: {', '.join(q.similar)}")

    blockers = [u for u in plan.unmapped if u.required or u.blocks]
    lines.append("")
    lines.append(f"BLOCKING — required and unanswered ({len(blockers)})")
    for u in blockers:
        lines.append(f"    [{u.tier}] [{u.id}] {u.label}")
        lines.append(f"      {u.reason}")
        if u.options:
            lines.append(f"      offers: {_offered(u.options, limit=10)}")

    if plan.skipped:
        lines.append("")
        lines.append(f"LEFT BLANK, optional ({len(plan.skipped)})")
        for sk in plan.skipped:
            lines.append(f"    [{sk.tier}] {sk.label}: {sk.reason}")

    lines.append("")
    lines.append(
        "PARKED: nothing would be submitted."
        if plan.parked
        else "READY: every required field resolved. --submit would click."
    )
    lines.extend(_ignored_override_lines(plan))
    return "\n".join(lines)


def render_fill(result: FillResult, plan: Plan) -> str:
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
    drifted = [r for r in result.manifest if not r["matches"] and r["group"] == "filled"]
    if drifted:
        lines.append(f"\n{len(drifted)} FIELD(S) THE PAGE DISAGREES WITH:")
        for row in drifted:
            lines.append(
                f"  - {row['id']}: planned {row['planned']!r}, page holds {row['actual']!r}"
            )
    occupied = [
        r
        for r in result.manifest
        if r["group"] in ("parked", "left blank", "skipped") and r["actual"]
    ]
    if occupied:
        lines.append(f"\n{len(occupied)} UNANSWERED QUESTION(S) THE PAGE ALREADY HAS A VALUE FOR:")
        for row in occupied:
            lines.append(
                f"  - {row['id']} [{row['group']}]: {row['actual']!r} — {row['label'][:60]}"
            )
    if result.failures:
        lines.append(f"\n{len(result.failures)} FAILURE(S):")
        lines.extend(f"  - {f}" for f in result.failures)
    return "\n".join(lines)


def render_report(
    outcomes: list[RunOutcome], started_at: datetime, runs_dir: Path | None = None
) -> str:
    """The markdown report, rendered from the run's JSON record."""
    record = run_record.build(outcomes, started_at.isoformat(timespec="seconds"), None)
    trend = run_record.trend(runs_dir) if runs_dir is not None else []
    return run_record.render(record, trend)


def write_report(
    outcomes: list[RunOutcome],
    started_at: datetime,
    out_dir: Path | None = None,
    path: Path | None = None,
) -> Path:
    """Write the run's JSON record, then the markdown report rendered from it.
    Returns the report's path; the record is beside it as `<stem>.json`."""
    if path is not None:
        out_dir = path.parent
    else:
        out_dir = out_dir if out_dir is not None else paths.APPLY_RUNS
    out_dir.mkdir(parents=True, exist_ok=True)
    path = path or reserve_report_path(started_at, out_dir)
    record = run_record.build(
        outcomes,
        started_at.isoformat(timespec="seconds"),
        clock.now().isoformat(timespec="seconds"),
    )
    run_record.write(record, path.with_suffix(".json"))
    path.write_text(run_record.render(record, run_record.trend(out_dir)), encoding="utf-8")
    return path


def write_manifest(result: FillResult, plan: Plan, path: Path) -> Path:
    """The post-fill read-back, as JSON, at a path the caller named.

    Only ever written when `--manifest` asked for it, and only for a run that
    actually opened a browser — an unasked-for artifact next to the run report
    would make "no run was started, so no report exists" untrue.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "job_id": plan.job_id,
                "company": plan.company,
                "title": plan.title,
                "form_url": plan.form_url,
                "landed_url": result.landed_url,
                "page_title": result.title,
                "submitted": result.submitted,
                "failures": list(result.failures),
                "recovered": list(result.recovered),
                "fields": result.manifest,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def reserve_report_path(started_at: datetime, out_dir: Path | None = None) -> Path:
    """A report path no existing run owns.

    This file is the only record of what went out, so two runs starting in the
    same second must not share a name. Reserved once per run and re-written
    per role, so the crash-survival writes keep hitting the same file.
    """
    out_dir = out_dir if out_dir is not None else paths.APPLY_RUNS
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = started_at.strftime("%Y-%m-%dT%H-%M-%S")
    path = out_dir / f"{stem}.md"
    n = 2
    while path.exists() or path.with_suffix(".json").exists():
        path = out_dir / f"{stem}.{n}.md"
        n += 1
    return path
