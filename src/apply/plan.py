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

    draftable[] an optional question no rule matched, of a kind a person would
                actually answer — /apply may draft it, and leaving it blank
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
from pathlib import Path

from src.apply.answers import Answers, Resolution, match_option, resolve
from src.apply.greenhouse import BoardForm
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

    @property
    def needs_selection_assert(self) -> bool:
        """react-select accepts a typed string that matches no option and ends
        up empty, so fill.py has to prove the selection stuck (§9). It is the
        only widget that can fail that quietly."""
        return self.kind == "react_select"


@dataclass(frozen=True)
class FilePlan:
    id: str
    label: str
    required: bool
    path: Path


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
    does not have to assume: it used to hardcode `multi=False`, so a required
    "mark all that apply" resolved to one label, got clicked once, and was
    marked recovered — the exact hazard `_check_value` names."""


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
    """Lever renders hCaptcha on every form (§12a). Unattended submission is
    not possible on one of these — `submit()` blocks for a human to solve it
    rather than clicking blind."""

    @property
    def parked(self) -> bool:
        return bool(self.unmapped)

    @property
    def submittable(self) -> bool:
        """Whether submit() may run at all. The guard itself lives in fill.py —
        this is the same question asked before a browser opens."""
        return not self.parked and self.submit_selector is not None

    @property
    def required_parked(self) -> tuple[str, ...]:
        """Required fields nothing could answer — the reason a role parks that
        the run report has to name."""
        return tuple(u.id for u in self.unmapped if u.required)


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


def _override_resolution(field: MergedField, value, tier: str) -> Resolution:
    """Apply an `--answers` override, held to the same standard as every
    deterministic answer.

    An override is an LLM-drafted string. Every other path into a select goes
    through `_pick_option`, which refuses a value the widget does not offer
    *and* canonicalizes to the board's own spelling. The override path used to
    bypass both, so `"yes"` against an offered `"Yes"` planned the literal
    lowercase string: the plan read READY, `/apply`'s unmapped-diff saw
    nothing wrong, and the role died in the browser instead — with the answer
    unavailable to the recovery path, which re-resolves without overrides.

    A value the widget does not offer parks rather than raising: the question
    is genuinely still unanswered, which is what `unmapped[]` means, and the
    run report then names it for the next `/apply` pass.
    """
    candidates = (value,) if isinstance(value, str) else tuple(value)
    # `field.multi` alone disagrees with `_check_value` and `build_plan`, which
    # both use `multi or kind in _MULTI_KINDS`. Using the bare flag here made a
    # 2-item override on a single-valued select silently keep the first value
    # and drop the second — where the old code raised. Same predicate
    # everywhere, and a list into a single-valued widget is an error, not a
    # truncation.
    multi = field.multi or field.kind in _MULTI_KINDS
    if not multi and len(candidates) > 1:
        raise PlanError(
            f"override for single-valued field {field.id!r} supplies "
            f"{len(candidates)} values: {list(candidates)!r}"
        )
    if not field.options:
        # DOM-only select or free text: nothing to validate against, and
        # fill.py's post-selection assert is the real check (§9).
        return Resolution("fill", value=value, tier=tier)

    picked = tuple(p for p in (match_option(field, c) for c in candidates) if p)
    if len(picked) != len(candidates):
        missing = [c for c in candidates if match_option(field, c) is None]
        return Resolution(
            "park", tier=tier,
            reason=f"override {missing!r} matches none of the options this widget "
                   f"offers ({list(o.label for o in field.options)!r})",
        )
    return Resolution("fill", value=picked if multi else picked[0], tier=tier)


def _unmapped(field: MergedField, resolution: Resolution, reason: str = "") -> Unmapped:
    return Unmapped(
        id=field.id,
        label=field.label,
        required=field.required,
        kind=field.kind,
        section=field.section,
        tier=resolution.tier,
        reason=reason or resolution.reason,
        options=tuple(o.label for o in field.options),
        multi=field.multi,
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
    overrides: dict[str, tuple[str | tuple[str, ...], str]] | None = None,
    ats: str = "greenhouse",
    requires_captcha: bool = False,
) -> Plan:
    """Resolve every reconciled field into a fill, an attachment or a park.

    `overrides` is a per-run, per-field lookup — `field.id -> (value, tier)`,
    `tier` one of `"C1"`/`"C2"`/`"JD"` — supplied by `/apply` (`.claude/
    commands/apply.md`) after it classifies and, for C1, drafts an answer, or
    resolves a C2 question from that role's `company_answers.md`, or reads a
    salary figure off the JD. It exists so a required Tier C question can
    ever leave `unmapped[]` without either judgment landing in this module
    (R7) or a company-specific answer leaking into
    `profile/application_answers.yaml` (§15 forbids exactly that for C1; it
    binds harder for C2, which is company-specific by construction).
    `"C1"`/`"C2"` are consulted only for `resolve()`'s Tier C outcomes, same
    as always. `"JD"` is the one exception: it is also consulted for a Tier
    B outcome, so a salary figure the JD itself states can supersede a
    static `rules:` match — every other tier keeps deciding itself.
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

    for field in reconciled.fields:
        if field.section == "employment" and not fill_employment:
            skipped.append(Skipped(
                id=field.id, label=field.label, tier="A",
                reason="employment.only_when_required, and this board does not "
                       "require the block",
            ))
            continue

        resolution = resolve(field, answers)

        if field.id in overrides:
            value, override_tier = overrides[field.id]
            # C1/C2 only ever supersede a Tier C outcome — a drafted answer
            # must never silently clobber an identity/EEOC/work-authorization
            # field. "JD" is the one exception: a figure read from the JD
            # is allowed to supersede a static Tier B rule too (a salary
            # question resolved by a generic `rules:` keyword match), since
            # that is the whole point of checking the JD before falling back
            # to the configured default.
            if resolution.tier == "C" or (resolution.tier == "B" and override_tier == "JD"):
                resolution = _override_resolution(field, value, override_tier)

        if resolution.action == "park":
            unmapped.append(_unmapped(field, resolution))
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
                draftable.append(_unmapped(field, resolution))
            else:
                skipped.append(Skipped(
                    id=field.id, label=field.label,
                    tier=resolution.tier, reason=resolution.reason,
                ))
            continue

        if resolution.action == "defer":
            path, reason = find_artifact(out_dir, field.id)
            if path is None:
                if field.required:
                    unmapped.append(_unmapped(field, resolution, reason))
                else:
                    skipped.append(Skipped(
                        id=field.id, label=field.label, tier=resolution.tier,
                        reason=reason,
                    ))
                continue
            files.append(FilePlan(
                id=field.id, label=field.label, required=field.required, path=path,
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
        ))

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
    overrides: dict[str, tuple[str | tuple[str, ...], str]] | None = None,
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
