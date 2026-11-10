"""`apply-eval`: stage the /apply eval for the apply-judge, and grade what it wrote.

`stage` groups cases by (persona, job country) into slices, each a packet the
judge reads. `grade` puts every judge entry through `plan.apply_override`,
the same gate and renderer `plan` uses, and compares it with the case's
expectation. Never
judges (R7): `scripts/eval_apply.sh` spawns the judges.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import random
import re
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from applyourself.apply.answers import (
    UNKNOWN_COUNTRY,
    Answers,
    Resolution,
    answer_question,
    resolve,
    resolve_fact_wording,
)
from applyourself.apply.bank_schema import load_answers
from applyourself.apply.judge_packet import JudgeEntry, build_packet, check_entries, to_override
from applyourself.apply.judged import checkbox_yes, field_from_dict
from applyourself.apply.plan import apply_override
from applyourself.evals.cases import (
    CASES_DIR,
    GATE_GROUPS,
    REAL_PERSONA,
    Case,
    load_cases,
    load_persona,
    persona_ids,
    persona_path,
    private_dir,
)
from applyourself.shared import paths

SLICE_SIZE = 25
OVERALL_MIN = 0.98
REPEAT_MIN = 0.99


def runs_dir() -> Path:
    return paths.APPLICATIONS / "evals" / "apply" / "runs"


class EvalError(Exception):
    pass


# ---------------------------------------------------------------- personas


def persona_answers(name: str, job_country: str) -> Answers:
    """The persona's bank for one job country, with the salary figure /apply
    would ask for there; `real` is the real bank. No job country reads as
    `UNKNOWN_COUNTRY`, as /apply does, never the home country."""
    from applyourself.apply_cli import salary_for

    answers = load_answers() if name == REAL_PERSONA else load_persona(persona_path(name))
    answers = replace(answers, job_country=job_country or UNKNOWN_COUNTRY)
    return replace(answers, salary_figure=salary_for("", "", answers.country, answers))


def persona_known_ids(name: str) -> set[str] | None:
    """The profile ids a persona's prose may cite; None reads the real profile."""
    return None if name == REAL_PERSONA else persona_ids(name)


def _persona_sources(packet: str, name: str) -> str:
    """A persona's packet names its own ids file, not the real profile's."""
    sidecar = persona_path(name).with_suffix(".ids.txt")
    ids = sorted(persona_ids(name))
    section = (
        "## Sources (Read only when drafting text)\n\n"
        f"- this persona's profile ids (the only B-/S-/SKILL- ids to cite): {sidecar}\n"
        f"- ids: {', '.join(ids) or 'none'}\n"
    )
    return re.sub(r"## Sources .*?(?=## Fields)", lambda _: section + "\n", packet, flags=re.S)


# ---------------------------------------------------------------- stage


def _case_files(which: str) -> list[Path]:
    root = CASES_DIR if which == "public" else private_dir()
    files = sorted(root.glob("*.jsonl"))
    if not files:
        raise EvalError(f"no case files under {root}")
    return files


def _field(case: Case) -> dict[str, Any]:
    return case.field.model_dump() | {"id": case.id}


def stage(which: str, sample: int | None, seed: int, out_root: Path | None = None) -> Path:
    """Write one run dir of packets and metas; print one `judge` line per slice."""
    cases = [c for f in _case_files(which) for c in load_cases(f)]
    ids = [c.id for c in cases]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise EvalError(f"case ids repeat across files: {dupes[:5]}")
    if sample is not None and sample < len(cases):
        cases = random.Random(seed).sample(cases, sample)
    run = (out_root or runs_dir()) / f"{datetime.now().astimezone():%Y-%m-%d_%H%M%S}-{which}"
    run.mkdir(parents=True, exist_ok=False)
    groups: dict[tuple[str, str], list[Case]] = {}
    for c in cases:
        groups.setdefault((c.persona, c.job_country), []).append(c)
    lines = [f"run {run}"]
    n = 0
    for (persona, country), members in sorted(groups.items()):
        answers = persona_answers(persona, country)
        for i in range(0, len(members), SLICE_SIZE):
            n += 1
            part = members[i : i + SLICE_SIZE]
            fields = [_field(c) for c in part]
            packet = build_packet(fields, answers, country)
            if persona != REAL_PERSONA:
                packet = _persona_sources(packet, persona)
            (run / f"packet_{n}.md").write_text(packet, encoding="utf-8")
            meta = {
                "persona": persona,
                "job_country": country,
                "cases": [c.model_dump(exclude_none=True) for c in part],
            }
            (run / f"meta_{n}.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
            lines.append(f"judge {run / f'packet_{n}.md'} -> {run / f'out_{n}.json'}")
    meta_run = {"set": which, "sample": sample, "seed": seed, "cases": len(cases), "slices": n}
    (run / "run.json").write_text(json.dumps(meta_run, indent=1) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return run


# ---------------------------------------------------------------- grade


def _slices(run: Path) -> list[tuple[int, dict[str, Any]]]:
    out = []
    for meta in run.glob("meta_*.json"):
        n = int(meta.stem.split("_")[1])
        out.append((n, json.loads(meta.read_text(encoding="utf-8"))))
    if not out:
        raise EvalError(f"{run}: no meta_*.json; run apply-eval stage first")
    return sorted(out, key=lambda t: t[0])


def norm(value: Any) -> Any:
    """A rendered value for comparison: case and spacing folded; a multi-select
    sorted; one picked option equals the same scalar."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)) and len(value) == 1:
        return norm(value[0])
    if isinstance(value, (list, tuple)):
        return sorted(norm(v) for v in value)
    return " ".join(str(value).split()).casefold()


def _action(res: Resolution, decision: str) -> str:
    if res.action == "park":
        return "ask" if decision == "ask" else "park"
    return res.action


def _expected(case: Case, field, answers: Answers) -> tuple[str, Any, str, str]:
    """`(action, normalized value or values, problem, park reason)` the case
    wants. The park reason is set when an expected question itself parks."""
    exp = case.expect
    if exp.action != "fill":
        return exp.action, None, "", ""
    if exp.value is not None:
        if field.kind == "checkbox" and checkbox_yes(field, exp.value):
            return "fill", norm("Yes"), "", ""  # a ticked checkbox renders Yes
        return "fill", norm(exp.value), "", ""
    if exp.one_of is not None:
        return "fill", [norm(v) for v in exp.one_of], "", ""
    if exp.prose:
        return "fill", None, "", ""
    if exp.question is not None:
        q = answers.question(exp.question)
        if q is None:
            return "fill", None, f"expected question {exp.question!r} is not in the bank", ""
        res = answer_question(q, field, answers, f"bank:{q.id}")
    else:
        assert exp.fact is not None
        res = resolve_fact_wording(field, answers, exp.fact.model_dump())
    if res.action != "fill":
        parked = res.reason if res.action == "park" and exp.question is not None else ""
        return "fill", None, f"the expectation itself renders {res.action}: {res.reason}", parked
    return "fill", norm(res.value), "", ""


def _overrides(run: Path, n: int, entries: dict[str, JudgeEntry], cases: list[Case]) -> dict:
    """The checked entries as `answers_override.json` reads them back (C4)."""
    from applyourself.apply_cli import load_overrides

    labels = {c.id: c.field.label for c in cases}
    data = {fid: to_override(e, labels[fid]) for fid, e in entries.items()}
    path = run / f"override_{n}.json"
    path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    return load_overrides(path)


def grade_case(
    case: Case,
    entry: JudgeEntry | None,
    override: Any,
    answers: Answers,
    known_ids: set[str] | None,
    problems: list[str],
) -> dict[str, Any]:
    """One case's result: pass, what it wanted, what the judge's entry renders to."""
    field_dict = _field(case)
    field = field_from_dict(field_dict)
    want_action, want_value, want_problem, want_park = _expected(case, field, answers)
    row: dict[str, Any] = {
        "id": case.id,
        "group": case.group,
        "persona": case.persona,
        "label": case.field.label,
        "expected": {"action": want_action, "value": want_value},
        "decision": entry.decision if entry else "",
        "why": entry.why if entry else "",
    }
    if entry is None or problems:
        row.update(passed=False, got={"action": "invalid", "problems": problems}, key=None)
        return row
    res, ignored = apply_override(
        field, resolve(field, answers), override, answers, known_ids=known_ids
    )
    if ignored:
        row.update(passed=False, got={"action": "ignored", "reason": f"ignored: {ignored}"})
        row["key"] = [entry.decision, "ignored"]
        return row
    got_action, got_value = _action(res, entry.decision), norm(res.value)
    row["got"] = {"action": got_action, "value": got_value, "reason": res.reason}
    # Repeat agreement compares outcomes, not the route: the same choice filled,
    # or free text filled (wording varies), or the field stopped for the user.
    if got_action != "fill":
        row["key"] = ["stop" if got_action in ("ask", "park") else got_action]
    elif field.options or field.kind == "checkbox":
        row["key"] = ["fill", got_value]
    else:
        row["key"] = ["text"]
    if want_park and got_action == "park" and res.reason == want_park:
        # The question parks without a live role (no role file, no posting):
        # right only when the judge named that question.
        passed = entry.decision == "question" and entry.question == case.expect.question
        row["passed"] = passed
        if not passed:
            row["problem"] = want_problem
        return row
    if want_problem:
        row.update(passed=False, problem=want_problem)
        return row
    if case.expect.or_ask and got_action == "ask":
        passed = True  # a declared second reading: stopping for the user
    elif (
        want_action == "ask" and got_action == "park" and "country could not be read" in res.reason
    ):
        passed = True  # the code stopped it for the user, as an ask would
    elif got_action != want_action:
        passed = False
    elif want_action != "fill":
        passed = True
    elif case.expect.prose:
        passed = bool(got_value)
    elif case.expect.one_of is not None:
        passed = got_value in want_value
    else:
        passed = got_value == want_value
    row["passed"] = passed
    if case.expect.or_ask or len(case.expect.one_of or []) > 1:
        # Two fair answers are declared: agreement asks only that both runs
        # landed on an accepted one.
        row["key"] = ["accepted" if passed else "miss"]
    return row


def _read_out(path: Path) -> tuple[Any, str]:
    if not path.is_file():
        return None, "no out file"
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except json.JSONDecodeError as exc:
        return None, f"out file is not JSON ({exc})"


def grade_slice(run: Path, n: int, meta: dict[str, Any]) -> list[dict[str, Any]]:
    cases = [Case.model_validate(c) for c in meta["cases"]]
    answers = persona_answers(meta["persona"], meta["job_country"])
    known = persona_known_ids(meta["persona"])
    raw, read_problem = _read_out(run / f"out_{n}.json")
    fields = [_field(c) for c in cases]
    problems = [read_problem] if read_problem else check_entries(raw, fields, answers)
    ids = {c.id for c in cases}
    by_case: dict[str, list[str]] = {i: [] for i in ids}
    shared: list[str] = []
    for p in problems:
        fid = p.split(":", 1)[0]
        if fid in ids:
            by_case[fid].append(p)
        elif not p.startswith("field_ids do not match"):
            shared.append(p)  # the whole file is refused
    entries: dict[str, JudgeEntry] = {}
    for e in raw if isinstance(raw, list) else []:
        if isinstance(e, dict) and e.get("field_id") in ids and not by_case[e["field_id"]]:
            with contextlib.suppress(ValueError):
                entries[e["field_id"]] = JudgeEntry.model_validate(e)
    from applyourself.apply_cli import ApplyCliError

    try:
        overrides = _overrides(run, n, entries, cases)
    except ApplyCliError as exc:
        shared.append(f"override file refused: {exc}")
        overrides = {}
    rows = []
    for c in cases:
        mine = shared + by_case[c.id]
        if c.id not in entries and not mine:
            mine = [read_problem or "not answered"]
        rows.append(grade_case(c, entries.get(c.id), overrides.get(c.id), answers, known, mine))
    return rows


def grade_run(run: Path) -> list[dict[str, Any]]:
    return [row for n, meta in _slices(run) for row in grade_slice(run, n, meta)]


def owed(run: Path) -> list[str]:
    """`judge` lines for slices whose out file is missing or refused."""
    lines = []
    for n, meta in _slices(run):
        cases = [Case.model_validate(c) for c in meta["cases"]]
        raw, problem = _read_out(run / f"out_{n}.json")
        if not problem:
            answers = persona_answers(meta["persona"], meta["job_country"])
            if not check_entries(raw, [_field(c) for c in cases], answers):
                continue
        lines.append(f"judge {run / f'packet_{n}.md'} -> {run / f'out_{n}.json'}")
    return lines


def agreement(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> tuple[float, list[str]]:
    """Share of common cases where both runs made the same decision and rendered
    the same value; and the ids that differ."""
    kb = {r["id"]: r.get("key") for r in b}
    common = [r for r in a if r["id"] in kb]
    differ = [r["id"] for r in common if r.get("key") is None or r.get("key") != kb[r["id"]]]
    return (1 - len(differ) / len(common)) if common else 0.0, differ


def gate(rows: list[dict[str, Any]], repeat: float | None = None) -> dict[str, Any]:
    by_group: dict[str, list[bool]] = {}
    for r in rows:
        by_group.setdefault(r["group"], []).append(bool(r["passed"]))
    rates = {g: sum(v) / len(v) for g, v in sorted(by_group.items())}
    overall = sum(r["passed"] for r in rows) / len(rows) if rows else 0.0
    failed = [f"{g} {rates[g]:.1%} < 100%" for g in GATE_GROUPS if rates.get(g, 1.0) < 1.0]
    if overall < OVERALL_MIN:
        failed.append(f"overall {overall:.1%} < {OVERALL_MIN:.0%}")
    if repeat is not None and repeat < REPEAT_MIN:
        failed.append(f"repeat agreement {repeat:.1%} < {REPEAT_MIN:.0%}")
    return {
        "groups": {g: {"rate": rates[g], "n": len(by_group[g])} for g in rates},
        "overall": overall,
        "repeat": repeat,
        "passed": not failed,
        "failed": failed,
    }


def cost(run: Path) -> float:
    total = 0.0
    for f in run.glob("*.cost"):
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                total += float(line.split()[-1])
            except (ValueError, IndexError):
                continue
    return total


def _report_md(report: dict[str, Any]) -> str:
    g = report["gate"]
    out = [
        f"# Apply eval: {report['run']}\n",
        f"Gate: {'PASS' if g['passed'] else 'FAIL'}"
        + (f" ({'; '.join(g['failed'])})" if g["failed"] else ""),
        f"Overall: {g['overall']:.1%} of {report['cases']} cases",
    ]
    if g["repeat"] is not None:
        out.append(f"Repeat agreement: {g['repeat']:.1%}")
    c = report["cost"]
    out.append(f"Cost: ${c['total']:.4f} (${c['per_slice']:.4f}/slice, ${c['per_case']:.5f}/case)")
    out += ["", "| group | gate | pass | n |", "|---|---|---|---|"]
    for name, v in g["groups"].items():
        out.append(
            f"| {name} | {'yes' if name in GATE_GROUPS else ''} | {v['rate']:.1%} | {v['n']} |"
        )
    out += ["", "## Misses", ""]
    for m in report["misses"]:
        out.append(
            f"- `{m['id']}` ({m['group']}) {m['label']!r}: expected {m['expected']}, "
            f"got {m['got']}"
            + (f"; {m['problem']}" if m.get("problem") else "")
            + (f"; why: {m['why']}" if m["why"] else "")
        )
    if report.get("disagree"):
        out += ["", "## Repeat disagreements", "", ", ".join(report["disagree"])]
    return "\n".join(out) + "\n"


def grade(run: Path, repeat_run: Path | None = None) -> dict[str, Any]:
    rows = grade_run(run)
    rate: float | None = None
    differ: list[str] = []
    if repeat_run is not None:
        rate, differ = agreement(rows, grade_run(repeat_run))
    slices = len(_slices(run))
    total = cost(run)
    report = {
        "run": str(run),
        "repeat_run": str(repeat_run) if repeat_run else None,
        "cases": len(rows),
        "gate": gate(rows, rate),
        "cost": {
            "total": total,
            "per_slice": total / slices if slices else 0.0,
            "per_case": total / len(rows) if rows else 0.0,
        },
        "misses": [{k: v for k, v in r.items() if k != "key"} for r in rows if not r["passed"]],
        "disagree": differ,
        "rows": rows,
    }
    (run / "report.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    (run / "report.md").write_text(_report_md(report), encoding="utf-8")
    return report


# ---------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="apply-eval", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("stage", help="write packets for one case set")
    s.add_argument("--set", dest="which", choices=("public", "private"), required=True)
    s.add_argument("--sample", type=int, help="grade only N random cases")
    s.add_argument("--seed", type=int, default=0)
    g = sub.add_parser("grade", help="grade a run dir; exit 1 when the gate fails")
    g.add_argument("run_dir", type=Path)
    g.add_argument("--repeat", type=Path, help="a second run of the same cases")
    o = sub.add_parser("owed", help="judge lines for slices with no valid out file")
    o.add_argument("run_dir", type=Path)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "stage":
            stage(args.which, args.sample, args.seed)
            return 0
        if args.cmd == "owed":
            print("\n".join(owed(args.run_dir)))
            return 0
        report = grade(args.run_dir, args.repeat)
    except EvalError as exc:
        print(f"apply-eval: {exc}", file=sys.stderr)
        return 2
    g = report["gate"]
    print(
        f"gate={'pass' if g['passed'] else 'fail'} overall={g['overall']:.1%} "
        f"cases={report['cases']} misses={len(report['misses'])} cost=${report['cost']['total']:.4f}"
    )
    for f in g["failed"]:
        print(f"  {f}")
    print(f"report {args.run_dir / 'report.md'}")
    return 0 if g["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
