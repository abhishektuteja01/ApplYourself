"""Turn a reconciled form plus the answer config into an ordered fill plan.

The plan is the whole contract with the browser: fill.py executes it and never
decides anything. So everything a submission depends on is settled here, offline
and inspectable — `uv run apply plan <job_id>` prints exactly what would be
typed into which selector before any browser exists.

Three outcomes per field, and one list each:

    fields[]    a value to type, select or check
    files[]     an artifact from the role's /tailor dir to attach
    unmapped[]  nothing could answer it -> the role parks, and submit() refuses

Two more lists, neither of which blocks a submission:

    draftable[] an optional question the bank does not answer, of a kind a
                person would actually answer — /apply may draft it, and leaving it blank
                stays valid. Without this an optional "why do you want to work
                here" would go out empty and nothing would say so.
    skipped[]   an optional field left alone on purpose.

A required field can never land in either — if one does, that is a bug in
answers.py and this module raises rather than submitting the form short.

Two file-format facts, both measured over the 120 existing /tailor dirs: PDF is
present in 116 and DOCX in 109, so PDF is preferred and DOCX is the fallback;
and Word leaves `~$`-prefixed lock files behind, which a naive glob would
happily upload.
"""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path

from src.apply.answers import (
    Answers,
    Resolution,
    match_option,
    resolve,
    resolve_fact_wording,
    similar_questions,
)
from src.apply.reconcile import MergedField, Reconciled

# Artifact suffixes in upload preference order. Greenhouse accepts
# .pdf,.doc,.docx,.txt,.rtf; these two are what /tailor writes.
ARTIFACT_SUFFIXES = (".pdf", ".docx")

# DOM id -> the stem endings that name that artifact in a /tailor dir. Matched
# casefolded, because the dirs hold Resume.pdf, resume.pdf and _resume.pdf.
ARTIFACT_STEMS = {
    "resume": ("resume",),
    "cover_letter": ("cover_letter", "cover-letter", "cover letter"),
}

# Word's lock files sit beside the real artifact and match every stem rule.
_LOCK_PREFIX = "~$"

# Kinds whose value is a set of labels rather than one.
_MULTI_KINDS = frozenset({"checkbox_group"})

# An optional question nothing matched is only worth drafting if a person would
# actually type or choose something. A stray checkbox is not; free text and a
# dropdown are. Measured over 39 live boards, the optional unmatched questions
# are overwhelmingly these kinds — some genuinely belong blank ("If other,
# please specify"), which is why this list never blocks a submission and /apply
# is free to draft nothing.
DRAFTABLE_KINDS = frozenset({"text", "textarea", "react_select"})


class PlanError(Exception):
    """The plan cannot be built. Distinct from a park: this is a broken input or
    an internal inconsistency, not a question nothing could answer."""


@dataclass(frozen=True)
class FieldPlan:
    id: str                 # the selector, verbatim
    name: str
    label: str
    kind: str
    section: str
    required: bool
    multi: bool
    value: str | tuple[str, ...] | bool
    tier: str
    description: str = ""
    """`MergedField.description`, carried through so step 7's manifest check can weigh
    a board's own instructional text, not just its label."""

    options: tuple[str, ...] = ()
    """What the widget offered. Held on a *filled* field, not just a parked one:
    an answer can only be audited against the alternatives it beat, and a
    keyword guess that picked the wrong one of nine options looks identical to a
    right one when all you can see is the value. Empty for free text and for
    every DOM-only select, whose real list exists only once a browser has
    opened it (`fill.py`'s `observed_options`)."""

    source: str = ""
    """`Resolution.source` — `bank:<id>[~keyword]`, or the resolver that
    answered this."""

    dom_path: str = ""
    """`MergedField.dom_path` — the real selector when `id` is an alias."""

    dom_control: str = ""
    """`MergedField.dom_control` — which control inside `dom_path`."""

    reason: str = ""
    """`Resolution.reason`. A filled field can carry a caveat worth reading."""

    replaced: str = ""
    """The source an override replaced — `bank:<id>~<keyword>` when the session
    corrected a keyword guess, which is how the bank learns not to guess it."""

    alternates: tuple[str, ...] = ()
    """`Resolution.alternates` — other values the read-back accepts as this answer."""

    @property
    def needs_selection_assert(self) -> bool:
        """react-select accepts a typed string that matches no option and ends
        up empty, so fill.py has to prove the selection stuck. It is the
        only widget that can fail that quietly."""
        return self.kind == "react_select"


@dataclass(frozen=True)
class FilePlan:
    id: str
    label: str
    required: bool
    path: Path
    name: str = ""
    """The DOM's own selector for this upload. Defaults to `id` — true for
    Greenhouse and Lever, where the two never diverge — and only differs on
    Ashby, where `id` is the canonical `resume`/`cover_letter` alias and
    `name` is the field's real, board-specific path."""

    def __post_init__(self):
        if not self.name:
            object.__setattr__(self, "name", self.id)


@dataclass(frozen=True)
class Unmapped:
    id: str
    label: str
    required: bool
    kind: str
    section: str
    tier: str
    reason: str
    options: tuple[str, ...] = ()
    """What the widget offers, for the run report and for /apply's Tier C
    classification — an unanswerable select is a different problem from an
    unanswerable free text."""

    multi: bool = False
    """Whether the widget takes several answers. Carried so fill-time recovery
    clicks every label a "mark all that apply" resolves to, rather than
    assuming one — the hazard `_check_value` names."""

    description: str = ""
    """`MergedField.description` — instructional text under the label that a
    board's own schema/API never states (Ashby, so far). `/apply`'s Tier C
    classification reads this alongside `label` so a question like "please
    explain briefly" or "don't write AI slop" is not classified or drafted
    off the label alone."""

    source: str = ""
    """`Resolution.source`. On a Tier B park, the bank question whose answer
    the board did not offer."""

    similar: tuple[str, ...] = ()
    """For a question the bank does not know: the closest bank questions."""

    blocks: bool = False
    """Parks the role though the board marks it optional: a cover-letter upload
    with no letter, under `cover_letter: when_offered`."""


@dataclass(frozen=True)
class Skipped:
    id: str
    label: str
    tier: str
    reason: str


@dataclass(frozen=True)
class Plan:
    job_id: str
    board: str
    token: str
    form_url: str
    company: str
    title: str
    out_dir: Path
    fields: tuple[FieldPlan, ...]
    files: tuple[FilePlan, ...]
    unmapped: tuple[Unmapped, ...]
    draftable: tuple[Unmapped, ...]
    skipped: tuple[Skipped, ...]
    submit_selector: str | None
    submit_disabled: bool
    api_only: tuple[str, ...] = ()
    ats: str = "greenhouse"
    """Which board this came from — Greenhouse fields select by `id`, Lever's
    by `name`; `fill.py` picks its driver off this, not off `board` (a slug,
    not an ATS name)."""
    requires_captcha: bool = False
    """Lever renders hCaptcha on every form. Unattended submission is
    not possible on one of these — `submit()` blocks for a human to solve it
    rather than clicking blind."""
    ignored_overrides: tuple[str, ...] = ()
    """`--answers` entries that could not apply: an id not on this form, an
    identity/EEOC or export-control field, or a file path that does not exist.
    `apply plan`/`apply run` exit non-zero while any remain."""

    work_authorization: dict = dc_field(default_factory=dict)
    """The facts for this job's country, so the session can judge a board's
    full-sentence work-authorization option against them."""

    @property
    def parked(self) -> bool:
        return bool(self.required_parked)

    @property
    def submittable(self) -> bool:
        """Whether submit() may run at all. The guard itself lives in fill.py —
        this is the same question asked before a browser opens."""
        return not self.parked and self.submit_selector is not None

    @property
    def required_parked(self) -> tuple[str, ...]:
        """Required fields nothing could answer — the reason a role parks that
        the run report has to name."""
        return tuple(u.id for u in self.unmapped if u.required or u.blocks)


# ------------------------------------------------------------------ artifacts


def find_artifact(out_dir: Path, artifact_id: str) -> tuple[Path | None, str]:
    """(path, reason) for one of the two upload artifacts in a /tailor dir.

    Returns (None, reason) when there is nothing to attach or when more than one
    candidate matches — two resumes in one dir is not a tie to break silently,
    it is a question about which document goes out under the user's name.
    """
    stems = ARTIFACT_STEMS.get(artifact_id)
    if stems is None:
        return None, f"{artifact_id}: not an artifact /tailor produces"

    for suffix in ARTIFACT_SUFFIXES:
        hits = sorted(
            p for p in out_dir.iterdir()
            if p.is_file()
            and not p.name.startswith(_LOCK_PREFIX)
            and p.suffix.casefold() == suffix
            and p.stem.casefold().endswith(stems)
        )
        if len(hits) == 1:
            return hits[0], ""
        if len(hits) > 1:
            return None, (
                f"{artifact_id}: {len(hits)} candidates in {out_dir.name} "
                f"({', '.join(p.name for p in hits)}) — cannot choose"
            )

    wanted = "/".join(s.lstrip(".") for s in ARTIFACT_SUFFIXES)
    return None, f"{artifact_id}: no {wanted} in {out_dir.name}"


# ------------------------------------------------------------------ values


def _check_value(field: MergedField, value) -> str | tuple[str, ...] | bool | None:
    """Reject a value that does not fit the widget. Returns an error string, or
    None when the value is usable.

    Cardinality is checked in both directions: a scalar into a "mark all that
    apply" submits one option where several were meant, and a tuple into a
    single select clicks twice, which clears the first click.
    """
    multi = field.multi or field.kind in _MULTI_KINDS

    if isinstance(value, bool):
        if field.kind != "checkbox":
            return f"boolean value for a {field.kind} field"
        return None
    if isinstance(value, tuple):
        if not multi:
            return f"list value for single-valued {field.kind} field {field.id!r}"
        if not value:
            return "empty list of options"
        if any(not str(v).strip() for v in value):
            return "blank option in the list"
        return None
    if isinstance(value, str):
        if multi:
            return f"scalar value for multi-valued field {field.id!r}"
        if not value.strip():
            return "blank value"
        return None
    return f"value is {type(value).__name__}, not str/tuple/bool"


@dataclass(frozen=True)
class Override:
    """One answer the /apply session supplied for one field."""

    value: str | tuple[str, ...]
    source: str = ""
    """Where it came from: `bullets:B-7`, `stories:S-3`, `company_answers`,
    `jd`, `option`, or a bank id."""
    question: str = ""
    """The bank question this label asks — the run saves the label under it."""
    claim: dict | None = None
    """For a work-authorization option: the facts its text states."""
    keyword: str = ""
    """Words every wording of the question shares; saved as a keyword."""
    fact: dict | None = None
    """For a yes/no work-authorization or export-control label the code cannot
    classify: `{fact, yes_when}`. The answer is computed from your facts."""


# Fields no override may touch: identity and EEOC come from stated facts only.
# Export control takes only a `fact` judgment, never a typed value.
_CLOSED_TIERS = frozenset({"A", "A2"})


# Free text a person types. A drafted answer here gets the same lint every
# other drafted text does; an option string never does, since the board wrote it.
_PROSE_KINDS = frozenset({"text", "textarea"})


def _linted(text: str, company: str) -> tuple[str, str]:
    """`(text with mechanical fixes, problem)`. A banned phrase is a problem:
    the field parks naming it, and the session rewrites that sentence."""
    from src.lint import find_phrase_violations, fix_mechanical, load_de_ai_rules

    rules = load_de_ai_rules()
    fixed, _ = fix_mechanical(text, rules)
    found = find_phrase_violations(fixed, context="resume", rules=rules,
                                   keep=[company] if company else [])
    if found:
        phrases = sorted({v["phrase"] for v in found})
        return fixed, f"lint: banned phrase(s) {phrases}"
    return fixed, ""


def _override_resolution(field: MergedField, value, source: str, tier: str,
                         company: str = "") -> Resolution:
    """Apply an override, held to the same standard as every other answer: a
    select takes only an option it offers, spelled the board's way. A value the
    widget does not take parks, and the reason says why."""
    multi = field.multi or field.kind in _MULTI_KINDS
    candidates = (value,) if isinstance(value, str) else tuple(value)
    tag = f"override:{source}" if source else "override"
    if not multi and len(candidates) > 1:
        return Resolution("park", tier=tier, source=tag,
                          reason=f"{len(candidates)} values for a single-valued field")
    if field.kind == "checkbox":
        # fill.py ticks on any truthy value, so only an explicit Yes may pass.
        if candidates[0].strip().casefold() in ("yes", "yes."):
            return Resolution("fill", value="Yes", tier=tier, source=tag)
        return Resolution("park", tier=tier, source=tag,
                          reason="a single checkbox takes only a Yes override")
    if not field.options:
        if field.kind in _PROSE_KINDS and not multi:
            text, problem = _linted(candidates[0], company)
            if problem:
                return Resolution("park", tier=tier, source=tag, reason=problem)
            return Resolution("fill", value=text, tier=tier, source=tag)
        chosen = candidates if multi else candidates[0]
        return Resolution("fill", value=chosen, tier=tier, source=tag)
    picked = tuple(p for p in (match_option(field, c) for c in candidates) if p)
    if len(picked) != len(candidates):
        missing = [c for c in candidates if match_option(field, c) is None]
        return Resolution(
            "park", tier=tier, source=tag,
            reason=f"override {missing!r} matches none of the options this widget "
                   f"offers ({list(o.label for o in field.options)!r})",
        )
    return Resolution("fill", value=picked if multi else picked[0], tier=tier, source=tag)


def _work_authorization_facts(answers: Answers) -> dict[str, object]:
    """The facts for this job's country, JSON-safe. An unknown country has no
    facts: the session must not pick an option from the default row's."""
    from src.apply.answers import UNKNOWN_COUNTRY

    if answers.country == UNKNOWN_COUNTRY:
        return {"job_country": None, "authorized": None, "requires_sponsorship": None,
                "unrestricted": None, "status_label": None,
                "nationalities": list(answers.nationalities), "us_person": answers.us_person,
                "known_claims": len(answers.claims)}
    facts = answers.facts_for(answers.country)
    return {
        "job_country": answers.country,
        "authorized": facts.authorized,
        "requires_sponsorship": facts.requires_sponsorship,
        "unrestricted": facts.unrestricted,
        "status_label": facts.status_label,
        "nationalities": list(answers.nationalities),
        "us_person": answers.us_person,
        "known_claims": len(answers.claims),
    }


def _unmapped(field: MergedField, resolution: Resolution, reason: str = "",
              answers: Answers | None = None, blocks: bool = False) -> Unmapped:
    similar = (similar_questions(field.label, answers)
               if answers is not None and resolution.tier == "C" else ())
    return Unmapped(
        similar=similar,
        id=field.id,
        label=field.label,
        required=field.required,
        kind=field.kind,
        section=field.section,
        tier=resolution.tier,
        reason=reason or resolution.reason,
        options=tuple(o.label for o in field.options),
        multi=field.multi,
        description=field.description,
        source=resolution.source,
        blocks=blocks,
    )


# ------------------------------------------------------------------ build


def build_plan(
    reconciled: Reconciled,
    answers: Answers,
    out_dir: Path,
    *,
    job_id: str = "",
    board: str = "",
    token: str = "",
    form_url: str = "",
    company: str = "",
    title: str = "",
    submit_selector: str | None = None,
    submit_disabled: bool = False,
    overrides: dict[str, Override] | None = None,
    ats: str = "greenhouse",
    requires_captcha: bool = False,
) -> Plan:
    """Resolve every reconciled field into a fill, an attachment or a park.

    `overrides` maps a field id to the session's answer for it. One gate: an
    override answers anything except identity/EEOC fields and export control.
    An override that cannot apply is listed in `ignored_overrides`.
    """
    overrides = overrides or {}
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        raise PlanError(f"{out_dir} is not a directory — run /tailor for this role first")

    # `employment.only_when_required` (see answers.py) decides whether the block
    # is volunteered where a board left it optional. Required-ness is a property
    # of the whole block, not of each field: filling company and title while
    # skipping an optional end date would read as "still works there".
    fill_employment = not answers.employment_only_when_required or any(
        f.required for f in reconciled.fields if f.section == "employment"
    )

    fields: list[FieldPlan] = []
    files: list[FilePlan] = []
    unmapped: list[Unmapped] = []
    draftable: list[Unmapped] = []
    skipped: list[Skipped] = []
    ignored: list[str] = []

    for field in reconciled.fields:
        if field.section == "employment" and not fill_employment:
            skipped.append(Skipped(
                id=field.id, label=field.label, tier="A",
                reason="employment.only_when_required, and this board does not "
                       "require the block",
            ))
            continue

        override = overrides.get(field.id)
        if field.kind == "file" and override is not None:
            # A file question's override is a path, resolved against the role's
            # own folder so a bare filename works.
            candidate = Path(override.value) if isinstance(override.value, str) else None
            if candidate is not None and not candidate.is_absolute():
                candidate = out_dir / candidate
            if candidate is not None and candidate.is_file():
                files.append(FilePlan(
                    id=field.id, name=field.name, label=field.label,
                    required=field.required, path=candidate,
                ))
                continue
            ignored.append(f"{field.id}: override path {override.value!r} is not a file")
            override = None

        resolution = resolve(field, answers)
        replaced = ""
        if override is not None and field.kind != "file":
            if resolution.tier in _CLOSED_TIERS:
                ignored.append(f"{field.id}: identity and EEOC fields take no override")
            elif override.fact is not None:
                if resolution.tier != "B0":
                    ignored.append(f"{field.id}: a fact judgment is only for a "
                                   "work-authorization or export-control question")
                else:
                    replaced = resolution.source
                    resolution = resolve_fact_wording(field, answers, override.fact)
            elif resolution.source == "export_control":
                ignored.append(f"{field.id}: export control takes a fact judgment "
                               "({fact, yes_when}), never a typed value")
            else:
                replaced = resolution.source
                resolution = _override_resolution(field, override.value, override.source,
                                                  resolution.tier, company)

        if resolution.action == "park":
            unmapped.append(_unmapped(field, resolution, answers=answers))
            continue

        if resolution.action == "skip":
            if field.required:
                raise PlanError(
                    f"{field.id!r} is required and resolved to skip "
                    f"({resolution.reason!r}) — a required field may only fill or park"
                )
            if resolution.tier == "C" and field.kind in DRAFTABLE_KINDS:
                # Optional and unmatched, but a human would answer it. /apply
                # gets the chance; leaving it blank stays a valid outcome.
                draftable.append(_unmapped(field, resolution, answers=answers))
            else:
                skipped.append(Skipped(
                    id=field.id, label=field.label,
                    tier=resolution.tier, reason=resolution.reason,
                ))
            continue

        if resolution.action == "defer":
            path, reason = find_artifact(out_dir, field.id)
            if path is None:
                wanted = (field.id == "cover_letter"
                          and answers.cover_letter == "when_offered")
                if field.required or wanted:
                    if wanted and not field.required:
                        reason += " (cover_letter: when_offered)"
                    unmapped.append(_unmapped(field, resolution, reason, blocks=wanted))
                else:
                    skipped.append(Skipped(
                        id=field.id, label=field.label, tier=resolution.tier,
                        reason=reason,
                    ))
                continue
            files.append(FilePlan(
                id=field.id, name=field.name, label=field.label,
                required=field.required, path=path,
            ))
            continue

        if resolution.action != "fill":
            raise PlanError(
                f"{field.id!r}: unknown resolution action {resolution.action!r}"
            )

        problem = _check_value(field, resolution.value)
        if problem is not None:
            # answers.py produced something the widget cannot take. Not a park:
            # parking would hide a resolver bug behind a plausible-looking
            # "nothing could answer this".
            raise PlanError(f"{field.id!r}: {problem} (value={resolution.value!r})")

        fields.append(FieldPlan(
            id=field.id,
            name=field.name,
            label=field.label,
            kind=field.kind,
            section=field.section,
            required=field.required,
            multi=field.multi or field.kind in _MULTI_KINDS,
            value=resolution.value,
            tier=resolution.tier,
            description=field.description,
            options=tuple(o.label for o in field.options),
            source=resolution.source,
            reason=resolution.reason,
            dom_path=field.dom_path,
            dom_control=field.dom_control,
            replaced=replaced,
            alternates=resolution.alternates,
        ))

    on_form = {f.id for f in reconciled.fields}
    for field_id in overrides:
        if field_id not in on_form:
            ignored.append(f"{field_id}: not a field on this form")

    plan = Plan(
        job_id=job_id,
        board=board,
        token=token,
        form_url=form_url,
        company=company,
        title=title,
        out_dir=out_dir,
        fields=tuple(fields),
        files=tuple(files),
        unmapped=tuple(unmapped),
        draftable=tuple(draftable),
        skipped=tuple(skipped),
        submit_selector=submit_selector,
        submit_disabled=submit_disabled,
        api_only=reconciled.api_only,
        ats=ats,
        requires_captcha=requires_captcha,
        work_authorization=_work_authorization_facts(answers),
        ignored_overrides=tuple(ignored),
    )
    _assert_accounted_for(reconciled, plan)
    return plan


def _assert_accounted_for(reconciled: Reconciled, plan: Plan) -> None:
    """Every rendered field appears exactly once in the plan, and every required
    one is filled, attached or parked.

    This is the guard for the failure mode that costs the most: a required field
    that quietly falls out between the scan and the plan, leaving a form that
    only fails at submit — or worse, submits without it.
    """
    placed: dict[str, int] = {}
    for group in (plan.fields, plan.files, plan.unmapped, plan.draftable, plan.skipped):
        for item in group:
            placed[item.id] = placed.get(item.id, 0) + 1

    scanned = {f.id for f in reconciled.fields}
    lost = scanned - set(placed)
    if lost:
        raise PlanError(f"fields vanished between scan and plan: {sorted(lost)}")
    twice = sorted(i for i, n in placed.items() if n > 1)
    if twice:
        raise PlanError(f"fields planned more than once: {twice}")
    invented = set(placed) - scanned
    if invented:
        raise PlanError(f"plan names fields the form does not render: {sorted(invented)}")

    answered = {f.id for f in plan.fields} | {f.id for f in plan.files}
    parked = {u.id for u in plan.unmapped}
    unresolved = [
        f.id for f in reconciled.fields
        if f.required and f.id not in answered and f.id not in parked
    ]
    if unresolved:
        raise PlanError(f"required fields neither resolved nor parked: {sorted(unresolved)}")


def plan_for_board(
    board_form,
    answers: Answers,
    out_dir: Path,
    job_id: str = "",
    overrides: dict[str, Override] | None = None,
    ats: str = "greenhouse",
    requires_captcha: bool = False,
) -> Plan:
    """build_plan over a fetched board, carrying its identifiers into the plan.

    `board_form` is `greenhouse.BoardForm` or a same-shaped stand-in
    (`lever.LeverBoard`, `ashby.AshbyBoard`) — duck-typed on purpose, so each
    board module can produce one without this function knowing it exists.
    """
    return build_plan(
        board_form.reconciled,
        answers,
        out_dir,
        job_id=job_id,
        board=board_form.slug,
        token=board_form.posting.token,
        form_url=board_form.posting.form_url,
        company=board_form.schema.company_name,
        title=board_form.schema.title,
        submit_selector=board_form.scan.submit_selector,
        submit_disabled=board_form.scan.submit_disabled,
        overrides=overrides,
        ats=ats,
        requires_captcha=requires_captcha,
    )
