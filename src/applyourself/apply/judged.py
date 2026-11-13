"""Render one judged answer (an override with a `decision`) into a Resolution.

The judge picks; this module only checks the pick is well-formed and turns it
into the board's value. It runs only behind `plan.apply_override`, the gate
`build_plan` and the eval grader share.
"""

from __future__ import annotations

from pathlib import Path

from applyourself.apply.answers import (
    Answers,
    Resolution,
    answer_question,
    resolve_fact_wording,
)
from applyourself.apply.reconcile import MergedField, MergedOption
from applyourself.shared import paths

DECISIONS = ("question", "fact", "option", "text", "ask", "skip")

# Basis words that name no profile entry.
BASIS_WORDS = ("jd", "company_answers", "option")

# Work-authorization keys a basis may cite, bare or as `work_authorization.<key>`.
WORK_AUTH_KEYS = (
    "us_person",
    "nationalities",
    "authorized",
    "requires_sponsorship",
    "unrestricted",
    "status_label",
)

# What a single checkbox's judged value may say to tick it, besides its own label.
CHECKBOX_YES = ("yes", "i acknowledge", "i agree", "i confirm")

# A basis citing one `demographics.answers` topic: `demographics.<topic>`.
DEMOGRAPHICS_PREFIX = "demographics."

# Profile entry id prefixes, longest first so `SKILL-` is not read as `S-`.
_ENTRY_PREFIXES = ("SKILL-", "B-", "S-")


def is_work_auth_basis(item: str) -> bool:
    """A basis naming a work-authorization key of the bank."""
    return item.removeprefix("work_authorization.") in WORK_AUTH_KEYS


def _checkbox_word(text: str) -> str:
    return " ".join(text.split()).casefold().rstrip(".!")


def checkbox_yes(field: MergedField, value) -> bool:
    """A judged value that ticks a single checkbox: one affirmative, or the
    checkbox's own label (or its one option)."""
    values = [value] if isinstance(value, str) else list(value or ())
    if len(values) != 1 or not isinstance(values[0], str):
        return False
    own = {_checkbox_word(field.label)}
    if len(field.options) == 1:
        own.add(_checkbox_word(field.options[0].label))
    word = _checkbox_word(values[0])
    return bool(word) and (word in CHECKBOX_YES or word in own)


def _park(reason: str, tier: str, source: str) -> Resolution:
    return Resolution("park", tier=tier, reason=reason, source=source)


def _entry_ids(path: Path) -> set[str]:
    """The `## ID` entries of one profile source file; none when it is absent."""
    from applyourself.resume_model import parse_entries

    if not path.is_file():
        return set()
    return set(parse_entries(path.read_text(encoding="utf-8"), path.name))


def _profile_ids() -> set[str]:
    """Every bullet, story and skill id the profile defines."""
    ids: set[str] = set()
    for path in (paths.BULLETS, paths.SKILLS, paths.STORIES):
        ids |= _entry_ids(path)
    return ids


def missing_basis(
    basis: tuple[str, ...], answers: Answers, known_ids: set[str] | None = None
) -> list[str]:
    """Basis ids that name nothing: an `F-` id not in the bank's facts, a
    `B-`/`S-`/`SKILL-` id not in the profile (or in `known_ids` when given),
    a `demographics.<topic>` the bank's demographics do not answer,
    or a word that is neither a basis word, a work-authorization key nor a
    bank question id."""
    missing: list[str] = []
    profile: set[str] | None = known_ids
    for item in basis:
        if item in BASIS_WORDS or is_work_auth_basis(item) or answers.question(item) is not None:
            continue
        if item.startswith(DEMOGRAPHICS_PREFIX):
            if item.removeprefix(DEMOGRAPHICS_PREFIX) not in answers.demographic_answers:
                missing.append(item)
            continue
        if item.startswith("F-"):
            if item not in answers.facts:
                missing.append(item)
            continue
        if item.startswith(_ENTRY_PREFIXES):
            if profile is None:
                profile = _profile_ids()
            if item not in profile:
                missing.append(item)
            continue
        missing.append(item)
    return missing


def render(
    field: MergedField,
    entry,
    answers: Answers,
    company: str = "",
    tier: str = "C",
    known_ids: set[str] | None = None,
) -> Resolution:
    """The value `entry` (a `plan.Override` with a `decision`) gives `field`.

    `tier` is the tier the code's own resolution reached; a judged option,
    text, ask or skip keeps it. `known_ids`: the profile ids a basis may cite,
    in place of the real profile's (an eval persona's)."""
    decision = entry.decision
    if decision == "question":
        question = answers.question(entry.question)
        if question is None:
            return _park(
                f"judged: no bank question {entry.question!r}", tier, f"judged:{entry.question}"
            )
        return answer_question(question, field, answers, f"bank:{question.id}")
    if decision == "fact":
        return resolve_fact_wording(field, answers, entry.fact)
    if decision == "ask":
        return _park(f"ask: {entry.ask}", tier, "judged:ask")
    if decision == "skip":
        if field.required:
            return _park("judged skip on a required field", tier, "judged:skip")
        return Resolution("skip", tier=tier, reason="judged: skip", source="judged:skip")
    if decision in ("option", "text"):
        from applyourself.apply.plan import _override_resolution

        source = entry.source or (entry.basis[0] if entry.basis else "")
        if not entry.basis:
            return _park(f"judged {decision}: no basis", tier, f"override:{source}")
        missing = missing_basis(tuple(entry.basis), answers, known_ids)
        if missing:
            return _park(
                f"judged {decision}: basis {missing} not found", tier, f"override:{source}"
            )
        value = entry.value
        if field.kind == "checkbox" and checkbox_yes(field, value):
            value = "Yes"
        return _override_resolution(field, value, source, tier, company)
    return _park(f"judged: unknown decision {decision!r}", tier, "judged")


def field_from_dict(d: dict) -> MergedField:
    """A `MergedField` from a judge field dict (`{id, label, kind, options,
    description, required, multi}`), for rendering outside a live form."""
    fid = str(d.get("id") or "")
    return MergedField(
        id=fid,
        name=fid.removesuffix("[]"),
        label=str(d.get("label") or ""),
        required=bool(d.get("required")),
        kind=str(d.get("kind") or "text"),
        section=str(d.get("section") or "questions"),
        multi=bool(d.get("multi")),
        options=tuple(MergedOption(label=str(o)) for o in d.get("options") or ()),
        description=str(d.get("description") or ""),
    )
