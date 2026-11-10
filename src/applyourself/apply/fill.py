"""Execute a plan against a real form: fill, and `submit()`, the one function that clicks submit.

The plan decided everything; this module types, selects, checks and uploads, and
refuses to paper over a widget that did not take the value. Two rules shape it:

**Config always wins.** Uploading a resume can trigger a server-side parse that
writes into fields already on the page. So the order is: attach first, re-read
every field, then overwrite from the plan regardless of what is sitting there.
Nothing parsed is trusted and nothing parsed is left in place — what goes out
has to be reproducible from `application_answers.yaml` (and the resume.yaml facts it reads) alone.

**A react-select that did not stick is a failure, not a filled field.** It
accepts a typed string matching no option and ends up empty; required, that
fails at submit, and optional, it fails silently. Every one of them is asserted
after selection. Measured over 45 live boards, the required react-selects with
no option list to validate against beforehand are `country` (32),
`candidate-location` (16), `degree--0` (4), `discipline--0` (4) and
`school--0` (3) — so this assert is not an edge case, it is the main path.

`playwright` is an optional dependency (`uv sync --group apply`); `browser.py`
is the only module in `src/` that names the driver.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from dataclasses import field as dc_field
from pathlib import Path

from applyourself.apply import bank
from applyourself.apply.answers import Answers, resolve
from applyourself.apply.browser import launch as _launch
from applyourself.apply.browser import require_playwright as _require_playwright
from applyourself.apply.drivers import (  # noqa: F401 - drivers are looked up by name in _driver_for
    _OPTION_POLL_MS,
    SUBMIT_REQUEST_MARKERS,
    UPLOAD_READBACK_TIMEOUT,
    AshbyBrowserDriver,
    BrowserDriver,
    FillError,
    LeverBrowserDriver,
)
from applyourself.apply.plan import FieldPlan, FilePlan, Plan
from applyourself.apply.reconcile import MergedField, MergedOption
from applyourself.shared import clock, paths
from applyourself.shared.job_boards import DRIVER_NAMES

log = logging.getLogger(__name__)

# Post-attach settle only. The resume parse writes into fields already on the
# page, and on Lever it routinely outruns OPTION_TIMEOUT -- the fill then races
# it and the parse wins. Floor as well as ceiling: networkidle can report idle
# in the gap before the parse XHR is even issued.
UPLOAD_PARSE_TIMEOUT = 30000
UPLOAD_PARSE_FLOOR_MS = 5000
# How many times to click when the board keeps refusing. A refusal is positive
# evidence nothing was sent, so a retry cannot duplicate an application.
SUBMIT_ATTEMPTS = 2
# How long to let the board answer the click before reading the page.
SUBMIT_SETTLE_MS = 5000
# Extra time to keep polling for the submit-endpoint request once
# SUBMIT_SETTLE_MS has passed and none has landed yet, before treating its
# absence as ambiguous rather than proof of nothing sent. A board can answer
# well after a fixed 5s wait would have called it silence. Polled in short
# steps so a request that lands early still returns immediately; only a truly
# silent click burns the whole budget.
SUBMIT_REQUEST_EXTRA_WAIT_MS = 15000
SUBMIT_REQUEST_POLL_MS = 500
# Wait between field writes. A board can debounce its own answered/validation
# state, and a fill fast enough to outrun that gets a varying subset of
# correctly-written fields reported missing at submit. Applied to every board:
# two seconds a field is cheap next to a false "missing" on an irreversible
# click.
FIELD_PACE_MS = 2000
# How long to let a react-select's chosen value land before calling it stuck.
# Ashby's React state update can lag a click by a beat.
SELECT_SETTLE_TIMEOUT = 2000


class SubmitGuardError(Exception):
    """Refused to click submit. The message names exactly what is still
    unresolved — this is the invariant that keeps a parked or half-filled role
    from ever going out."""


@dataclass
class FieldOutcome:
    id: str
    action: str  # filled | attached | failed
    before: str = ""  # what was in the field before we wrote to it
    after: str = ""
    note: str = ""

    @property
    def was_prefilled(self) -> bool:
        """Non-empty before we touched it — either a board default or, after an
        upload, the resume parser having written into it."""
        return bool(self.before.strip())


@dataclass
class FillResult:
    form_url: str
    outcomes: list[FieldOutcome] = dc_field(default_factory=list)
    failures: list[str] = dc_field(default_factory=list)
    observed_options: dict[str, tuple[str, ...]] = dc_field(default_factory=dict)
    """Options read off an opened react-select. The only place these exist —
    neither the API nor the served HTML carries them for the DOM-only selects."""

    recovered: list[str] = dc_field(default_factory=list)
    """Parked fields that resolved once their real options could be read. The
    submit guard has to subtract these from the plan's unmapped[], or a role
    parks over a question that is now answered."""

    manifest: list[dict] = dc_field(default_factory=list)
    """`form_manifest()` — every question and what the page actually held for
    it, read after the fill and before any click. Published on the result (and
    so through `sink`) rather than returned, so it survives the same
    exceptions `submitted` does."""

    landed_url: str = ""
    title: str = ""
    """Where the fill ended up. The browser knows both and nothing recorded
    them outside the post-submit evidence file."""

    submitted: bool = False
    """The submit button was clicked. Set the instant the click returns, before
    anything post-click can fail — an exception after the click must not lose
    the fact that we applied, or the role stays `tailored` and the next run
    applies to the same board again."""

    confirmed: bool = False
    """The board positively acknowledged the application. Absence is NOT
    evidence of failure: this only ever goes true on a positive match against
    `CONFIRMATION_MARKERS`. A submitted-but-unconfirmed role is reported as
    such and still transitions to `applied`, because a duplicate application
    is the worse failure."""

    submit_requests: list[str] = dc_field(default_factory=list)
    """2xx responses to the board's own submit endpoint, seen after the click.
    The one signal that cannot be faked by a page that merely looks right —
    Ashby's button rendered, enabled and clickable, fired nothing at all."""

    evidence: Path | None = None
    """Where the post-submit page was written. The record that lets
    `CONFIRMATION_MARKERS` be extended from what a board actually renders
    rather than from another guess."""

    outcome: str = ""
    """What the click came to, set by `submit()`:
    `accepted` — the board's submit request or confirmation text was seen.
    `ambiguous` — clicked on a board with no request marker, no confirmation.
    `unsent` — clicked on a board with a request marker, and no request fired.
    `rejected` — the board received it and said no (spam flag, duplicate).
    `refused` — the board declined every click; nothing was sent.
    Empty when no click was made."""

    submit_error: str = ""
    """Set by `run_one` when `submit=True`. Empty + `submitted=False` means
    submission was never attempted (either `submit=False` or the fill itself
    failed first) — check `submit_error` to tell that apart from a refusal."""

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def prefilled(self) -> tuple[str, ...]:
        return tuple(o.id for o in self.outcomes if o.was_prefilled)


#: Board -> driver class name, from the shared board table.
_DRIVER_NAMES = dict(DRIVER_NAMES)


def has_driver(ats: str) -> bool:
    """Whether a browser driver exists for this board.

    `apply_cli` checks this before opening anything, so a board with no driver
    is reported as manual-apply instead of launching Chrome and only then
    raising.
    """
    return ats in _DRIVER_NAMES


def _driver_for(ats: str, page) -> BrowserDriver:
    """Looks the class up by name in this module's globals at call time, not
    a dict of class objects bound at import time — so
    `monkeypatch.setattr(fill, "BrowserDriver", Fake)` in a test still reaches
    here, the same convention `apply_cli.py` uses for its own stubs."""
    name = _DRIVER_NAMES.get(ats)
    if name is None:
        raise FillError(f"no browser driver for ats={ats!r}")
    return globals()[name](page)


def _merged_from(field: FieldPlan, options: tuple[str, ...]) -> MergedField:
    """The planned field as reconcile would have produced it, had the options
    been readable without a browser."""
    return MergedField(
        id=field.id,
        name=field.name,
        label=field.label,
        required=field.required,
        kind=field.kind,
        section=field.section,
        multi=field.multi,
        options=tuple(MergedOption(label=o) for o in options),
    )


def _relabel(field: FieldPlan, options: tuple[str, ...], answers: Answers | None) -> str | None:
    """Re-resolve a field against the options actually on screen. Same rule as
    the plan used, better input — never a fuzzy match on the planned string."""
    if answers is None:
        return None
    resolution = resolve(_merged_from(field, options), answers)
    if resolution.action != "fill":
        return None
    value = resolution.value
    return (
        value[0]
        if isinstance(value, tuple) and value
        else (value if isinstance(value, str) else None)
    )


def _select(
    driver, field: FieldPlan, label: str, result: FillResult, answers: Answers | None = None
) -> str:
    """Type a label into a react-select, click the matching option, and prove it
    stuck. Returns the value now showing; raises FillError if nothing stuck."""
    driver.type_into(field.id, label)
    # Typing filters the list, so this is what remains — enough to explain a
    # miss, and the only record of what the widget offered at all.
    offered = driver.visible_options()
    if offered:
        result.observed_options.setdefault(field.id, offered)

    if not driver.click_option(label):
        # The planned string missed. The options are readable now, which they
        # were not when the plan was built, so ask the same rule again with the
        # real list before giving up — `country` is planned as "United States"
        # and offered as "United States +1" on every board that renders it.
        retry = _relabel(field, offered, answers) if offered else None
        if retry is not None and retry != label:
            driver.type_into(field.id, retry)
            if driver.click_option(retry):
                label = retry
            else:
                retry = None
        if retry is None:
            driver.close()
            raise FillError(
                f"{field.id}: no option matching {label!r}"
                + (f"; offered {list(offered)}" if offered else "")
            )
    if driver.is_expanded(field.id):
        raise FillError(f"{field.id}: listbox still open after selecting {label!r}")
    # The click has already returned and the option is chosen, but Ashby's
    # React state update can lag it by a beat — read immediately after and
    # `selected_label` can still come back empty for a value that lands a
    # moment later — the same field, read again a moment after, holds the
    # right value. So poll rather than raise on the first read.
    shown = driver.selected_label(field.id)
    deadline = SELECT_SETTLE_TIMEOUT
    while not shown and deadline > 0:
        driver.wait(_OPTION_POLL_MS)
        shown = driver.selected_label(field.id)
        deadline -= _OPTION_POLL_MS
    if not shown:
        raise FillError(f"{field.id}: selected {label!r} but the field is empty")
    return shown


def _apply_field(
    driver, field: FieldPlan, result: FillResult, answers: Answers | None = None
) -> FieldOutcome:
    before = driver.value_of(field.id)
    kind = driver.resolve_kind(field.id, field.kind)
    if kind != field.kind:
        field = replace(field, kind=kind)

    if field.kind == "yesno":
        driver.set_yesno(field.id, str(field.value))
        shown = driver.selected_label(field.id)
        if shown.strip().casefold() != str(field.value).strip().casefold():
            raise FillError(f"{field.id}: chose {field.value!r} but reads {shown!r}")
        return FieldOutcome(field.id, "filled", before, shown)

    if field.kind == "checkbox":
        # Only True or an explicit Yes ticks; False unticks; any other value,
        # "No" above all, is refused rather than read as truthy.
        if not isinstance(field.value, bool) and (
            str(field.value).strip().rstrip(".").casefold() != "yes"
        ):
            raise FillError(f"{field.id}: a single checkbox takes only Yes, got {field.value!r}")
        driver.set_checkbox(field.id, field.value is True or isinstance(field.value, str))
        return FieldOutcome(field.id, "filled", before, str(bool(field.value)))

    if field.kind == "checkbox_group":
        labels = field.value if isinstance(field.value, tuple) else (field.value,)
        for label in labels:
            driver.check_group_option(field.id, label)
        return FieldOutcome(field.id, "filled", before, ", ".join(labels))

    if field.kind == "react_select":
        labels = field.value if isinstance(field.value, tuple) else (field.value,)
        shown = ""
        for label in labels:
            shown = _select(driver, field, label, result, answers)
        return FieldOutcome(field.id, "filled", before, shown)

    if field.kind == "select":
        driver.select_native(field.id, str(field.value))
        shown = driver.selected_option_label(field.id)
        if shown.strip() != str(field.value).strip():
            raise FillError(f"{field.id}: selected {field.value!r} but reads {shown!r}")
        return FieldOutcome(field.id, "filled", before, shown)

    if field.kind == "radio_group":
        labels = field.value if isinstance(field.value, tuple) else (field.value,)
        for label in labels:
            driver.check_radio_group(field.id, label)
        return FieldOutcome(field.id, "filled", before, ", ".join(labels))

    driver.fill_text(field.id, str(field.value))
    if field.kind == "date":
        # Ashby's react-datepicker opens on focus and nothing else dismisses
        # it, so the overlay intercepts the click on every later field.
        # Closed before the read-back, not after: the widget clears an
        # unparseable value on blur, so only a value that survives the close
        # is really there.
        driver.close()
    after = driver.value_of(field.id)
    if after.strip() != str(field.value).strip():
        raise FillError(f"{field.id}: wrote {field.value!r} but the field reads {after!r}")
    return FieldOutcome(field.id, "filled", before, after)


def _attach(driver, upload: FilePlan) -> FieldOutcome:
    if not upload.path.is_file():
        raise FillError(f"{upload.id}: {upload.path} is gone")
    driver.set_files(upload.name, upload.path)
    # Read back, not assumed: an input that ends up empty leaves failures[]
    # empty, so the submit guard passes and the application goes out with no
    # resume attached. See `attached_files` for why `None` is not failure.
    #
    # The widget outranks the input. A board that rendered an upload error
    # refused the file no matter what `input.files` says, and a board showing
    # the filename accepted it no matter whether the input survived.
    shown = driver.upload_shows(upload.name, upload.path.name)
    if shown is False:
        raise FillError(
            f"{upload.id}: the board rejected {upload.path.name} — "
            f"its upload widget rendered an error"
        )
    if shown is True:
        return FieldOutcome(upload.id, "attached", "", upload.path.name)

    held = driver.attached_files(upload.name)
    if held is None:
        return FieldOutcome(
            upload.id,
            "attached",
            "",
            upload.path.name,
            note="attached, unverified: the input was replaced after upload",
        )
    if not held:
        raise FillError(f"{upload.id}: set {upload.path.name} but the input holds no file")
    if upload.path.name not in held:
        raise FillError(
            f"{upload.id}: set {upload.path.name} but the input holds {', '.join(held)}"
        )
    return FieldOutcome(upload.id, "attached", "", upload.path.name)


def fill_plan(
    plan: Plan, driver, answers: Answers | None = None, result: FillResult | None = None
) -> FillResult:
    """Drive one plan to a filled — never submitted — form.

    Attachments go first and the page is allowed to settle, because an upload
    can rewrite fields. Everything else is then written over whatever is there.
    """
    result = result if result is not None else FillResult(form_url=plan.form_url)
    if hasattr(driver, "learn_dom_aliases"):
        driver.learn_dom_aliases(plan)
    driver.goto(plan.form_url)

    # `goto` waits only for the form element, but an upload widget is driven by
    # script that loads later. Clicking Attach before it is wired produces the
    # same "uploadFile of undefined" refusal as writing to the hidden input —
    # observed hitting the first upload while the second, a moment later, was
    # fine.
    if plan.files:
        driver.settle()

    for upload in plan.files:
        try:
            result.outcomes.append(_attach(driver, upload))
        except FillError as exc:
            result.failures.append(str(exc))
            result.outcomes.append(FieldOutcome(upload.id, "failed", note=str(exc)))
    if plan.files:
        driver.settle(timeout=UPLOAD_PARSE_TIMEOUT, floor_ms=UPLOAD_PARSE_FLOOR_MS)

    for field in plan.fields:
        try:
            result.outcomes.append(_apply_field(driver, field, result, answers))
        except FillError as exc:
            result.failures.append(str(exc))
            result.outcomes.append(FieldOutcome(field.id, "failed", note=str(exc)))
        driver.wait(FIELD_PACE_MS)

    _probe_parked_selects(driver, plan, result, answers)
    return result


def _probe_parked_selects(
    driver, plan: Plan, result: FillResult, answers: Answers | None = None
) -> None:
    """Read the option list of every parked react-select, and re-resolve it.

    A parked field is not filled, so its widget is never opened, so the one
    place its options exist stays unread. Some of those parks are caused by
    exactly that absence rather than by anything unanswerable: measured live,
    `hispanic_ethnicity` offers `Yes / No / Decline To Self Identify`, which the
    Tier A2 opt-out rule already covers — it simply had no list to match
    against. Same for a Tier B candidate list that could not be checked.

    So: open, read, and ask `answers.resolve` again with the real options. The
    rule that decides is unchanged; only its input got better. Anything still
    unresolved stays parked.
    """
    for parked in plan.unmapped:
        # The driver has the last word on the widget: an Ashby field planned as
        # `select`/`combobox` renders as the same type-and-pick control this
        # recovery pass was written for, and would otherwise never be probed.
        if driver.resolve_kind(parked.id, parked.kind) != "react_select":
            continue
        if parked.multi:
            # `resolve` returns one label for a multi field, `_apply_field`
            # clicks it once, and this loop would then call the question
            # answered. A "mark all that apply" needs every intended option,
            # and nothing here can know the set — leave it parked.
            continue
        options = result.observed_options.get(parked.id)
        if options is None:
            try:
                options = driver.open_options(parked.id)
                driver.close()
            except Exception as exc:  # noqa: BLE001 - a diagnostic must not fail a run
                log.debug("could not read options for %s: %s", parked.id, exc)
                continue
            if options:
                result.observed_options[parked.id] = options
        if not options or answers is None:
            continue

        field = MergedField(
            id=parked.id,
            name=parked.id,
            label=parked.label,
            required=parked.required,
            kind=parked.kind,
            section=parked.section,
            multi=parked.multi,
            options=tuple(MergedOption(label=o) for o in options),
        )
        resolution = resolve(field, answers)
        if resolution.action != "fill":
            continue
        plan_field = FieldPlan(
            id=parked.id,
            name=parked.id,
            label=parked.label,
            kind=parked.kind,
            section=parked.section,
            required=parked.required,
            multi=parked.multi,
            value=resolution.value,
            tier=resolution.tier,
        )
        try:
            outcome = _apply_field(driver, plan_field, result, answers)
        except FillError as exc:
            log.debug("re-resolved %s but it would not take: %s", parked.id, exc)
            continue
        outcome.note = f"recovered at fill time ({resolution.tier})"
        result.outcomes.append(outcome)
        result.recovered.append(parked.id)


def blocking_questions(plan: Plan, result: FillResult) -> tuple[str, ...]:
    """Required questions still unanswered after fill-time recovery.

    `Plan.required_parked`, not all of `plan.unmapped`: an optional field the
    judge asked about parks too, and is never a reason to refuse the click.
    """
    recovered = set(result.recovered)
    return tuple(i for i in plan.required_parked if i not in recovered)


def blocking_details(plan: Plan, result: FillResult) -> tuple[dict, ...]:
    """The same questions as `blocking_questions`, as rows a report can read.

    A bare field id names nothing a person can act on — `d08327ab-…` is the
    whole of what a parked role's report line said, while the label, the reason
    it parked and the options the widget offered were all sitting in
    `plan.unmapped`. Kept as a sibling rather than a wider return type because
    `blocking_questions` is also the submit guard's own check, where the
    ids are exactly right.

    `options` prefers what the browser actually read (`observed_options`) over
    the plan's list: for a DOM-only select the plan's list is empty, and the
    real one only exists once the widget has been opened.
    """
    blocking = set(blocking_questions(plan, result))
    rows = []
    for u in plan.unmapped:
        if u.id not in blocking:
            continue
        rows.append(
            {
                "id": u.id,
                "label": u.label,
                "tier": u.tier,
                "reason": u.reason,
                "source": u.source,
                "options": list(result.observed_options.get(u.id) or u.options),
                "description": u.description,
            }
        )
    return tuple(rows)


def read_back(driver, field_id: str, planned_kind: str) -> str:
    """What the page shows in one field right now, as a string.

    The per-kind read half of `_reverify_fields`, factored out so something
    other than the drift check can use it. Reads against the *live* kind, for
    the same reason `_reverify_fields` does: Ashby declares `select` and then
    renders a radio group, a combobox or a lone checkbox.

    Best-effort by construction — an unreadable field returns `""`, because a
    manifest that raises is worse than a manifest with a gap in it.
    """
    try:
        kind = driver.resolve_kind(field_id, planned_kind)
        if kind == "yesno":
            return driver.selected_label(field_id)
        if kind == "radio_group":
            return driver.checked_radio_label(field_id)
        if kind == "checkbox_group":
            return ", ".join(driver.checked_group_labels(field_id))
        if kind == "select":
            return driver.selected_option_label(field_id)
        if kind == "react_select":
            return driver.selected_label(field_id)
        return driver.value_of(field_id)
    except Exception as exc:  # noqa: BLE001 - a read must never fail a run
        log.debug("%s: read-back failed (%s)", field_id, exc)
        return ""


def form_manifest(driver, plan: Plan, result: FillResult) -> list[dict]:
    """Every question on the form and what the page actually holds for it, read
    back after the fill and before any click.

    Three things make this different from `render_fill`'s per-field report,
    and each one is a failure it exists to catch:

    - It covers the questions nothing else reads back — `unmapped`,
      `draftable` and `skipped`. A parked question is still a field on a live
      form, and a board that prefilled it (or a resume parser that did) has
      already put an answer there under the user's name.
    - It pairs planned against actual, which `FieldOutcome` alone cannot: it
      records `after` but not what was intended, so nothing could compare them
      without re-joining against the plan.
    - It carries the real option list for a DOM-only select. Greenhouse's
      `candidate-location`, Ashby's comboboxes and Lever's location box declare
      no options anywhere in the API or the served HTML, so at plan time the
      list is empty and any value is accepted blind. `observed_options` is the
      only place the true list ever exists.

    `invalid` is the browser's own `checkValidity()` verdict, which `submit()`
    computes and then discards.
    """
    invalid = set()
    try:
        invalid = set(driver.invalid_fields())
    except Exception as exc:  # noqa: BLE001 - a read must never fail a run
        log.debug("validity check unreadable (%s)", exc)

    outcomes = {o.id: o for o in result.outcomes}
    rows: list[dict] = []

    def row(field_id, label, kind, planned, group, **extra):
        outcome = outcomes.get(field_id)
        actual = read_back(driver, field_id, kind)
        try:
            live = driver.resolve_kind(field_id, kind)
        except Exception as exc:  # noqa: BLE001 - a read must never fail a run
            log.debug("%s: live kind unreadable (%s)", field_id, exc)
            live = kind
        # Popped unconditionally: computing it inside the dict display let the
        # `or` short-circuit skip the pop, and `**extra` then put the plan's
        # empty list back over the options the browser had just read.
        planned_options = extra.pop("options", ())
        alternates = extra.pop("alternates", ())
        return {
            "id": field_id,
            "label": label,
            "kind": kind,
            "group": group,
            "planned": planned,
            "actual": actual,
            "matches": any(
                _matches(field_id, live, want, actual) for want in (planned, *alternates)
            ),
            "prefilled": bool(outcome.was_prefilled) if outcome else False,
            "before": outcome.before if outcome else "",
            "invalid": field_id in invalid,
            "options": list(result.observed_options.get(field_id) or planned_options),
            **extra,
        }

    for f in plan.fields:
        rows.append(
            row(
                f.id,
                f.label,
                f.kind,
                _planned_text(f.value),
                "filled",
                options=f.options,
                tier=f.tier,
                source=f.source,
                alternates=f.alternates,
            )
        )
    for u in plan.unmapped:
        rows.append(
            row(
                u.id, u.label, u.kind, "", "parked", options=u.options, tier=u.tier, reason=u.reason
            )
        )
    for d in plan.draftable:
        rows.append(row(d.id, d.label, d.kind, "", "left blank", options=d.options, tier=d.tier))
    for s in plan.skipped:
        rows.append(row(s.id, s.label, "text", "", "skipped", tier=s.tier))
    for f in plan.files:
        attached = None
        try:
            attached = driver.attached_files(f.id)
        except Exception as exc:  # noqa: BLE001 - a read must never fail a run
            log.debug("%s: attached files unreadable (%s)", f.id, exc)
        rows.append(
            {
                "id": f.id,
                "label": f.label,
                "kind": "file",
                "group": "attached",
                "planned": f.path.name,
                "actual": ", ".join(attached) if attached else "",
                "matches": bool(attached) and f.path.name in attached,
                "prefilled": False,
                "before": "",
                "invalid": f.id in invalid,
                "options": [],
            }
        )
    return rows


def _planned_text(value) -> str:
    if isinstance(value, tuple):
        return ", ".join(str(v) for v in value)
    if isinstance(value, bool):
        return str(value)
    return str(value)


_CHOICE_KINDS = frozenset({"select", "react_select", "yesno", "radio_group", "checkbox_group"})


def _matches(field_id: str, kind: str, planned: str, actual: str) -> bool:
    """Whether the page holds what was planned, read the way each widget shows
    it: a single checkbox has no text to compare; Greenhouse's country field
    shows its dial code ("+1"); a phone box may reformat the number; a
    multi-select lists its picks in any order; a chooser may show a longer
    label that starts with the one picked (Ashby stacks name, country and
    domain). "Male" never matches "Female"."""
    if kind == "checkbox":
        return True
    if field_id == "country" and actual.strip().startswith("+"):
        return True
    p, a = " ".join(planned.casefold().split()), " ".join(actual.casefold().split())
    if "phone" in field_id.casefold():
        return re.sub(r"\D", "", p).endswith(re.sub(r"\D", "", a)[-10:]) if a else not p
    if kind == "checkbox_group":
        return {x.strip() for x in p.split(",")} == {x.strip() for x in a.split(",")}
    if kind in _CHOICE_KINDS and p and a and p != a:
        short, long = sorted((p, a), key=len)
        return long.startswith(short) and not long[len(short)].isalnum()
    return p == a


def form_check(manifest: list[dict]) -> list[str]:
    """Filled fields whose page value is not what was planned. Any one refuses
    the submit click: the form that goes out must be the form that was read."""
    return [
        f"{r['label'] or r['id']}: planned {r['planned']!r}, page holds {r['actual']!r}"
        for r in manifest
        if r.get("group") == "filled" and not r.get("matches")
    ]


def _reverify_fields(driver, plan: Plan) -> None:
    """Re-check every field immediately before the click, and re-apply any
    that drifted since `fill_plan` set it.

    A toggle can read back correctly when `_apply_field` sets it and be
    cleared again by submit time — a different field each retry, so not one
    bad selector but something external resetting fields asynchronously after
    the fill finished (a board's own resume-autofill, for one). The fix does
    not depend on the cause: this is the last chance to notice before the
    irreversible click.

    Every field kind gets checked against the *live* kind
    (`driver.resolve_kind`), not `field.kind` as planned. Ashby declares a
    field's API type up front (e.g. `select`) and only decides at render time
    whether that is a radio group, a combobox or a lone checkbox (see
    `AshbyBrowserDriver.resolve_kind`) — comparing against the planned kind
    silently skipped every drifted field Ashby renders that way, which is
    exactly the shape b9a009ad failed in ("Which best describes your
    experience with AI agents...", a `select`-planned field Ashby rendered
    and filled as a radio group).

    Text and textarea fields get the same treatment: b9a009ad's other two
    failures ("LinkedIn Profile", plain text) were never covered at all
    before this, which left every free-text answer with no safety net.

    `checkbox` (a lone consent tick) and `react_select` are deliberately left
    out — re-driving a react-select this close to the click means reopening
    and re-typing into a live widget, a bigger and riskier action than a
    single click or fill, and neither has been observed drifting. Nothing
    rules it out either, but the trade isn't worth it without evidence.
    """
    for field in plan.fields:
        kind = driver.resolve_kind(field.id, field.kind)

        if kind == "yesno":
            want = str(field.value)
            shown = driver.selected_label(field.id)
            if shown.strip().casefold() == want.strip().casefold():
                continue
            try:
                driver.set_yesno(field.id, want)
            except FillError:
                continue  # best-effort; an unresolved drift still fails at the click

        elif kind == "radio_group":
            want = (field.value if isinstance(field.value, tuple) else (field.value,))[0]
            shown = driver.checked_radio_label(field.id)
            if shown.strip().casefold() == str(want).strip().casefold():
                continue
            try:
                driver.check_radio_group(field.id, str(want))
            except FillError:
                continue

        elif kind == "checkbox_group":
            wanted = field.value if isinstance(field.value, tuple) else (field.value,)
            shown = {s.strip().casefold() for s in driver.checked_group_labels(field.id)}
            for label in wanted:
                if str(label).strip().casefold() in shown:
                    continue
                try:
                    driver.check_group_option(field.id, str(label))
                except FillError:
                    continue

        elif kind == "select":
            want = str(field.value)
            shown = driver.selected_option_label(field.id)
            if shown.strip() == want.strip():
                continue
            try:
                driver.select_native(field.id, want)
            except FillError:
                continue

        elif kind in ("text", "textarea", "date"):
            want = str(field.value)
            shown = driver.value_of(field.id)
            if shown.strip() == want.strip():
                continue
            try:
                driver.fill_text(field.id, want)
                if kind == "date":
                    # Ashby's react-datepicker opens on focus, which fill()
                    # triggers; nothing else ever dismisses it, so the
                    # leftover calendar overlay intercepts clicks on every
                    # field filled after this one.
                    driver.close()
            except FillError:
                continue


def submit(
    plan: Plan, result: FillResult, driver, answers: Answers | None = None, before_click=None
) -> None:
    """Click the one submit button on an already-filled form.

    The only path to a real click — `fill_plan` never reaches it, by
    construction (`test_nothing_ever_clicks_submit`). Raises
    `SubmitGuardError` instead of clicking when a required question is still
    unanswered, the fill itself failed a field, or the board renders no submit
    button at all.

    `before_click()` runs immediately before every click, so the caller can
    record the attempt before anything irreversible happens. Sets
    `result.outcome`.
    """
    if bank.unattended():
        raise SubmitGuardError(f"{bank.UNATTENDED_ENV}=1: no submit in an unattended run")
    blocking = blocking_questions(plan, result)
    if blocking:
        raise SubmitGuardError(f"required question(s) unresolved: {', '.join(blocking)}")
    if result.failures:
        raise SubmitGuardError(f"fill failure(s): {'; '.join(result.failures)}")
    if plan.submit_selector is None:
        raise SubmitGuardError("no submit button found on this form")
    if driver.submit_disabled_now(plan.submit_selector):
        raise SubmitGuardError("submit button is disabled")

    for attempt in range(SUBMIT_ATTEMPTS):
        # An upload still in flight makes the board refuse the click outright.
        # Observed live on Ashby, which says so in as many words.
        driver.settle()
        # Re-checked after `settle()`, not before: `settle()`'s networkidle
        # wait is exactly the window a field has been seen drifting in, and a
        # reverify done earlier in the function would miss a drift that
        # happens during the wait it is meant to guard against. Repeated on
        # every retry attempt too — a drift caught and fixed on attempt 1 is
        # not evidence attempt 2 is still safe.
        _reverify_fields(driver, plan)
        # Asked of the browser, immediately before the click, because this is
        # the one refusal that leaves no trace: constraint validation blocks
        # the form before the board's own script runs, so the click fires no
        # request, the page does not navigate, and the only signal is a
        # validation bubble no capture reads. Recorded as "submitted, no
        # confirmation seen" and transitioned to applied on a form the board
        # never received (387c1801: an unfilled self-ID date).
        invalid = driver.invalid_fields()
        if invalid:
            raise SubmitGuardError(
                "the browser refuses this form: "
                f"{', '.join(invalid)} {'is' if len(invalid) == 1 else 'are'} "
                "required and empty. Nothing was clicked."
            )
        # Registered fresh, right here, never earlier: Ashby's location combobox
        # queries the same GraphQL gateway on every keystroke, and
        # `_reverify_fields` above — or a prior attempt's post-refusal
        # reapply of a named combobox field — can retype one. Watching from
        # before the loop (or reusing one sink across attempts) would let that
        # unrelated POST get read as the submit's own signal, once the marker
        # stopped requiring the mutation's own op name (see
        # SUBMIT_REQUEST_MARKERS). A sink opened immediately before this
        # click can only ever catch traffic the click itself caused.
        attempt_requests: list[str] = []
        driver.watch_submit_requests(plan.ats, attempt_requests)
        if plan.requires_captcha:
            # Before the click, not after: the click is what opens the
            # challenge, and the wait below has to be able to tell this
            # solve's token from one an earlier interaction already banked.
            driver.clear_captcha_token()
        if before_click is not None:
            before_click()
        driver.click_submit(plan.submit_selector)
        if plan.requires_captcha:
            # Lever's submit is a JS-driven type="button", not a native form
            # submit (see lever.py) — the click above only opens the hCaptcha
            # challenge; the real POST fires from the captcha's own success
            # callback. So on a captcha board the click is NOT yet the
            # irreversible act: if the captcha is abandoned or times out,
            # nothing was sent (acea77ed: a timed-out captcha was recorded as
            # applied when the board never received anything).
            driver.wait_for_captcha()
        # The irreversible act: on a non-captcha board, the click itself; on
        # a captcha board, a captcha just solved. Record it before anything
        # else runs — a driver error or navigation hiccup past this point
        # must not erase the fact that an application went out.
        result.submitted = True
        # Judged only after the board has had time to answer. Reading straight
        # off the click caught pages mid-navigation and reported a perfectly
        # good submission as unconfirmed.
        driver.wait(SUBMIT_SETTLE_MS)

        # A board we can watch normally settles it outright: a request to its
        # submit endpoint is the strongest signal there is. But its absence at
        # this point is not yet proof of anything — see SUBMIT_REQUEST_EXTRA_WAIT_MS
        # — so a board we watch gets extra time for a late request to land
        # before its absence is treated as meaningful at all.
        if SUBMIT_REQUEST_MARKERS.get(plan.ats) and not attempt_requests:
            waited = 0
            while waited < SUBMIT_REQUEST_EXTRA_WAIT_MS and not attempt_requests:
                driver.wait(SUBMIT_REQUEST_POLL_MS)
                waited += SUBMIT_REQUEST_POLL_MS
            if not attempt_requests:
                # Still ambiguous, never proof of failure: `submitted` stays
                # true and this attempt is not retried on this signal alone — a
                # real submission whose request merely arrived even later would
                # otherwise get clicked a second time. Only an explicit refusal
                # below is a strong enough signal to justify clicking again.
                result.submit_error = (
                    f"clicked {plan.submit_selector} but no {plan.ats} submit "
                    f"request was observed — ambiguous, not re-clicked; verify "
                    f"by hand before retrying"
                )
                log.info("submit request marker not observed (attempt %d)", attempt + 1)

        # Folded into the aggregate only now, after this attempt's own
        # post-click window has closed — `result.confirmed` and the evidence
        # file read the aggregate, but nothing before this point ever
        # consulted it, so an earlier attempt's stray combobox traffic still
        # cannot count towards this attempt's signal.
        result.submit_requests.extend(attempt_requests)

        # Received and turned down. Final: another click would be judged the
        # same way, and on a duplicate it would be a second application.
        rejection = driver.submission_rejected()
        if rejection:
            result.submitted = False
            result.outcome = "rejected"
            result.submit_error = f"the board rejected the application: {rejection}"
            log.info("submit rejected: %s", rejection)
            break

        refusal = driver.submission_refused()
        if not refusal:
            break
        # The board *stated* it did not accept this click, so nothing was sent
        # and re-clicking cannot duplicate anything. Absence of a refusal still
        # means "assume submitted" — that default is what keeps duplicates off
        # the table, and it is not touched here.
        result.submitted = False
        result.submit_error = f"the board refused the click: {refusal}"
        log.info("submit refused (attempt %d): %s", attempt + 1, refusal)

        # Named fields were confirmed correct in the DOM — right value, right
        # radio checked — both right after the original fill and in the
        # post-refusal screenshot (b9a009ad). `_reverify_fields` above already
        # found nothing to fix for exactly this reason: a read-back that
        # already matches looks identical whether the board's own state is
        # fine or not. Forcing a fresh write on exactly what the board named,
        # rather than skipping because it reads correct, is the one recovery
        # that reacts to the board's real complaint instead of a guess made in
        # advance.
        named = driver.missing_field_labels()
        if named:
            wanted = {label.strip().casefold() for label in named}
            for missed in plan.fields:
                if missed.label.strip().casefold() not in wanted:
                    continue
                try:
                    outcome = _apply_field(driver, missed, result, answers)
                except FillError as exc:
                    log.info("re-apply after refusal failed for %s: %s", missed.id, exc)
                    continue
                outcome.note = "reapplied after the board named it missing"
                result.outcomes.append(outcome)

    # A recorded submit response is confirmation on its own — it is the board
    # accepting the application, not a page that happens to read like it did.
    # Deliberately `attempt_requests` (the last attempt only), not the
    # `result.submit_requests` aggregate: a refused first attempt's own
    # traffic — including a reapplied combobox's stray query to the same
    # endpoint — must not stand in for evidence about a silent second click.
    result.confirmed = result.submitted and (
        bool(attempt_requests) or driver.submission_confirmed()
    )
    if result.outcome != "rejected":
        if not result.submitted:
            result.outcome = "refused"
        elif result.confirmed:
            result.outcome = "accepted"
        elif SUBMIT_REQUEST_MARKERS.get(plan.ats):
            # A watched board fired no submit request in the whole wait: the
            # click reached nothing. Not tracked as applied; the ledger line
            # still stops a second automatic click until someone checks.
            result.outcome = "unsent"
        else:
            result.outcome = "ambiguous"
    result.evidence = capture_submit_evidence(driver, plan, result)


def capture_submit_evidence(driver, plan, result: FillResult | None = None) -> Path | None:
    """Write what the board rendered after the click, and return the text path.

    `CONFIRMATION_MARKERS` only covers wordings someone has read, so a
    submission it does not match is exactly the one worth keeping the page
    for. `result.submit_requests` is recorded alongside it: an empty list
    there means the marker genuinely never fired, not that no one checked.

    Diagnostics only. It runs after the click, never decides whether one
    happens, and every failure here is swallowed — losing a screenshot must not
    turn a submitted application into a reported error.
    """
    try:
        stamp = clock.now().strftime("%Y-%m-%dT%H-%M-%S")
        out_dir = paths.APPLICATIONS / "apply_runs" / "evidence"
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = out_dir / f"{stamp}_{plan.job_id or 'unknown'}"

        page = driver.page
        text = page.locator("body").inner_text(timeout=UPLOAD_READBACK_TIMEOUT)
        requests = result.submit_requests if result is not None else []
        note = (
            f"# post-submit capture\n"
            f"job_id: {plan.job_id}\n"
            f"company: {plan.company}\n"
            f"submit_requests: {requests!r}\n"
            f"ats: {plan.ats}\n"
            f"form_url: {plan.form_url}\n"
            f"landed_url: {page.url}\n"
            f"title: {page.title()}\n"
            f"markers_matched: {driver.submission_confirmed()}\n"
            f"\n--- rendered text ---\n{text}\n"
        )
        txt = stem.with_suffix(".txt")
        txt.write_text(note, encoding="utf-8")
        try:
            page.screenshot(path=str(stem.with_suffix(".png")), full_page=True)
        except Exception:  # noqa: BLE001 - text alone is still worth keeping
            log.debug("post-submit screenshot failed")
        return txt
    except Exception as exc:  # noqa: BLE001 - never let diagnostics fail a run
        log.warning("post-submit capture failed: %s", exc)
        return None


def run_one(
    plan: Plan,
    answers: Answers | None = None,
    *,
    sink: list | None = None,
    headless: bool = False,
    submit_after: bool = False,
    after=None,
    before_click=None,
) -> FillResult:
    """Open a browser, fill the form, and — only when `submit_after` is set —
    make the one guarded click, all inside the same session. A real
    submission needs the click on the page it just filled; a fresh browser
    would see none of that work.

    `after(result)` runs with the window still open, so a caller can report and
    let someone look at the form before it closes.

    `submit_after` is the caller's decision made once per invocation, never a
    config default; this function does not default it on, and a guard refusal is recorded in `result.submit_error` rather than
    raised, so one parked role in a queue does not kill the run.
    """
    # Built and published BEFORE the browser opens. `submitted` is the record
    # that an application went out, and a caller that only sees it via the
    # return value loses it to any exception — including a BaseException like
    # Ctrl-C, which no `except Exception` can catch. `sink` gives the caller
    # the same object this function is about to mutate.
    result = FillResult(form_url=plan.form_url)
    if sink is not None:
        sink.append(result)

    sync_playwright = _require_playwright()
    with sync_playwright() as p:
        context = _launch(p, headless=headless)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            driver = _driver_for(plan.ats, page)
            fill_plan(plan, driver, answers, result=result)
            # After the fill, before any click: the one moment the page holds
            # the finished form and nothing irreversible has happened.
            try:
                result.manifest = form_manifest(driver, plan, result)
                result.landed_url = page.url
                result.title = page.title()
            except Exception as exc:  # noqa: BLE001 - never block a submit
                log.warning("reading the form manifest failed: %s", exc)
            problems = form_check(result.manifest) if submit_after else []
            if problems:
                result.submit_error = "form check refused the click: " + "; ".join(problems)
            elif submit_after:
                try:
                    submit(plan, result, driver, answers, before_click=before_click)
                except SubmitGuardError as exc:
                    # Refusal — raised before the click, so `submitted` is
                    # still False and nothing went out.
                    result.submit_error = str(exc)
                except Exception as exc:  # noqa: BLE001
                    # Anything else may have fired after the click (or, on a
                    # captcha board, after the captcha resolved). `submit`
                    # already set `result.submitted` at the point the
                    # application actually went out, so that flag — not this
                    # handler — decides whether one did. A captcha that never
                    # resolves raises here with `submitted` still False.
                    result.submit_error = f"{type(exc).__name__}: {exc}"
            if after is not None:
                try:
                    after(result)
                except Exception as exc:  # noqa: BLE001
                    log.warning("after-fill callback failed: %s", exc)
        finally:
            # Teardown must never be the thing that loses a submission. A
            # window the user closed after the click makes this raise, and
            # that exception used to propagate out of `run_one` and discard
            # `result` — so `submitted` was never read and the role was
            # re-applied to on the next run.
            try:
                context.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("closing the browser context failed: %s", exc)
    return result
