"""The one writer of the answer bank (`profile/application_answers.yaml`).

`apply run --answers` calls `learn()` with what the user just confirmed, so the
next form that asks the same thing is answered without anyone deciding again:

- a label the session mapped to a question joins that question's `wordings`;
- a label a keyword guessed right joins `wordings` too, so it is exact next time;
- a keyword guess the user corrected joins the guessing question's `not:`;
- a board's spelling of a known answer joins its candidates;
- a confirmed answer to a new question becomes a question of its own;
- a work-authorization option judged true or false becomes a `claims:` entry;
- a yes/no work-authorization or export-control label judged against one of
  your facts becomes a `fact_wordings:` entry.

Facts about the user (identity, work authorization per country) are never
written here: those change only when the user states them. Every write is
validated by loading the whole bank first, and the previous file is kept in
`.bank_history/` next to it (last 20).

R7: the session decides what each label means; this module only records it.
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from src.apply import answers as A

DEFAULT_PATH = A.DEFAULT_PATH

# Set to 1 by the night run: no bank write and no submit without a person there.
UNATTENDED_ENV = "APPLY_UNATTENDED"


def unattended() -> bool:
    return os.environ.get(UNATTENDED_ENV) == "1"


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
    keyword: str = ""
    """Words every wording of this question shares, so a new wording is guessed
    (and shown for confirmation) instead of parking."""
    fact: dict | None = None
    """For a yes/no work-authorization or export-control label: `{fact, yes_when}`."""


def read(path: Path | None = None) -> dict:
    p = Path(path) if path is not None else DEFAULT_PATH
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


HISTORY_KEEP = 20


def write(data: dict, path: Path | None = None) -> Path:
    """Validate, keep the old file in `.bank_history/`, then replace atomically."""
    if unattended():
        raise BankError(f"{UNATTENDED_ENV}=1: the answer bank is not written in an "
                        "unattended run")
    p = Path(path) if path is not None else DEFAULT_PATH
    A.answers_from(data, where=str(p))
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    if p.exists():
        history = p.parent / ".bank_history"
        history.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S-%f")
        (history / f"{p.stem}.{stamp}.yaml").write_text(p.read_text(encoding="utf-8"),
                                                          encoding="utf-8")
        for old in sorted(history.glob(f"{p.stem}.[0-9][0-9][0-9][0-9]-*.yaml"))[:-HISTORY_KEEP]:
            old.unlink()
    os.replace(tmp, p)
    return p


def _as_list(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)] if value not in (None, "") else []


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


# Sources a new question's answer may come from. A drafted sentence about the
# user (a bullet, a story), a board's own option, or the user's own words
# answers any employer the same way. Anything else (the JD, a company's
# answers) is about one employer and stays with that role.
_REUSABLE_SOURCES = ("B-", "S-", "SKILL-", "bullets", "stories", "option", "user")

# A new id this long that is the start of an existing one is a typo, not a
# new question ("do_you_have_at_least_3_years_of_profession").
_TYPO_PREFIX_MIN = 16


def _reusable(source: str) -> bool:
    return any(part.strip().startswith(_REUSABLE_SOURCES) for part in source.split(","))


def _one(data: dict, lesson: Lesson) -> tuple[list[str], str]:
    """Apply one lesson to `data` in place. Returns (changes, refusal)."""
    questions = data.setdefault("questions", {})
    wording = A.norm_label(lesson.label)
    changes: list[str] = []

    if lesson.claim is not None:
        claims = data.get("claims") or {}
        data["claims"] = claims
        for option in _as_list(lesson.value):
            if A._norm_option(option) not in A._BARE_OPTIONS and claims.get(option) != lesson.claim:
                claims[option] = dict(lesson.claim)
                changes.append(f"claim: {option!r} -> {lesson.claim}")
        return changes, ""
    if lesson.fact is not None:
        judged = data.get("fact_wordings") or {}
        data["fact_wordings"] = judged
        label = lesson.label.strip()
        taken = next((k for k in judged if A.norm_label(k) == wording), None)
        if taken is not None and judged[taken] != lesson.fact:
            return changes, (f"{label!r} is already judged {judged[taken]}; "
                             f"undo it first (apply learn --undo-fact)")
        if taken is None:
            judged[label] = dict(lesson.fact)
            changes.append(f"fact wording: {label!r} -> {lesson.fact}")
        return changes, ""
    if not wording or not lesson.question:
        return [], ""

    qid = lesson.question
    if qid not in questions:
        if not _reusable(lesson.source):
            return changes, (f"{qid}: an answer from {lesson.source or 'nowhere'} is about "
                             f"one employer, so it stays with this role")
        near = [q for q in questions if len(qid) >= _TYPO_PREFIX_MIN and q.startswith(qid)]
        if near:
            return changes, f"{qid}: did you mean {near[0]!r}?"
        values = _as_list(lesson.value)
        if not values:
            return changes, f"{qid}: a new question needs an answer"
        owner = _owner_of(questions, wording)
        if owner:
            return changes, f"{wording!r} already belongs to {owner}"
        spec = {"answer": values[0] if len(values) == 1 else values,
                "wordings": [lesson.label.strip()]}
        if lesson.source:
            spec["source"] = lesson.source
        if lesson.job_id:
            spec["from"] = [lesson.job_id]
        questions[qid] = spec
        changes.append(f"new question {qid}: {lesson.label.strip()!r}")
    else:
        spec = questions[qid]
        owner = _owner_of(questions, wording)
        if owner and owner != qid:
            return changes, f"{wording!r} already belongs to {owner}, not {qid}"
        answer = spec.get("answer")
        if lesson.offered and not isinstance(answer, dict):
            candidates = _as_list(answer)
            offered = {A._norm_option(o) for o in lesson.offered}
            known_here = [c for c in candidates if A._norm_option(c) in offered]
            for option in _as_list(lesson.value):
                if A._norm_option(option) in {A._norm_option(c) for c in candidates}:
                    continue
                if known_here:
                    # The board offered the bank's own answer and the user
                    # chose another: a different question, or a wrong answer.
                    return changes, (f"{qid} answers {known_here[0]!r}, this form was "
                                     f"answered {option!r}; give it its own question id")
                candidates.append(option)
                changes.append(f"option -> {qid}: {option!r}")
            spec["answer"] = candidates[0] if len(candidates) == 1 else candidates
        if not owner and _add(spec, "wordings", lesson.label.strip()):
            changes.append(f"wording -> {qid}: {lesson.label.strip()!r}")
        if lesson.job_id:
            _add(spec, "from", lesson.job_id)
    if lesson.keyword:
        changes.extend(_add_keyword(data, qid, lesson.keyword))
    return changes, ""


def _still_answers(data: dict, lesson: Lesson) -> str:
    """"" when the bank, with this lesson in, finds the lesson's question for
    its label; else why not."""
    if lesson.claim is not None or (not lesson.question and lesson.fact is None):
        return ""
    try:
        bank = A.answers_from(data)
    except A.AnswersError as exc:
        return f"the bank would not load: {exc}"
    if lesson.fact is not None:
        return ("" if A.norm_label(lesson.label) in bank.fact_wordings
                else f"{lesson.label.strip()!r} would not be found")
    found = A.find_question(lesson.label, bank)
    if found is None or found[0].id != lesson.question:
        got = found[0].id if found else "nothing"
        return f"{lesson.label.strip()!r} would find {got}, not {lesson.question}"
    return ""


def apply_lessons(data: dict, lessons: list[Lesson]) -> list[str]:
    """Fold lessons into a raw bank mapping in place, one at a time. A lesson
    is kept only if the bank still loads and still finds that lesson's question
    for its label; a refused lesson is named and the others stand."""
    changes: list[str] = []
    for lesson in lessons:
        wording = A.norm_label(lesson.label)
        trial = copy.deepcopy(data)
        made, refusal = _one(trial, lesson)
        refusal = refusal or (_still_answers(trial, lesson) if made else "")
        if refusal:
            changes.append(f"not saved: {refusal}")
            continue
        questions = trial.get("questions") or {}
        if (lesson.misfired and lesson.misfired != lesson.question  # noqa: SIM102
                and lesson.misfired in questions and wording
                and lesson.question in questions):
            # Only once the right answer is saved: a "not" with nowhere else
            # for the label to go would just park it.
            if _add(questions[lesson.misfired], "not", wording):
                made.append(f"not: {lesson.misfired} no longer answers {wording!r}")
        data.clear()
        data.update(trial)
        changes.extend(made)
    return changes


def _add_keyword(data: dict, qid: str, keyword: str) -> list[str]:
    """Add a keyword only if the bank still loads with it: a keyword that
    shadows another question, or asks a work-authorization question, is
    refused on its own and the rest of the lesson stands."""
    spec = data["questions"][qid]
    word = A.norm_label(keyword)
    if not word or word in [A.norm_label(k) for k in spec.get("keywords") or []]:
        return []
    spec.setdefault("keywords", []).append(word)
    try:
        A.answers_from(data)
    except A.AnswersError as exc:
        spec["keywords"].remove(word)
        if not spec["keywords"]:
            spec.pop("keywords")
        return [f"keyword refused for {qid}: {word!r} ({exc})"]
    return [f"keyword -> {qid}: {word!r}"]


def learn(lessons: list[Lesson], path: Path | None = None) -> list[str]:
    """Write every lesson that holds into the bank; name the ones that do not."""
    if not lessons:
        return []
    data = read(path)
    changes = apply_lessons(data, lessons)
    if any(not c.startswith(("not saved:", "keyword refused")) for c in changes):
        write(data, path)
    return changes


def undo(qid: str = "", wording: str = "", keyword: str = "", claim: str = "",
         fact: str = "", path: Path | None = None) -> str:
    """Take one thing back: a whole question, one wording or keyword of it, one
    claim, or one fact wording. A question left with no way to be found is
    removed whole."""
    data = read(path)
    if fact:
        judged = data.get("fact_wordings") or {}
        key = next((k for k in judged if A.norm_label(k) == A.norm_label(fact)), None)
        if key is None:
            raise BankError(f"no fact wording {fact!r}")
        judged.pop(key)
        if not judged:
            data.pop("fact_wordings", None)
        write(data, path)
        return f"removed fact wording {key!r}"
    if claim:
        claims = data.get("claims") or {}
        if claim not in claims:
            raise BankError(f"no claim {claim!r}")
        claims.pop(claim)
        write(data, path)
        return f"removed claim {claim!r}"
    questions = data.get("questions") or {}
    if qid not in questions:
        raise BankError(f"no question {qid!r}")
    spec = questions[qid]
    if wording or keyword:
        key, target = ("wordings", wording) if wording else ("keywords", keyword)
        kept = [w for w in spec.get(key) or [] if A.norm_label(w) != A.norm_label(target)]
        if len(kept) == len(spec.get(key) or []):
            raise BankError(f"{qid} has no {key[:-1]} {target!r}")
        spec[key] = kept
        if not kept:
            spec.pop(key)
        done = f"removed {target!r} from {qid}"
        if not spec.get("wordings") and not spec.get("keywords"):
            questions.pop(qid)
            done += f"; {qid} had no other way to be found, so it is removed"
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
    for label, judged in (data.get("fact_wordings") or {}).items():
        lines.append(f"fact wording {label!r}: Yes when {judged['fact']} is "
                     f"{str(judged['yes_when']).lower()}")
    return lines
