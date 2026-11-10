"""The one writer of the answer bank (`profile/application_answers.yaml`).

It learns two things, each under the user's typed confirmation:

- a user's answer to an `ask` becomes a new `facts:` line;
- an approved new prose answer becomes a new `questions:` entry, its `source`
  the `B-`/`S-`/`SKILL-` ids it was written from.

An existing fact or question is never overwritten: `undo` it first. Every write
is validated by loading the whole bank, keeps the bank's text (comments
included) where it can, and keeps the previous file in `.state/bank_history/`
next to it (last 20). Nothing is written in an unattended run.

R7: the session decides what each answer says; this module only records it.
"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from applyourself.apply import bank_schema as BS
from applyourself.shared import clock, paths
from applyourself.shared.files import atomic_write_text
from applyourself.shared.parquet_io import locked

# Set to 1 by the night run: no bank write and no submit without a person there.
UNATTENDED_ENV = "APPLY_UNATTENDED"

HISTORY_KEEP = 20

# The profile ids an approved prose answer may be written from.
PROSE_SOURCES = ("B-", "S-", "SKILL-")

_QID = re.compile(r"^[a-z0-9_]+$")


def unattended() -> bool:
    return os.environ.get(UNATTENDED_ENV) == "1"


class BankError(Exception):
    """A change this module refuses to write."""


@dataclass(frozen=True)
class Lesson:
    """One thing a confirmed run taught."""

    kind: Literal["fact", "question"]
    id: str
    """`F-...` for a fact; a question id (`[a-z0-9_]+`)."""
    value: str | list[str]
    """The fact's sentence; the question's answer."""
    source: str = ""
    """`user` for a fact; the comma-joined `B-`/`S-`/`SKILL-` ids for prose."""
    label: str = ""
    """The board label it came from (provenance only, not stored)."""


def read(path: Path | None = None) -> dict:
    p = Path(path) if path is not None else paths.ANSWER_BANK
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _dump(data: dict) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


def save_text(text: str, path: Path | None = None) -> Path:
    """Validate `text`, keep the old file in `.state/bank_history/`, then
    replace atomically with `text` as it is."""
    if unattended():
        raise BankError(f"{UNATTENDED_ENV}=1: the answer bank is not written in an unattended run")
    p = Path(path) if path is not None else paths.ANSWER_BANK
    BS.answers_from(yaml.safe_load(text), where=str(p), resume_path=BS.resume_for(p))
    if p.exists():
        history = p.parent / ".state" / "bank_history"
        history.mkdir(parents=True, exist_ok=True)
        stamp = clock.now().strftime("%Y-%m-%dT%H-%M-%S-%f")
        (history / f"{p.stem}.{stamp}.yaml").write_text(
            p.read_text(encoding="utf-8"), encoding="utf-8"
        )
        for old in sorted(history.glob(f"{p.stem}.[0-9][0-9][0-9][0-9]-*.yaml"))[:-HISTORY_KEEP]:
            old.unlink()
    atomic_write_text(p, text)
    return p


def write(data: dict, path: Path | None = None) -> Path:
    """`data` dumped, through `save_text` (comments are not kept)."""
    return save_text(_dump(data), path)


# ------------------------------------------------------------- text edits


def _block(lines: list[str], key: str) -> tuple[int, int] | None:
    """(the `key:` line, the end of its entries) for a top-level block key, or
    None. The end stops before the comments and blanks above the next key."""
    at = next((i for i, ln in enumerate(lines) if re.match(rf"{re.escape(key)}:\s*$", ln)), None)
    if at is None:
        return None
    end = at + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][:1] in (" ", "\t", "#")):
        end += 1
    while end > at + 1 and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
        end -= 1
    return at, end


def _with_entry(text: str, key: str, entry_id: str, value) -> str:
    """text with `entry_id: value` appended to the top-level block `key`."""
    body = "".join(f"  {ln}\n" for ln in _dump({entry_id: value}).splitlines())
    lines = (text if text.endswith("\n") else text + "\n").splitlines(keepends=True)
    found = _block(lines, key)
    if found is None:
        if any(ln.startswith(f"{key}:") for ln in lines):
            return ""  # a flow-style block: `_save` dumps the data instead
        return "".join(lines) + f"{key}:\n" + body
    _, end = found
    return "".join(lines[:end] + [body] + lines[end:])


def _without_entry(text: str, key: str, entry_id: str) -> str:
    """text without `entry_id:` (and the lines indented under it) in the block `key`."""
    lines = (text if text.endswith("\n") else text + "\n").splitlines(keepends=True)
    found = _block(lines, key)
    if found is None:
        return text
    at, end = found
    rx = re.compile(rf"  ['\"]?{re.escape(entry_id)}['\"]?\s*:")
    i = next((i for i in range(at + 1, end) if rx.match(lines[i])), None)
    if i is None:
        return text
    j = i + 1
    while j < end and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > 2):
        j += 1
    while j > i + 1 and not lines[j - 1].strip():
        j -= 1
    return "".join(lines[:i] + lines[j:])


def _save(text: str, edited: str, data: dict, path: Path | None) -> None:
    """Write the text edit when it parses to `data`, else `data` dumped."""
    try:
        same = yaml.safe_load(edited) == data
    except yaml.YAMLError:
        same = False
    save_text(edited if same else _dump(data), path)


# ------------------------------------------------------------- learning


def _as_value(value) -> str | list[str] | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value if str(v).strip()]
        return (items[0] if len(items) == 1 else items) or None
    return None


def _refusal(data: dict, lesson: Lesson) -> str:
    """Why `lesson` cannot go into `data`, or ""."""
    if lesson.kind == "fact":
        if not BS.FACT_ID.match(lesson.id):
            return f"{lesson.id}: a fact id matches {BS.FACT_ID.pattern}"
        if lesson.id in (data.get("facts") or {}):
            return f"{lesson.id}: already a fact; undo it first (apply learn --undo {lesson.id})"
        if not isinstance(lesson.value, str) or not lesson.value.strip():
            return f"{lesson.id}: a fact is one sentence"
        if "\n" in lesson.value.strip():
            return f"{lesson.id}: a fact is one line"
        return ""
    if lesson.kind == "question":
        if not _QID.match(lesson.id):
            return f"{lesson.id}: a question id is lowercase letters, digits and _"
        if lesson.id in (data.get("questions") or {}):
            return (
                f"{lesson.id}: already a question; undo it first (apply learn --undo {lesson.id})"
            )
        parts = [p.strip() for p in lesson.source.split(",") if p.strip()]
        if not parts or not all(p.startswith(PROSE_SOURCES) for p in parts):
            return (
                f"{lesson.id}: a saved answer is written from profile ids "
                f"({', '.join(PROSE_SOURCES)}), not {lesson.source or 'nowhere'}"
            )
        if _as_value(lesson.value) is None:
            return f"{lesson.id}: a new question needs an answer"
        return ""
    return f"{lesson.id}: unknown lesson kind {lesson.kind!r}"


def _entry(lesson: Lesson) -> tuple[str, object]:
    """(top-level block, the entry's value) a lesson adds."""
    if lesson.kind == "fact":
        return "facts", str(lesson.value).strip()
    return "questions", {"answer": _as_value(lesson.value), "source": lesson.source.strip()}


def learn(lessons: list[Lesson], path: Path | None = None) -> tuple[list[Lesson], list[str]]:
    """Write every lesson that holds into the bank. Returns (kept, refusals):
    each lesson is tried alone and kept only if the bank still loads; a
    refused one is named and the rest stand."""
    if not lessons:
        return [], []
    if unattended():
        raise BankError(f"{UNATTENDED_ENV}=1: the answer bank is not written in an unattended run")
    p = Path(path) if path is not None else paths.ANSWER_BANK
    with locked(p):
        text = p.read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
        edited = text
        kept: list[Lesson] = []
        refusals: list[str] = []
        for lesson in lessons:
            refusal = _refusal(data, lesson)
            if not refusal:
                key, value = _entry(lesson)
                trial = copy.deepcopy(data)
                trial[key] = {**(trial.get(key) or {}), lesson.id: value}
                try:
                    BS.answers_from(trial, where=str(p), resume_path=BS.resume_for(p))
                except BS.AnswersError as exc:
                    refusal = f"{lesson.id}: the bank would not load: {exc}"
            if refusal:
                refusals.append(refusal)
                continue
            data = trial
            edited = _with_entry(edited, key, lesson.id, value)
            kept.append(lesson)
        if kept:
            _save(text, edited, data, p)
    return kept, refusals


def undo(item_id: str, path: Path | None = None) -> str:
    """Remove one fact (`F-...`) or one question, under the bank's lock."""
    p = Path(path) if path is not None else paths.ANSWER_BANK
    key = "facts" if item_id.startswith("F-") else "questions"
    with locked(p):
        text = p.read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
        block = data.get(key) or {}
        if item_id not in block:
            raise BankError(f"no {key[:-1]} {item_id!r}")
        block.pop(item_id)
        if not block:
            data.pop(key, None)
        _save(text, _without_entry(text, key, item_id), data, p)
    return f"removed {key[:-1]} {item_id}"


def review(path: Path | None = None) -> str:
    """Every fact, then every question: one line each."""
    data = read(path)
    lines = [f"{fid}: {text}" for fid, text in (data.get("facts") or {}).items()]
    for qid, spec in (data.get("questions") or {}).items():
        answer = (spec or {}).get("answer")
        shown = " | ".join(map(str, answer)) if isinstance(answer, list) else str(answer)
        source = (spec or {}).get("source")
        lines.append(f"{qid}: {' '.join(shown.split())[:90]}" + (f"  ({source})" if source else ""))
    return "\n".join(lines)
