"""The shape of each YAML file a session writes for `uv run app build`, and the
problem codes a build reports. Shape only; the grounding checks live in
resume_model and app_cli."""

from __future__ import annotations

import difflib
import typing
from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError


class ProblemCode(StrEnum):
    """Every code `app build` reports; .claude/shared/no_fab.md lists the same."""

    UNKNOWN_ID = "unknown_id"
    CONTEXT = "context"
    BUDGET = "budget"
    LAYOUT = "layout"
    LICENSE = "license"
    NUMBER = "number"
    SKILL = "skill"
    LINT = "lint"
    PAGES = "pages"
    LENGTH = "length"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Summary(_Strict):
    text: str = ""
    uses: list[str] = []


class Entry(_Strict):
    id: str
    text: str | None = None


class SkillPick(_Strict):
    id: str
    alias: str | None = None


def _bare_id(v):
    return {"id": v} if isinstance(v, str) else v


Pick = Annotated[SkillPick, BeforeValidator(_bare_id, json_schema_input_type=str | SkillPick)]


class ResumePlan(_Strict):
    summary: Summary | None = None
    entries: list[Entry] = []
    skills: dict[str, list[Pick]] = {}


class LetterPlan(_Strict):
    salutation: str
    uses: list[str] = []
    body: list[str]


class CompanyAnswers(_Strict):
    researched_at: date | str
    sources: list[str]
    uses: list[str]
    why_company: str | None
    why_role: str | None
    what_interests_you_about_product: str | None


class OutreachDraft(_Strict):
    channel: Literal["recruiter", "referral", "alumni"]
    medium: Literal["linkedin", "email"]
    to: str | None = None
    contact_id: str | None = None
    subject: str | None = None
    body: str
    why: str
    uses: list[str] = []


KINDS: dict[str, tuple[str, type[BaseModel]]] = {
    "resume": ("resume_plan.yaml", ResumePlan),
    "letter": ("cover_letter.yaml", LetterPlan),
    "answers": ("company_answers.yaml", CompanyAnswers),
    "outreach": ("outreach.yaml", OutreachDraft),
}


def _inner(ann) -> type[BaseModel] | None:
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return ann
    for a in typing.get_args(ann):
        m = _inner(a)
        if m is not None:
            return m
    return None


def _fields_at(model: type[BaseModel], loc: tuple) -> list[str]:
    m: type[BaseModel] | None = model
    for part in loc[:-1]:
        if m is not None and isinstance(part, str) and part in m.model_fields:
            m = _inner(m.model_fields[part].annotation)
    return list(m.model_fields) if m else []


def _path(loc: tuple) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out


def problems(kind: str, data: dict) -> list[dict]:
    """[] when `data` has the shape of `kind`'s file, else one layout problem
    per bad key: file, key path, and the closest valid key for an unknown one."""
    fname, model = KINDS[kind]
    try:
        model.model_validate(data)
    except ValidationError as e:
        out = []
        for err in e.errors():
            loc = err["loc"]
            if err["type"] == "extra_forbidden":
                close = difflib.get_close_matches(str(loc[-1]), _fields_at(model, loc), n=1)
                detail = "unknown key" + (f"; closest valid key: {close[0]!r}" if close else "")
            elif err["type"] == "missing":
                detail = "missing key"
            else:
                detail = err["msg"]
            out.append(
                {"where": f"{fname}: {_path(loc)}", "code": ProblemCode.LAYOUT, "detail": detail}
            )
        return out
    return []
