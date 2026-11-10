"""Review the written answers the judge drafted, so the good ones join the bank.

`review()` lists each judged `text` answer in the roles' `answers_override.json`
files that rests only on profile entries (`B-`/`S-`/`SKILL-`), runs to more
than one sentence, and whose label no bank question answers yet, with a
proposed question id. The owner sets each row's `decision` to yes or no;
`to_save()` reads back the yes rows as `bank.Lesson`s of kind `question`, which
`apply prose --save` hands to `bank.learn`.

R7: the owner decides; this module only lists and reads back.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from applyourself.apply import bank
from applyourself.apply.bank_schema import Answers
from applyourself.shared import paths
from applyourself.shared.files import atomic_write_text

HEADER = (
    "# Written answers the judge drafted from your profile, not yet in the bank.\n"
    "# decision: yes saves the row's answer as bank question <question> (edit the\n"
    "# id or the answer first if you like); no or blank saves nothing.\n"
    "# Then run, in your terminal:\n"
    "#   uv run apply prose --save <this file>\n"
)

_PROFILE_PREFIXES = ("B-", "S-", "SKILL-")
_QID = re.compile(r"^[a-z0-9_]+$")
_QID_MAX = 40
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


class PendingError(Exception):
    """The review file cannot be read back."""


def default_out() -> Path:
    return paths.APPLY_RUNS / "prose_review.yaml"


def _norm(label: str) -> str:
    return " ".join(label.replace("✱", " ").replace("*", " ").split()).casefold()


def question_id(label: str) -> str:
    """A bank question id proposed for a label: its words, lowercase, `_`-joined."""
    words = re.findall(r"[a-z0-9]+", label.casefold())
    out = ""
    for word in words:
        nxt = f"{out}_{word}" if out else word
        if len(nxt) > _QID_MAX:
            break
        out = nxt
    return out or "question"


def _sentences(text: str) -> int:
    return len([s for s in _SENTENCE_END.split(text.strip()) if s.strip()])


def _answer_files() -> list[Path]:
    runs = paths.APPLY_RUNS.resolve()
    return sorted(
        p
        for p in paths.APPLICATIONS.glob("*/*/answers_override.json")
        if p.parent.parent.resolve() != runs
    )


def _plan_labels(folder: Path) -> dict[str, str]:
    try:
        plan = json.loads((folder / "plan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        str(e.get("id")): str(e.get("label") or "")
        for key in ("fields", "unmapped", "draftable")
        for e in plan.get(key) or []
        if isinstance(e, dict)
    }


def _decision(raw) -> str:
    """yes / no / "" — YAML reads a bare yes or no as a bool."""
    if isinstance(raw, bool):
        return "yes" if raw else "no"
    text = str(raw or "").strip().lower()
    return text if text in ("yes", "no") else ""


def _earlier(out: Path) -> dict[str, dict]:
    try:
        rows = (yaml.safe_load(out.read_text(encoding="utf-8")) or {}).get("prose") or []
    except (OSError, yaml.YAMLError, AttributeError):
        return {}
    return {
        _norm(str(r.get("label") or "")): r
        for r in rows
        if isinstance(r, dict) and _decision(r.get("decision"))
    }


def collect(bank_answers: Answers) -> list[dict]:
    """The rows `review()` would write, without the earlier decisions."""
    known = {q.id for q in bank_answers.questions}
    seen: dict[str, dict] = {}
    for path in _answer_files():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        labels: dict[str, str] | None = None
        for field_id, entry in data.items():
            if not isinstance(entry, dict) or entry.get("decision") != "text":
                continue
            basis = entry.get("basis")
            value = entry.get("value")
            if (
                not isinstance(basis, list)
                or not basis
                or not all(isinstance(b, str) and b.startswith(_PROFILE_PREFIXES) for b in basis)
                or not isinstance(value, str)
                or _sentences(value) < 2
            ):
                continue
            label = str(entry.get("label") or "").strip()
            if not label:
                if labels is None:
                    labels = _plan_labels(path.parent)
                label = labels.get(field_id, "").strip()
            qid = question_id(label)
            if not label or qid in known:
                continue
            row = seen.setdefault(
                _norm(label),
                {
                    "label": label,
                    "question": qid,
                    "answer": value.strip(),
                    "source": ",".join(b.strip() for b in basis),
                    "forms": set(),
                    "decision": "",
                },
            )
            row["forms"].add(str(data.get("job_id") or path.parent.name))
    rows = [{**r, "forms": len(r["forms"])} for r in seen.values()]
    rows.sort(key=lambda r: (-r["forms"], r["label"]))
    return rows


def review(bank_answers: Answers, out: Path | None = None) -> tuple[Path, list[dict]]:
    """Write the review file and return (its path, its rows). A row decided in
    an earlier file keeps its decision, question id and answer."""
    out = Path(out) if out is not None else default_out()
    earlier = _earlier(out)
    rows = []
    for row in collect(bank_answers):
        before = earlier.get(_norm(row["label"]))
        if before:
            row = {
                **row,
                "question": str(before.get("question") or row["question"]),
                "answer": str(before.get("answer") or row["answer"]),
                "decision": _decision(before.get("decision")),
            }
        rows.append(row)
    text = HEADER + yaml.safe_dump({"prose": rows}, sort_keys=False, allow_unicode=True, width=100)
    atomic_write_text(out, text, mkdir=True)
    return out, rows


def to_save(path: Path) -> list[bank.Lesson]:
    """The rows marked `decision: yes`, as question lessons."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PendingError(f"{path}: not readable ({exc})") from exc
    rows = data.get("prose") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise PendingError(f"{path}: no prose: list")
    lessons = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or _decision(row.get("decision")) != "yes":
            continue
        qid = str(row.get("question") or "").strip()
        answer = str(row.get("answer") or "").strip()
        source = str(row.get("source") or "").strip()
        if not _QID.match(qid) or not answer:
            raise PendingError(f"{path}: prose[{i}] needs a question id ([a-z0-9_]+) and an answer")
        if not source or not all(
            s.strip().startswith(_PROFILE_PREFIXES) for s in source.split(",")
        ):
            raise PendingError(f"{path}: prose[{i}] source must be B-/S-/SKILL- ids")
        lessons.append(
            bank.Lesson(
                kind="question",
                id=qid,
                value=answer,
                source=source,
                label=str(row.get("label") or "").strip(),
            )
        )
    return lessons
