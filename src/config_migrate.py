"""`config migrate` — move a profile/ written for an older layout onto the
current one. `--dry-run` prints the diff, `--apply` writes it. Idempotent: a
migrated profile has nothing left to move.

Text edits keep a YAML file's comments; the answer bank is rewritten through
`bank.write`, which validates it and keeps the old file in `.bank_history/`.
"""

from __future__ import annotations

import difflib
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

BANK = "application_answers.yaml"
SCORING = "scoring.yaml"


@dataclass
class Profile:
    """Every file a step may change, as text, plus what the steps found."""

    root: Path
    texts: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def text(self, name: str) -> str | None:
        if name not in self.texts:
            p = self.root / name
            if not p.is_file():
                return None
            self.texts[name] = p.read_text(encoding="utf-8")
        return self.texts[name]

    def data(self, name: str) -> Any:
        text = self.text(name)
        return None if text is None else yaml.safe_load(text)

    def set_bank(self, data: dict) -> None:
        self.texts[BANK] = dump_bank(data)


def dump_bank(data: dict) -> str:
    """The text `bank.write` would write."""
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


def drop_top_key(text: str, key: str) -> str:
    """text without the top-level `key:` block and the comment lines right above it."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if re.match(rf"{re.escape(key)}\s*:", ln)), None)
    if start is None:
        return text
    end = start + 1
    while end < len(lines) and (lines[end][:1] in (" ", "\t") or not lines[end].strip()):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    while start > 0 and lines[start - 1].lstrip().startswith("#"):
        start -= 1
    if start > 0 and not lines[start - 1].strip() and end < len(lines) and not lines[end].strip():
        end += 1
    return "".join(lines[:start] + lines[end:])


# ---- steps, oldest first ----


def _languages(p: Profile) -> None:
    """scoring.yaml `languages` -> the bank's `languages_fluent` question."""
    from src.score_filter import LANGUAGES_QUESTION

    scoring = p.data(SCORING)
    if not isinstance(scoring, dict) or "languages" not in scoring:
        return
    spoken = [str(x) for x in scoring.get("languages") or []]
    bank = p.data(BANK)
    if not isinstance(bank, dict):
        p.problems.append(f"{BANK}: needed to move scoring.yaml `languages`")
        return
    questions = bank.setdefault("questions", {}) or {}
    bank["questions"] = questions
    q = questions.get(LANGUAGES_QUESTION)
    if q is None:
        if spoken:
            questions[LANGUAGES_QUESTION] = {
                "answer": spoken,
                "keywords": ["languages in which you are fluent"],
            }
            p.set_bank(bank)
            p.notes.append(f"{BANK}: questions.{LANGUAGES_QUESTION} = {spoken}")
    else:
        have = q.get("answer")
        have = have if isinstance(have, list) else [have]
        if {str(x) for x in have} != set(spoken):
            p.problems.append(
                f"{SCORING} languages {spoken} differ from {BANK} questions."
                f"{LANGUAGES_QUESTION} {have}: make the bank's answer right, then "
                f"delete `languages` from {SCORING}"
            )
            return
    p.texts[SCORING] = drop_top_key(p.text(SCORING) or "", "languages")


STEPS: tuple[Callable[[Profile], None], ...] = (_languages,)


def plan(root: Path) -> Profile:
    """Run every step on root's files in memory."""
    p = Profile(root)
    for step in STEPS:
        step(p)
    return p


def changed(p: Profile) -> dict[str, tuple[str, str]]:
    """{file: (old text, new text)} for every file a step changed."""
    out = {}
    for name, new in p.texts.items():
        old = (p.root / name).read_text(encoding="utf-8") if (p.root / name).is_file() else ""
        if new != old:
            out[name] = (old, new)
    return out


def diff(p: Profile) -> str:
    return "".join(
        "".join(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                f"a/{name}",
                f"b/{name}",
            )
        )
        for name, (old, new) in sorted(changed(p).items())
    )


def write(p: Profile) -> list[str]:
    """Write every changed file; the bank last, through bank.write."""
    from src.apply import bank as bank_writer
    from src.shared.parquet_io import locked

    done = []
    todo = changed(p)
    for name, (_, new) in sorted(todo.items()):
        if name == BANK:
            continue
        path = p.root / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(new, encoding="utf-8")
        os.replace(tmp, path)
        done.append(name)
    if BANK in todo:
        path = p.root / BANK
        with locked(path):
            bank_writer.write(yaml.safe_load(todo[BANK][1]), path)
        done.append(BANK)
    return done


def main(args: Any) -> int:
    """`config migrate`: args has .profile, .apply."""
    from src.shared import paths

    root = Path(args.profile) if args.profile else paths.PROFILE
    p = plan(root)
    for line in p.notes:
        print(f"move   {line}")
    for line in p.problems:
        print(f"ERROR  {line}")
    if p.problems:
        return 1
    todo = changed(p)
    if not todo:
        print("nothing to migrate")
        return 0
    if BANK in todo and re.search(r"^\s*#", todo[BANK][0], re.MULTILINE):
        print(
            f"note   {BANK} is rewritten without its comments; the old file stays in .bank_history/"
        )
    if not args.apply:
        print(diff(p), end="")
        print("\ndry run: nothing written; --apply writes it")
        return 0
    from src.apply.answers import AnswersError
    from src.apply.bank import BankError

    try:
        written = write(p)
    except (AnswersError, BankError, OSError) as exc:
        print(f"ERROR  {exc}")
        return 1
    for name in written:
        print(f"wrote  {root / name}")
    print("now run: uv run config-check")
    return 0
