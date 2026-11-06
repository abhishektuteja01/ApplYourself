"""One pydantic model per profile/*.yaml file. Each names every valid key
(extra="forbid"), so a typo'd key fails loud with the closest valid key. The
loaders still check most values themselves (R7: no LLM)."""

from __future__ import annotations

import difflib
import types
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Union, get_args, get_origin

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from applyourself.shared import schemas


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retired: ClassVar[dict[str, str]] = {}
    """Removed key -> where it moved, named in the error."""


# ---- verticals.yaml ----


class LaneResume(_Strict):
    sections: Any = None
    bullets: Any = None
    skill_lines: Any = None
    summary: Any = None


class Focus(_Strict):
    question: Any = None
    yes_min: Any = None
    no_max: Any = None


class Lane(_Strict):
    retired: ClassVar[dict[str, str]] = {
        "salary_expectation": "application_answers.yaml work_authorization.countries.<country>.salary"
    }
    display_name: Any = None
    search_terms: Any = None
    linkedin_terms: Any = None
    abroad_terms: Any = None
    drop_phrases: Any = None
    years: Any = None
    skill_weights: Any = None
    focus: Focus | None = None
    title_exclude_terms: Any = None
    title_include_terms: Any = None
    title_strong_keep_terms: Any = None
    resume: LaneResume | None = None


class RulePattern(_Strict):
    match: Any = None
    require_any: Any = None


class ClassifierRule(_Strict):
    vertical: Any = None
    pattern: str | RulePattern | None = None


class VerticalsFile(_Strict):
    retired: ClassVar[dict[str, str]] = {
        "verticals": "verticals/<lane>/lane.yaml, one file per lane (uv run config migrate)"
    }
    schema_version: Any = None
    default_vertical: Any = None
    lanes: Any = None
    defaults: Lane | None = None
    classifier_rules: list[ClassifierRule] | None = None


# ---- scoring.yaml ----

Score = Annotated[int, Field(strict=True, ge=0)]


class Cutoffs(_Strict):
    """Fit-score cutoffs: review and tailor (suggested_action), keep_long
    (discovery keeps a role high_days), sheet (the night's status sheet)."""

    review: Score
    tailor: Score
    keep_long: Score
    sheet: Score

    @model_validator(mode="after")
    def _rising(self) -> Cutoffs:
        if not self.review <= self.tailor <= self.keep_long <= self.sheet:
            raise ValueError("must rise: review <= tailor <= keep_long <= sheet")
        return self


class JdQuality(_Strict):
    min_words: Any = None
    skills_cap: Any = None


class TrimJd(_Strict):
    headings: Any = None
    keep_words: Any = None


class Shortlist(_Strict):
    top_n: Any = None
    pipeline_states: Any = None
    pipeline_show: Any = None
    sponsorship_order: Any = None
    note: Any = None


class ScoringFile(_Strict):
    retired: ClassVar[dict[str, str]] = {
        "action": "cutoffs.review and cutoffs.tailor",
        "languages": "application_answers.yaml questions.languages_fluent",
    }
    schema_version: Any = None
    ineligible: Any = None
    no_sponsorship: Any = None
    keywords_max: Any = None
    years_margin: Any = None
    jd_quality: JdQuality | None = None
    trim_jd: TrimJd | None = None
    shortlist: Shortlist | None = None
    cutoffs: Cutoffs


# ---- settings.yaml ----


class OutreachWordLimits(_Strict):
    recruiter_linkedin: int
    recruiter_email: int
    referral: int
    alumni: int
    subject: int


class Browser(_Strict):
    driver: Any = None
    stealth: Any = None


class Settings(_Strict):
    retired: ClassVar[dict[str, str]] = {"score_threshold": "scoring.yaml cutoffs.sheet"}
    schema_version: int | None = None
    table_path: str | None = None
    tailor_parallel: int | None = None
    tailor_stagger_seconds: int | None = None
    retries: int | None = None
    max_budget_usd_per_call: float | None = None
    max_spend_usd_per_night: float | None = None
    auto_add_synonyms: bool | None = None
    unlock_max_open: int | None = None
    summary_email: str | None = None
    tailor_max_per_night: int | None = None
    apply_prep_max_per_night: int | None = None
    ghosted_after_days: int | None = None
    company_cooldown_days: int | None = None
    outreach_word_limits: OutreachWordLimits | None = None
    browser: Browser | None = None


# ---- contacts.yaml ----


class Contact(_Strict):
    id: Any = None
    name: Any = None
    company: Any = None
    role: Any = None
    relationship: Literal["recruiter", "referral", "alumni", "other"] | None = None
    channel: Any = None
    email: Any = None
    phone: Any = None
    linkedin: Any = None
    shared_affiliation: Any = None
    tags: Any = None
    notes: Any = None


class ContactsFile(_Strict):
    schema_version: Any = None
    contacts: list[Contact] | None = None


# ---- de_ai_rules.yaml ----


class LintRule(_Strict):
    label: str
    phrases: list[str] = Field(min_length=1)
    match: Literal["word", "sentence_start", "regex"]
    scope: list[Literal["resume", "outreach"]] = Field(min_length=1)
    level: Literal["block", "warn"]
    unless_before: Literal["number"] | None = None


class DeAiRules(_Strict):
    retired: ClassVar[dict[str, str]] = {
        "banned_phrases": "`rules:` (schema_version 2; see data/defaults/de_ai_rules.yaml)",
        "outreach_only": "`rules:` entries with `scope: [outreach]`",
    }
    schema_version: Literal[2]
    mechanical: dict[str, str]
    rules: list[LintRule]
    no_emoji: bool = False
    no_exclamation: bool = False
    stopwords: str = ""


# ---- validation ----


def _model_at(model: type[BaseModel], loc: tuple[Any, ...]) -> type[BaseModel] | None:
    """The model whose fields hold loc's last key, following pydantic's loc
    (dict keys, list indexes and union member names)."""
    t: Any = model
    for seg in loc[:-1]:
        while True:
            origin = get_origin(t)
            if origin in (Union, types.UnionType):
                members = [a for a in get_args(t) if a is not type(None)]
                if len(members) == 1:
                    t = members[0]
                    continue
                named = [a for a in members if getattr(a, "__name__", None) == seg]
                if named:
                    t = named[0]
                    break
                return None
            if isinstance(t, type) and issubclass(t, BaseModel):
                field = t.model_fields.get(seg)
                if field is None:
                    return None
                t = field.annotation
            elif origin is dict:
                t = get_args(t)[1]
            elif origin is list:
                t = get_args(t)[0]
            else:
                return None
            break
    while get_origin(t) in (Union, types.UnionType):
        members = [a for a in get_args(t) if a is not type(None)]
        if len(members) != 1:
            break
        t = members[0]
    return t if isinstance(t, type) and issubclass(t, BaseModel) else None


def _where(model: type[BaseModel], loc: tuple[Any, ...]) -> str:
    # Drop union member names pydantic adds to loc.
    names = {a.__name__ for a in _models(model)} | {"str"}
    return ".".join(
        f"[{s}]" if isinstance(s, int) else str(s) for s in loc if s not in names
    ).replace(".[", "[")


def _models(model: type[BaseModel], seen: set[Any] | None = None) -> set[Any]:
    seen = seen if seen is not None else set()
    if model in seen:
        return seen
    seen.add(model)

    def walk(t: Any) -> None:
        for a in get_args(t) or ():
            walk(a)
        if isinstance(t, type) and issubclass(t, BaseModel):
            _models(t, seen)

    for f in model.model_fields.values():
        walk(f.annotation)
    return seen


def validate[M: BaseModel](model: type[M], data: Any, path: Path | str) -> M:
    """data as `model`, or ValueError naming every problem as `path: key: message`.
    An unknown key names the closest valid key, or where a retired key moved."""
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        errors = exc.errors()
    extra = [e for e in errors if e["type"] == "extra_forbidden"]
    # A union's other member fails where an unknown key sits; that error is noise.
    beside = {_where(model, tuple(e["loc"])[:-1]) for e in extra}
    lines = []
    for e in errors:
        loc = tuple(e["loc"])
        if e["type"] != "extra_forbidden" and _where(model, loc) in beside:
            continue
        if e["type"] == "extra_forbidden":
            owner = _model_at(model, loc)
            key = str(loc[-1])
            valid = sorted(owner.model_fields) if owner else []
            where = _where(model, loc[:-1]) or "top level"
            retired = getattr(owner, "retired", {})
            if key in retired:
                hint = f" (moved to {retired[key]})"
            else:
                close = difflib.get_close_matches(key, valid, n=1)
                hint = f" (did you mean {close[0]!r}?)" if close else ""
            lines.append(
                f"{path}: {where}: unknown key {key!r}{hint}; valid keys: {', '.join(valid)}"
            )
        else:
            where = _where(model, loc) or "top level"
            lines.append(f"{path}: {where}: {e['msg']}")
    raise ValueError("\n".join(dict.fromkeys(lines)))


def moved_hint(path: Path) -> ValueError | None:
    """A "moved to" error when path is missing but the old automation.yaml is there."""
    old = path.with_name("automation.yaml")
    if path.is_file() or not old.is_file():
        return None
    return ValueError(f"{old} moved to {path}: run `uv run config migrate --dry-run`, then --apply")


def load_settings(path: Path) -> dict[str, Any]:
    """profile/settings.yaml as a dict, every key checked."""
    if (exc := moved_hint(path)) is not None:
        raise exc
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    validate(Settings, data, path)
    schemas.check(data.get("schema_version"), schemas.SETTINGS_SCHEMA_VERSION, path)
    return data


def settings_data() -> dict[str, Any]:
    """profile/settings.yaml as a dict, or {} when the file is absent."""
    from applyourself.shared import paths

    path = paths.SETTINGS
    if (exc := moved_hint(path)) is not None:
        raise exc
    return load_settings(path) if path.is_file() else {}


def setting(key: str, default: Any, data: dict[str, Any] | None = None) -> Any:
    """One profile/settings.yaml value (from `data` when given, already loaded),
    or default when the file or key is absent."""
    value = (settings_data() if data is None else data).get(key)
    return default if value is None else value
