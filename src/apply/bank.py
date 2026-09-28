"""The one writer of the answer bank (`profile/application_answers.yaml`).

`apply run --answers` calls `learn()` with what the user just confirmed, so the
next form that asks the same thing is answered without anyone deciding again:

- a label the session mapped to a question joins that question's `wordings`;
- a label a keyword guessed right joins `wordings` too, so it is exact next time;
- a keyword guess the user corrected joins the guessing question's `not:`;
- a board's spelling of a known answer joins its candidates;
- a confirmed answer to a new question becomes a question of its own;
- a work-authorization option judged true or false becomes a `claims:` entry.

Facts about the user (identity, work authorization per country) are never
written here: those change only when the user states them. Every write is
validated by loading the whole bank first, and the previous file is kept as
`<name>.bak`.

R7: the session decides what each label means; this module only records it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from src.apply import answers as A

DEFAULT_PATH = A.DEFAULT_PATH


class BankError(Exception):
    """A change this module refuses to write."""


@dataclass(frozen=True)
class Lesson:
    """One thing a confirmed run taught."""

    label: str
    question: str = ""
    """The bank question the label asks, existing or new."""
    value: str | tuple[str, ...] = ()
    """The confirmed answer."""
    offered: tuple[str, ...] = ()
    """The options the board offered, if any."""
    misfired: str = ""
    """The question whose keyword answered this label wrongly."""
    claim: dict | None = None
    """For a work-authorization option: the facts its text states."""
    source: str = ""
    job_id: str = ""


def read(path: Path | None = None) -> dict:
    p = Path(path) if path is not None else DEFAULT_PATH
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def write(data: dict, path: Path | None = None) -> Path:
    """Validate, keep the old file as `.bak`, then replace atomically."""
    p = Path(path) if path is not None else DEFAULT_PATH
    A.answers_from(data, where=str(p))
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    if p.exists():
        p.with_suffix(p.suffix + ".bak").write_text(p.read_text(encoding="utf-8"),
                                                     encoding="utf-8")
    os.replace(tmp, p)
    return p


def _as_list(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)] if value not in (None, "") else []


def _new_id(label: str, taken) -> str:
    base = A._PUNCT.sub(" ", A.norm_label(label)).replace(" ", "_")[:48].strip("_") or "question"
    qid, n = base, 2
    while qid in taken:
        qid, n = f"{base}_{n}", n + 1
    return qid


def _owner_of(questions: dict, wording: str) -> str:
    for qid, spec in questions.items():
        if wording in {A.norm_label(w) for w in spec.get("wordings") or []}:
            return qid
    return ""


def _add(spec: dict, key: str, value: str) -> bool:
    values = spec.setdefault(key, [])
    if value in values:
        return False
    values.append(value)
    return True


def apply_lessons(data: dict, lessons: list[Lesson]) -> list[str]:
    """Fold lessons into a raw bank mapping in place. Returns one line per change."""
    questions = data.setdefault("questions", {})
    claims = data.setdefault("claims", {}) or {}
    data["claims"] = claims
    changes: list[str] = []
    for lesson in lessons:
        wording = A.norm_label(lesson.label)
        if lesson.claim is not None:
            for option in _as_list(lesson.value):
                if A._norm_option(option) in A._BARE_OPTIONS:
                    continue
                if claims.get(option) != lesson.claim:
                    claims[option] = dict(lesson.claim)
                    changes.append(f"claim: {option!r} -> {lesson.claim}")
            continue
        if not wording or not lesson.question:
            continue

        if lesson.misfired and lesson.misfired != lesson.question \
                and lesson.misfired in questions:
            if _add(questions[lesson.misfired], "not", wording):
                changes.append(f"not: {lesson.misfired} no longer answers {wording!r}")

        qid = lesson.question
        if qid not in questions:
            if lesson.source.startswith("company_answers"):
                # A company's own answer is per role; the bank only ever points
                # at company_answers.yaml for it.
                continue
            if not _as_list(lesson.value):
                raise BankError(f"new question {qid!r} needs an answer")
            values = _as_list(lesson.value)
            spec = {"answer": values[0] if len(values) == 1 else values,
                    "wordings": [lesson.label.strip()]}
            if lesson.source:
                spec["source"] = lesson.source
            if lesson.job_id:
                spec["from"] = [lesson.job_id]
            questions[qid] = spec
            changes.append(f"new question {qid}: {lesson.label.strip()!r}")
            continue

        spec = questions[qid]
        owner = _owner_of(questions, wording)
        if owner and owner != qid:
            raise BankError(f"{wording!r} already belongs to {owner}, not {qid}")
        if not owner and _add(spec, "wordings", lesson.label.strip()):
            changes.append(f"wording -> {qid}: {lesson.label.strip()!r}")
        answer = spec.get("answer")
        if lesson.offered and not isinstance(answer, dict):
            candidates = _as_list(answer)
            known = {A._norm_option(c) for c in candidates}
            for option in _as_list(lesson.value):
                if option in lesson.offered and A._norm_option(option) not in known:
                    candidates.append(option)
                    changes.append(f"option -> {qid}: {option!r}")
            spec["answer"] = candidates[0] if len(candidates) == 1 else candidates
        if lesson.job_id:
            _add(spec, "from", lesson.job_id)
    return changes


def learn(lessons: list[Lesson], path: Path | None = None) -> list[str]:
    """Write every lesson into the bank. All or nothing: an invalid result
    writes nothing."""
    if not lessons:
        return []
    data = read(path)
    changes = apply_lessons(data, lessons)
    if changes:
        write(data, path)
    return changes


def undo(qid: str, wording: str = "", path: Path | None = None) -> str:
    """Remove one wording from a question, or the whole question."""
    data = read(path)
    questions = data.get("questions") or {}
    if qid not in questions:
        raise BankError(f"no question {qid!r}")
    if wording:
        target = A.norm_label(wording)
        spec = questions[qid]
        kept = [w for w in spec.get("wordings") or [] if A.norm_label(w) != target]
        if len(kept) == len(spec.get("wordings") or []):
            raise BankError(f"{qid} has no wording {wording!r}")
        spec["wordings"] = kept
        if not kept:
            spec.pop("wordings")
        done = f"removed {wording!r} from {qid}"
    else:
        questions.pop(qid)
        done = f"removed question {qid}"
    write(data, path)
    return done


def review(path: Path | None = None, since_job: str = "") -> list[str]:
    """Every question, its answer, and how it is found. `since_job` limits to
    questions a given job taught."""
    data = read(path)
    lines = []
    for qid, spec in (data.get("questions") or {}).items():
        taught = spec.get("from") or []
        if since_job and since_job not in taught:
            continue
        answer = spec.get("answer")
        shown = answer if not isinstance(answer, list) else " | ".join(map(str, answer))
        lines.append(f"{qid}: {str(shown)[:90]}")
        for w in spec.get("wordings") or []:
            lines.append(f"    = {w}")
        for k in spec.get("keywords") or []:
            lines.append(f"    ~ {k}")
        for n in spec.get("not") or []:
            lines.append(f"    ! {n}")
        if taught:
            lines.append(f"    from {', '.join(taught)}")
    for option, claim in (data.get("claims") or {}).items():
        lines.append(f"claim {option!r}: {claim}")
    return lines
