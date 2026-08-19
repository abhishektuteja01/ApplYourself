"""Greenhouse application-question schema: fetch and normalize.

Enrichment only. The rendered DOM is the source of truth for what must be
filled; this supplies clean labels, required flags and select option lists.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.discovery.sources.ats.http import fetch_json

QUESTIONS_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}?questions=true"

# Open set: a type not listed here raises rather than being silently dropped.
FIELD_TYPES = frozenset({
    "input_text",
    "input_file",
    "input_hidden",
    "textarea",
    "multi_value_single_select",
    "multi_value_multi_select",
})

# Sources for the flat question list. `compliance` nests its questions inside
# groups and is handled separately; `demographic_questions` is a different
# object shape entirely and never lands here.
_FLAT_SOURCES = ("questions", "location_questions")


class SchemaError(Exception):
    """The board returned a payload this module cannot represent."""


@dataclass(frozen=True)
class Option:
    label: str
    value: object


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    options: tuple[Option, ...] = ()
    multi: bool = False


@dataclass(frozen=True)
class Question:
    label: str
    required: bool
    fields: tuple[Field, ...]
    source: str
    description: str | None = None

    @property
    def satisfy(self) -> str:
        """A question with several fields is satisfied by any one of them:
        Resume/CV offers resume (input_file) and resume_text (textarea)."""
        return "any"


@dataclass(frozen=True)
class DemographicOption:
    id: int | None
    label: str
    free_form: bool = False
    decline_to_answer: bool = False


@dataclass(frozen=True)
class DemographicQuestion:
    id: int | None
    label: str
    required: bool
    type: str
    options: tuple[DemographicOption, ...]


@dataclass(frozen=True)
class BoardSchema:
    questions: tuple[Question, ...]
    demographic: tuple[DemographicQuestion, ...]
    education: str | None
    employment: str | None
    company_name: str
    title: str


def _parse_option(raw) -> Option:
    if not isinstance(raw, dict):
        raise SchemaError(f"option is not an object: {raw!r}")
    return Option(label=str(raw.get("label") or ""), value=raw.get("value"))


def _parse_field(raw) -> Field:
    if not isinstance(raw, dict):
        raise SchemaError(f"field is not an object: {raw!r}")
    name = str(raw.get("name") or "")
    ftype = str(raw.get("type") or "")
    if not name:
        raise SchemaError(f"field has no name: {raw!r}")
    if ftype not in FIELD_TYPES:
        raise SchemaError(f"unknown field type {ftype!r} on field {name!r}")
    # multi_value_multi_select names arrive as "question_123[]".
    multi = name.endswith("[]")
    if multi:
        name = name[:-2]
    return Field(
        name=name,
        type=ftype,
        options=tuple(_parse_option(v) for v in raw.get("values") or []),
        multi=multi,
    )


def _parse_question(raw, source: str) -> Question:
    if not isinstance(raw, dict):
        raise SchemaError(f"question is not an object: {raw!r}")
    fields = tuple(_parse_field(f) for f in raw.get("fields") or [])
    if not fields:
        raise SchemaError(f"question has no fields: {raw.get('label')!r}")
    return Question(
        label=str(raw.get("label") or ""),
        required=bool(raw.get("required")),
        fields=fields,
        source=source,
        description=raw.get("description"),
    )


def _parse_demographic(raw) -> DemographicQuestion:
    if not isinstance(raw, dict):
        raise SchemaError(f"demographic question is not an object: {raw!r}")
    dtype = str(raw.get("type") or "")
    if dtype not in FIELD_TYPES:
        raise SchemaError(f"unknown demographic question type {dtype!r}")
    options = []
    for o in raw.get("answer_options") or []:
        if not isinstance(o, dict):
            raise SchemaError(f"answer_option is not an object: {o!r}")
        options.append(DemographicOption(
            id=o.get("id"),
            label=str(o.get("label") or ""),
            free_form=bool(o.get("free_form")),
            decline_to_answer=bool(o.get("decline_to_answer")),
        ))
    return DemographicQuestion(
        id=raw.get("id"),
        label=str(raw.get("label") or ""),
        required=bool(raw.get("required")),
        type=dtype,
        options=tuple(options),
    )


def parse_schema(payload) -> BoardSchema:
    """Normalize a ?questions=true payload. Pure — no network."""
    if not isinstance(payload, dict):
        raise SchemaError(f"payload is not an object: {type(payload).__name__}")

    questions: list[Question] = []
    for source in _FLAT_SOURCES:
        raw = payload.get(source)
        if raw is None:
            continue
        if not isinstance(raw, list):
            raise SchemaError(f"{source} is not a list: {type(raw).__name__}")
        questions.extend(_parse_question(q, source) for q in raw)

    # compliance is a list of groups, each holding its own questions.
    compliance = payload.get("compliance")
    if compliance is not None:
        if not isinstance(compliance, list):
            raise SchemaError(f"compliance is not a list: {type(compliance).__name__}")
        for group in compliance:
            if not isinstance(group, dict):
                raise SchemaError(f"compliance group is not an object: {group!r}")
            gtype = str(group.get("type") or "unknown")
            questions.extend(
                _parse_question(q, f"compliance:{gtype}")
                for q in group.get("questions") or []
            )

    demographic: list[DemographicQuestion] = []
    dq = payload.get("demographic_questions")
    if dq is not None:
        if not isinstance(dq, dict):
            raise SchemaError(f"demographic_questions is not an object: {type(dq).__name__}")
        demographic.extend(_parse_demographic(q) for q in dq.get("questions") or [])

    return BoardSchema(
        questions=tuple(questions),
        demographic=tuple(demographic),
        education=payload.get("education"),
        employment=payload.get("employment"),
        company_name=str(payload.get("company_name") or ""),
        title=str(payload.get("title") or ""),
    )


def fetch_questions(board_slug: str, job_id: str | int, timeout: int = 30) -> BoardSchema:
    """GET the board's question schema. Raises CareersError on fetch failure."""
    url = QUESTIONS_URL.format(slug=board_slug, job_id=job_id)
    return parse_schema(fetch_json(url, timeout=timeout))
