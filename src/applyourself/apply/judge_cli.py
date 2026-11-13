"""`apply judge-pack` and `apply judge-check`: one role's packet for the
apply-judge, and the check that merges its answer into answers_override.json.
Never judges (R7)."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from applyourself.apply.bank_schema import Answers, AnswersError, load_answers
from applyourself.apply.judge_packet import (
    JudgeEntry,
    build_packet,
    check_entries,
    fields_for_judge,
    to_override,
    with_refusals,
)
from applyourself.apply.role_facts import company_answers_for


class JudgeCliError(Exception):
    pass


def _role(job_id: str) -> tuple[Path, dict[str, Any]]:
    from applyourself.apply_cli import ApplyCliError, resolve_out_dir

    try:
        out_dir = resolve_out_dir(job_id)
    except ApplyCliError as exc:
        raise JudgeCliError(str(exc)) from exc
    plan_file = out_dir / "plan.json"
    try:
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JudgeCliError(
            f"{plan_file}: not readable JSON ({exc}); run apply plan first"
        ) from exc
    return out_dir, plan


def _job_country(plan: dict[str, Any]) -> str | None:
    return (plan.get("work_authorization") or {}).get("job_country") or None


def _answers(out_dir: Path) -> Answers:
    """The bank with this role's company answers, as the plan reads it."""
    return replace(load_answers(), company_answers=company_answers_for(out_dir))


def _posting(plan: dict[str, Any]) -> dict[str, str]:
    return {k: str(plan.get(k) or "") for k in ("company", "title", "location")}


def merge(
    overrides: dict[str, Any], entries: list[JudgeEntry], labels: dict[str, str]
) -> dict[str, Any]:
    """`overrides` with each judged field replaced by its C4 entry. An entry
    without `decision` is the session's or yours: it always stands."""
    out = dict(overrides)
    for e in entries:
        old = out.get(e.field_id)
        if isinstance(old, dict) and "decision" not in old:
            print(f"kept your entry for {e.field_id}")
            continue
        out[e.field_id] = to_override(e, labels.get(e.field_id, ""))
    return out


def _cmd_pack(args: argparse.Namespace) -> int:
    out_dir, plan = _role(args.job_id)
    fields = fields_for_judge(plan)
    judge = out_dir / "judge"
    judge.mkdir(exist_ok=True)
    (judge / "out.json").unlink(missing_ok=True)  # never a leftover answer
    packet = judge / "packet.md"
    packet.write_text(
        build_packet(
            fields, _answers(out_dir), _job_country(plan), role_dir=out_dir, posting=_posting(plan)
        ),
        encoding="utf-8",
    )
    (judge / "fields.json").write_text(json.dumps(fields, indent=1) + "\n", encoding="utf-8")
    print(f"judge {packet} -> {judge / 'out.json'}")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    out_dir, plan = _role(args.job_id)
    fields = fields_for_judge(plan)
    judge = out_dir / "judge"
    out_file = judge / "out.json"
    try:
        raw = json.loads(out_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        problems = [f"{out_file}: not readable JSON ({exc})"]
        raw = None
    else:
        problems = check_entries(raw, fields, _answers(out_dir), str(plan.get("company") or ""))
    packet = judge / "packet.md"
    if packet.is_file():
        # The retry reads the same packet: it carries this refusal's problems.
        old = packet.read_text(encoding="utf-8")
        new = with_refusals(old, problems)
        if new != old:
            packet.write_text(new, encoding="utf-8")
    if problems:
        if raw is not None:
            (judge / "rejected_out.json").write_text(
                json.dumps(raw, indent=1) + "\n", encoding="utf-8"
            )
        for p in problems:
            print(p)
        return 1

    overrides_file = out_dir / "answers_override.json"
    overrides: dict[str, Any] = {"job_id": args.job_id}
    if overrides_file.exists():
        try:
            overrides = json.loads(overrides_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise JudgeCliError(f"{overrides_file}: not readable JSON ({exc})") from exc
        if not isinstance(overrides, dict):
            raise JudgeCliError(f"{overrides_file}: top level must be a JSON object")
        if overrides.get("job_id", args.job_id) != args.job_id:
            raise JudgeCliError(f"{overrides_file} belongs to job_id {overrides['job_id']!r}")
    entries = [JudgeEntry.model_validate(e) for e in raw]
    labels = {f["id"]: f["label"] for f in fields}
    merged = merge(overrides, entries, labels)
    overrides_file.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"merged {len(entries)} judged field(s) into {overrides_file}")
    return 0


def _run(func: Any) -> Any:
    def wrapped(args: argparse.Namespace) -> int:
        try:
            return int(func(args))
        except (JudgeCliError, AnswersError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    return wrapped


def register(sub: Any) -> None:
    """Add `judge-pack` and `judge-check` to the `apply` subparsers."""
    pk = sub.add_parser("judge-pack", help="write one role's apply-judge packet")
    pk.add_argument("job_id")
    pk.set_defaults(func=_run(_cmd_pack))
    ck = sub.add_parser("judge-check", help="check the apply-judge's answer and merge it")
    ck.add_argument("job_id")
    ck.set_defaults(func=_run(_cmd_check))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apply", description=__doc__)
    register(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
