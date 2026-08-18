"""Settle the disagreement between the rendered form and the question API.

The two do not describe the same set of fields, in either direction. Measured
over 60 live boards:

- rendered but never declared: `country` (60/60), the EEOC `hispanic_ethnicity`
  (22/60), and the whole education and employment blocks
- declared but never rendered: `latitude`, `longitude`, `race`, and the
  `resume_text` / `cover_letter_text` twins of the two file inputs
- rendered under a different name: API `location` -> DOM `candidate-location`

So the DOM decides what exists and what is required; the API contributes the
clean label and the option list for selects. A field the DOM renders is always
planned, whether the API declared it or not — that is the regression this module
exists to prevent.

Pure function over the two parsed halves: no browser, no network.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.apply.domscan import FormScan
from src.apply.schema import BoardSchema

# The only name mismatch observed. Keyed on the DOM id.
DOM_TO_API_ALIASES = {"candidate-location": "location"}

# Marks a merged field whose enrichment came from `demographic_questions`,
# which is a different object shape and joins on id rather than name.
DEMOGRAPHIC_SOURCE = "demographic_questions"


class ReconcileError(Exception):
    """The two halves cannot be joined unambiguously."""


@dataclass(frozen=True)
class MergedOption:
    label: str
    value: object = None
    free_form: bool = False
    decline_to_answer: bool = False


@dataclass(frozen=True)
class MergedField:
    id: str                 # the selector, verbatim, "[]" included
    name: str               # "[]" stripped — the API join key
    label: str
    required: bool          # DOM
    kind: str               # DOM
    section: str            # DOM
    multi: bool             # DOM
    options: tuple[MergedOption, ...] = ()
    api_source: str | None = None   # question source, or DEMOGRAPHIC_SOURCE
    api_type: str | None = None
    description: str = ""
    """Instructional text a board renders under the label but declares nowhere
    in its own schema/API — e.g. Ashby's sibling description div (§12a). Empty
    for every board/field that has none; Greenhouse and Lever never set it."""

    @property
    def dom_only(self) -> bool:
        """No API counterpart, so no label, no required flag and no option list
        beyond what the rendered form itself carries."""
        return self.api_source is None


@dataclass(frozen=True)
class Reconciled:
    fields: tuple[MergedField, ...]
    api_only: tuple[str, ...]
    """Declared by the API and rendered nowhere. Diagnostic only — nothing can
    fill a field that does not exist. Unrendered demographic questions appear
    here as "demographic_questions:<id>"."""

    def by_id(self, field_id: str) -> MergedField | None:
        for f in self.fields:
            if f.id == field_id:
                return f
        return None

    def by_section(self, section: str) -> tuple[MergedField, ...]:
        return tuple(f for f in self.fields if f.section == section)

    @property
    def required_ids(self) -> tuple[str, ...]:
        return tuple(f.id for f in self.fields if f.required)

    @property
    def dom_only_ids(self) -> tuple[str, ...]:
        return tuple(f.id for f in self.fields if f.dom_only)


def _label(text: str) -> str:
    return " ".join((text or "").split())


def _index_api(schema: BoardSchema):
    """field name -> (question index, question, field). Raises on a duplicate:
    last-wins would silently enrich a select with another question's options."""
    index = {}
    for qi, q in enumerate(schema.questions):
        for field in q.fields:
            if field.name in index:
                raise ReconcileError(f"duplicate API field name {field.name!r}")
            index[field.name] = (qi, q, field)
    return index


def _index_demographic(schema: BoardSchema):
    index = {}
    for question in schema.demographic:
        if question.id is None:
            continue
        key = str(question.id)
        if key in index:
            raise ReconcileError(f"duplicate demographic question id {key!r}")
        index[key] = question
    return index


def reconcile(scan: FormScan, schema: BoardSchema) -> Reconciled:
    """Merge a scanned form with its question schema, DOM-authoritative."""
    api_index = _index_api(schema)
    demo_index = _index_demographic(schema)

    fields: list[MergedField] = []
    seen_ids: set[str] = set()
    reached_questions: set[int] = set()
    joined_demographic: set[str] = set()

    for dom in scan.fields:
        if dom.id in seen_ids:
            raise ReconcileError(f"duplicate DOM field id {dom.id!r}")
        seen_ids.add(dom.id)

        label, options, api_source, api_type = dom.label, (), None, None

        if dom.section == "demographic":
            question = demo_index.get(dom.id)
            if question is not None:
                joined_demographic.add(dom.id)
                label = question.label or dom.label
                api_source, api_type = DEMOGRAPHIC_SOURCE, question.type
                options = tuple(
                    MergedOption(
                        label=o.label,
                        value=o.id,
                        free_form=o.free_form,
                        decline_to_answer=o.decline_to_answer,
                    )
                    for o in question.options
                )
        else:
            hit = api_index.get(DOM_TO_API_ALIASES.get(dom.id, dom.name))
            if hit is not None:
                qi, question, api_field = hit
                reached_questions.add(qi)
                label = question.label or dom.label
                api_source, api_type = question.source, api_field.type
                options = tuple(
                    MergedOption(label=o.label, value=o.value) for o in api_field.options
                )

        # The checkbox group renders every option with its own visible label, so
        # the DOM is the better source and works even with no API match at all.
        if dom.kind == "checkbox_group" and dom.options:
            options = tuple(MergedOption(label=o.label, value=o.value) for o in dom.options)

        # The two halves state cardinality independently — the API by type, the
        # DOM by a class on an ancestor div. They agreed on all 29 multi-select
        # questions seen live. A disagreement means one of them is being read
        # wrong, and guessing either way submits a wrong answer: too few options
        # on a "mark all that apply", or a second click that clears the first.
        if api_type is not None and dom.multi is not (api_type == "multi_value_multi_select"):
            raise ReconcileError(
                f"{dom.id!r}: DOM says multi={dom.multi}, API type is {api_type!r}"
            )

        fields.append(MergedField(
            id=dom.id,
            name=dom.name,
            label=_label(label),
            required=dom.required,
            kind=dom.kind,
            section=dom.section,
            multi=dom.multi,
            options=options,
            api_source=api_source,
            api_type=api_type,
        ))

    # A question counts as reached when ANY of its fields matched a rendered
    # control: Resume/CV declares `resume` and `resume_text`, and satisfying
    # either satisfies the question.
    api_only = [
        field.name
        for qi, question in enumerate(schema.questions)
        if qi not in reached_questions
        for field in question.fields
    ]
    api_only.extend(
        f"{DEMOGRAPHIC_SOURCE}:{question.id}"
        for question in schema.demographic
        if str(question.id) not in joined_demographic
    )

    return Reconciled(fields=tuple(fields), api_only=tuple(api_only))
