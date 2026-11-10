"""The /apply eval case: one form field, the persona it is answered for, and
what a correct answer does. Public cases and personas live in `evals/apply/`
(tracked, synthetic); the private set in `applications/evals/apply/private/`."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from applyourself.apply.bank_schema import Answers, AnswersError, answers_from
from applyourself.shared import paths

PUBLIC_DIR = paths.REPO_ROOT / "evals" / "apply"
PERSONAS_DIR = PUBLIC_DIR / "personas"
CASES_DIR = PUBLIC_DIR / "cases"


def private_dir() -> Path:
    return paths.APPLICATIONS / "evals" / "apply" / "private"


GROUPS = (
    "rewording",
    "flip_word",
    "double_negation",
    "polarity",
    "country_in_label",
    "wrong_country",
    "scope",
    "threshold",
    "range_dropdown",
    "sentence_option",
    "consent",
    "company_history",
    "money_units",
    "junk",
    "injection",
    "must_ask",
    "multi_select",
    "checkbox_ack",
    "prose",
    "prose_reuse",
    "persona_swap",
    "work_auth_corpus",
    "export",
    "private_bank",
    "private_override",
)
#: Groups that must pass every case.
GATE_GROUPS = (
    "work_auth_corpus",
    "flip_word",
    "double_negation",
    "sentence_option",
    "export",
    "consent",
    "must_ask",
    "injection",
    "wrong_country",
    "country_in_label",
)
#: The persona name private cases use; the runner points it at the real bank.
REAL_PERSONA = "real"

Group = Literal[
    "rewording",
    "flip_word",
    "double_negation",
    "polarity",
    "country_in_label",
    "wrong_country",
    "scope",
    "threshold",
    "range_dropdown",
    "sentence_option",
    "consent",
    "company_history",
    "money_units",
    "junk",
    "injection",
    "must_ask",
    "multi_select",
    "checkbox_ack",
    "prose",
    "prose_reuse",
    "persona_swap",
    "work_auth_corpus",
    "export",
    "private_bank",
    "private_override",
]

_STRICT = ConfigDict(extra="forbid", strict=True)


class CaseError(Exception):
    """A case or persona file cannot be loaded."""


class Field(BaseModel):
    """The judge's view of one form field (contract C2); `id` defaults to the case id."""

    model_config = _STRICT

    id: str = ""
    label: str
    kind: str
    options: list[str] = []
    description: str = ""
    required: bool = True
    multi: bool = False


class FactExpect(BaseModel):
    model_config = _STRICT

    fact: Literal["authorized", "requires_sponsorship", "unrestricted", "us_person"]
    yes_when: bool


_EXPECTATIONS = ("value", "one_of", "question", "fact", "prose")


class Expect(BaseModel):
    """`fill` takes exactly one of value / one_of / question / fact / prose;
    `ask` and `skip` take none. `or_ask` marks a fill case where stopping for
    the user is also right (two fair readings); a `one_of` entry may be a list
    for a multi-select."""

    model_config = _STRICT

    action: Literal["fill", "ask", "skip"]
    value: str | list[str] | None = None
    one_of: list[str | list[str]] | None = None
    or_ask: bool = False
    question: str | None = None
    fact: FactExpect | None = None
    prose: Literal[True] | None = None

    @model_validator(mode="after")
    def _one_expectation(self) -> Expect:
        given = [k for k in _EXPECTATIONS if getattr(self, k) is not None]
        if self.action == "fill" and len(given) != 1:
            raise ValueError(f"fill takes exactly one of {list(_EXPECTATIONS)}, got {given}")
        if self.action != "fill" and given:
            raise ValueError(f"{self.action} takes no expectation, got {given}")
        if self.one_of is not None and not self.one_of:
            raise ValueError("one_of must not be empty")
        if self.or_ask and self.action != "fill":
            raise ValueError("or_ask is only for a fill case")
        return self


class Case(BaseModel):
    model_config = _STRICT

    id: str
    group: Group
    persona: str
    job_country: str
    field: Field
    expect: Expect

    @model_validator(mode="after")
    def _field_id(self) -> Case:
        if not self.field.id:
            self.field.id = self.id
        return self


def load_cases(path: Path) -> list[Case]:
    """Every case in one jsonl file. Fails naming the line on a bad case or a repeated id."""
    path = Path(path)
    cases: list[Case] = []
    seen: set[str] = set()
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            case = Case.model_validate_json(line)
        except ValidationError as exc:
            raise CaseError(f"{path}:{n}: {exc}") from exc
        if case.id in seen:
            raise CaseError(f"{path}:{n}: case id {case.id!r} repeats")
        seen.add(case.id)
        cases.append(case)
    return cases


def dump_case(case: Case) -> str:
    """One jsonl line, without the defaults left unset."""
    return json.dumps(case.model_dump(exclude_none=True), ensure_ascii=False)


def load_persona(path: Path) -> Answers:
    """A persona bank. It never reads resume.yaml: every identity, education and
    employment key comes from the persona file itself."""
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CaseError(f"{path}: {exc}") from exc
    try:
        return answers_from(data, where=str(path), resume_path=Path(os.devnull))
    except AnswersError as exc:
        raise CaseError(str(exc)) from exc


def persona_path(name: str) -> Path:
    return PERSONAS_DIR / f"{name}.yaml"


def persona_ids(name: str) -> set[str]:
    """The fake bullet and story ids a persona's prose cases may cite."""
    sidecar = PERSONAS_DIR / f"{name}.ids.txt"
    if not sidecar.is_file():
        return set()
    lines = sidecar.read_text(encoding="utf-8").splitlines()
    return {s.strip() for s in lines if s.strip() and not s.startswith("#")}
