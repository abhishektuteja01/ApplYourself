"""Review the work-authorization wordings saved plans park on, in one file.

`review()` lists each distinct yes/no work-authorization label in the saved
`plan.json` files whose wording the bank's `fact_wordings` does not hold yet,
with the fact the regex families suggest and the answer saving it would give.
The owner sets each row's `decision` to yes or no; `to_save()` reads back the
yes rows as `bank.Lesson`s, which `apply wordings --save` hands to `bank.learn`.

R7: the owner decides; this module only lists and reads back.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import yaml

from src.apply import answers as A
from src.apply import bank
from src.apply.reconcile import MergedField, MergedOption
from src.shared import paths

_PLAN_KEYS = ("fields", "unmapped", "draftable")

HEADER = (
    "# Work-authorization wordings your saved plans park on, not yet in the bank.\n"
    "# decision: yes saves the row as an exact fact_wordings entry (Yes when\n"
    "# <fact> is true); no or blank saves nothing. Edit fact to save another one.\n"
    "# Then run, in your terminal:\n"
    "#   uv run apply wordings --save <this file>\n"
)


class WordingsError(Exception):
    """The review file cannot be read back."""


def default_out() -> Path:
    return paths.APPLY_RUNS / "wordings_review.yaml"


def _plan_files() -> list[Path]:
    runs = paths.APPLY_RUNS.resolve()
    return sorted(
        p for p in paths.APPLICATIONS.glob("*/*/plan.json") if p.parent.parent.resolve() != runs
    )


def _field(entry: dict) -> MergedField:
    return MergedField(
        id=str(entry.get("id") or ""),
        name=str(entry.get("name") or entry.get("id") or ""),
        label=str(entry.get("label") or ""),
        required=bool(entry.get("required")),
        kind=str(entry.get("kind") or ""),
        section=str(entry.get("section") or ""),
        multi=bool(entry.get("multi")),
        options=tuple(MergedOption(label=str(o)) for o in entry.get("options") or ()),
    )


def _shown(value) -> str:
    return ", ".join(map(str, value)) if isinstance(value, (list, tuple)) else str(value)


def _decision(raw) -> str:
    """yes / no / "" — YAML reads a bare yes or no as a bool."""
    if isinstance(raw, bool):
        return "yes" if raw else "no"
    text = str(raw or "").strip().lower()
    return text if text in ("yes", "no") else ""


def _earlier_decisions(out: Path) -> dict[str, str]:
    try:
        rows = (yaml.safe_load(out.read_text(encoding="utf-8")) or {}).get("wordings") or []
    except (OSError, yaml.YAMLError, AttributeError):
        return {}
    return {
        A.norm_label(str(r.get("label") or "")): _decision(r.get("decision"))
        for r in rows
        if isinstance(r, dict) and _decision(r.get("decision"))
    }


def review(bank_answers: A.Answers, out: Path | None = None) -> tuple[Path, list[dict]]:
    """Write the review file and return (its path, its rows). A decision already
    set in an earlier file is kept for a wording still listed."""
    out = Path(out) if out is not None else default_out()
    seen: dict[str, dict] = {}
    for plan_file in _plan_files():
        try:
            plan = json.loads(plan_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        country = (plan.get("work_authorization") or {}).get("job_country") or A.UNKNOWN_COUNTRY
        scoped = replace(bank_answers, job_country=country)
        for key in _PLAN_KEYS:
            for entry in plan.get(key) or []:
                field = _field(entry)
                norm = A.norm_label(field.label)
                if not A.in_work_authorization_domain(field.label):
                    continue
                if norm in bank_answers.fact_wordings:
                    continue
                got = A.resolve(field, scoped)
                if not got.source.startswith("suggest:"):
                    continue
                fact = got.source.split(":", 1)[1]
                judged = {"fact": fact, "yes_when": True}
                computed = A.resolve_fact_wording(field, scoped, judged)
                row = seen.setdefault(
                    norm, {"label": field.label.strip(), "fact": fact, "forms": set(), "by": {}}
                )
                row["forms"].add(str(plan.get("job_id") or plan_file.parent.name))
                row["by"].setdefault(str(country), _shown(computed.value))
    earlier = _earlier_decisions(out)
    rows = []
    for norm, row in seen.items():
        answers = set(row["by"].values())
        answer = (
            answers.pop()
            if len(answers) == 1
            else "; ".join(f"{a} in {c}" for c, a in sorted(row["by"].items()))
        )
        rows.append(
            {
                "label": row["label"],
                "forms": len(row["forms"]),
                "fact": row["fact"],
                "answer": answer,
                "decision": earlier.get(norm, ""),
            }
        )
    rows.sort(key=lambda r: (-r["forms"], r["label"]))
    text = HEADER + yaml.safe_dump(
        {"wordings": rows}, sort_keys=False, allow_unicode=True, width=100
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, out)
    return out, rows


def to_save(path: Path) -> list[bank.Lesson]:
    """The rows marked `decision: yes`, as exact `fact_wordings` lessons."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise WordingsError(f"{path}: not readable ({exc})") from exc
    rows = data.get("wordings") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise WordingsError(f"{path}: no wordings: list")
    lessons = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or _decision(row.get("decision")) != "yes":
            continue
        label, fact = str(row.get("label") or "").strip(), row.get("fact")
        if not label or fact not in A.FACT_WORDING_FACTS:
            raise WordingsError(
                f"{path}: wordings[{i}] needs a label and a fact in {list(A.FACT_WORDING_FACTS)}"
            )
        lessons.append(bank.Lesson(label=label, fact={"fact": fact, "yes_when": True}))
    return lessons
