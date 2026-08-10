"""Execute a plan against a real form. Fill only — there is no submit here.

The plan decided everything; this module types, selects, checks and uploads, and
refuses to paper over a widget that did not take the value. Two rules shape it:

**Config always wins.** Uploading a resume can trigger a server-side parse that
writes into fields already on the page. So the order is: attach first, re-read
every field, then overwrite from the plan regardless of what is sitting there.
Nothing parsed is trusted and nothing parsed is left in place — what goes out
has to be reproducible from `application_answers.yaml` alone.

**A react-select that did not stick is a failure, not a filled field.** It
accepts a typed string matching no option and ends up empty; required, that
fails at submit, and optional, it fails silently. Every one of them is asserted
after selection. Measured over 45 live boards, the required react-selects with
no option list to validate against beforehand are `country` (32),
`candidate-location` (16), `degree--0` (4), `discipline--0` (4) and
`school--0` (3) — so this assert is not an edge case, it is the main path.

`playwright` is an optional dependency (`uv sync --group apply`), and this and
`_launch` are the only places in `src/` that name the driver, so swapping in
patchright later is a one-line change.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from src import paths
from src.apply.answers import Answers, resolve
from src.apply.plan import FieldPlan, FilePlan, Plan
from src.apply.reconcile import MergedField, MergedOption

log = logging.getLogger(__name__)

USER_DATA_DIR = paths.REPO_ROOT / ".apply_profile"

FORM_SELECTOR = "#application-form"
# Attribute form, not "#id": a checkbox group's id carries a literal "[]".
FIELD = '{form} [id="{id}"]'

# react-select's rendered parts, as they appear on every board sampled.
SELECT_CONTAINER = ".select__container"
SELECT_OPTION = ".select__option"
SELECT_SINGLE_VALUE = ".select__single-value"
SELECT_MULTI_VALUE = ".select__multi-value__label"

# How long to wait for a listbox to open, in ms. Generous: these are remote
# taxonomies on some boards.
OPTION_TIMEOUT = 5000
FORM_TIMEOUT = 30000


class FillError(Exception):
    """A widget did not take the value, or the form is not what was planned."""


class SubmitGuardError(Exception):
    """Refused to click submit. The message names exactly what is still
    unresolved — this is the invariant that keeps a parked or half-filled role
    from ever going out."""


@dataclass
class FieldOutcome:
    id: str
    action: str             # filled | attached | failed
    before: str = ""        # what was in the field before we wrote to it
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

    submitted: bool = False
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


def _require_playwright():
    """Import the driver, or explain how to install it. Call-time, so the module
    imports fine without the group and `tests/test_profile_templates.py`'s AST
    walk keeps working."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "ERROR: playwright not installed. Run `uv sync --group apply` "
            "then `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 uv run playwright install chrome`."
        ) from exc
    return sync_playwright


def _launch(p, *, headless: bool = False):
    """The one place a browser is constructed.

    A separate, empty profile: `channel="chrome"` selects the system Chrome
    binary, not the user's session. Pointing this at the real profile would
    expose every logged-in cookie and is refused by Chrome anyway.
    """
    return p.chromium.launch_persistent_context(
        user_data_dir=str(USER_DATA_DIR),
        channel="chrome",
        headless=headless,
    )


class BrowserDriver:
    """Everything the fill sequence does to a page, in one swappable object.

    Split out so `fill_plan` — the ordering, the overwrite rule, the asserts —
    is testable without a browser.
    """

    def __init__(self, page):
        self.page = page

    def _locator(self, field_id: str):
        return self.page.locator(FIELD.format(form=FORM_SELECTOR, id=field_id))

    def goto(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded")
        self.page.wait_for_selector(FORM_SELECTOR, timeout=FORM_TIMEOUT)

    def settle(self) -> None:
        """Let an upload's XHR finish before anything is read back."""
        try:
            self.page.wait_for_load_state("networkidle", timeout=OPTION_TIMEOUT)
        except Exception:  # noqa: BLE001 - a busy page is not a failure
            log.debug("networkidle did not settle; continuing")

    def value_of(self, field_id: str) -> str:
        el = self._locator(field_id)
        if el.count() == 0:
            return ""
        try:
            value = el.first.input_value(timeout=1000)
        except Exception:  # noqa: BLE001 - not an <input>, e.g. a fieldset
            value = ""
        if value:
            return value
        # A react-select keeps its input empty and shows the choice in a sibling.
        return self.selected_label(field_id)

    def selected_label(self, field_id: str) -> str:
        container = self._locator(field_id).locator(f"xpath=ancestor::*[contains(@class,"
                                                    f"'select__container')][1]")
        if container.count() == 0:
            return ""
        parts = []
        for selector in (SELECT_SINGLE_VALUE, SELECT_MULTI_VALUE):
            nodes = container.first.locator(selector)
            parts.extend(nodes.nth(i).inner_text().strip() for i in range(nodes.count()))
        return ", ".join(p for p in parts if p)

    def is_expanded(self, field_id: str) -> bool:
        return self._locator(field_id).first.get_attribute("aria-expanded") == "true"

    def set_files(self, field_id: str, path: Path) -> None:
        self._locator(field_id).first.set_input_files(str(path))

    def fill_text(self, field_id: str, value: str) -> None:
        el = self._locator(field_id).first
        el.fill("")
        el.fill(value)

    def set_checkbox(self, field_id: str, checked: bool) -> None:
        el = self._locator(field_id).first
        el.check() if checked else el.uncheck()

    def open_options(self, field_id: str) -> tuple[str, ...]:
        """Click a react-select open and read its full, unfiltered option list.

        The only way to know what one offers. `country`, `candidate-location`,
        the education selects and `hispanic_ethnicity` carry no option list in
        either the API payload or the served HTML — it is built client-side.
        """
        self._locator(field_id).first.click()
        return self.visible_options()

    def visible_options(self) -> tuple[str, ...]:
        """Whatever the open listbox currently shows. After typing this is the
        filtered set, not everything the widget has."""
        try:
            self.page.wait_for_selector(SELECT_OPTION, timeout=OPTION_TIMEOUT)
        except Exception:  # noqa: BLE001 - an empty listbox is an answer
            return ()
        nodes = self.page.locator(SELECT_OPTION)
        return tuple(nodes.nth(i).inner_text().strip() for i in range(nodes.count()))

    def check_group_option(self, field_id: str, label: str) -> None:
        """Tick one box in a checkbox fieldset, matched on its visible label."""
        fieldset = self._locator(field_id).first
        boxes = fieldset.locator('input[type="checkbox"]')
        for i in range(boxes.count()):
            box = boxes.nth(i)
            box_id = box.get_attribute("id") or ""
            labels = fieldset.locator(f'label[for="{box_id}"]') if box_id else None
            text = labels.first.inner_text().strip() if labels and labels.count() else ""
            if text.casefold() == label.casefold():
                box.check()
                return
        raise FillError(f"{field_id}: no checkbox labelled {label!r}")

    def type_into(self, field_id: str, value: str) -> None:
        """Character-wise: the dropdown is keystroke-triggered, so fill() leaves
        it closed and the suggestion list never appears."""
        el = self._locator(field_id).first
        el.click()
        el.fill("")
        el.type(value, delay=25)

    def click_option(self, label: str) -> bool:
        options = self.page.locator(SELECT_OPTION)
        for i in range(options.count()):
            node = options.nth(i)
            if node.inner_text().strip().casefold() == label.casefold():
                node.click()
                return True
        return False

    def close(self) -> None:
        self.page.keyboard.press("Escape")

    def submit_disabled_now(self, selector: str) -> bool:
        """Re-read `aria-disabled` live — `plan.submit_disabled` is a scan-time
        snapshot and every field write since then can have changed it."""
        return self.page.locator(selector).first.get_attribute("aria-disabled") == "true"

    def click_submit(self, selector: str) -> None:
        self.page.locator(selector).first.click()


def _merged_from(field: FieldPlan, options: tuple[str, ...]) -> MergedField:
    """The planned field as reconcile would have produced it, had the options
    been readable without a browser."""
    return MergedField(
        id=field.id, name=field.name, label=field.label, required=field.required,
        kind=field.kind, section=field.section, multi=field.multi,
        options=tuple(MergedOption(label=o) for o in options),
    )


def _relabel(field: FieldPlan, options: tuple[str, ...],
             answers: Answers | None) -> str | None:
    """Re-resolve a field against the options actually on screen. Same rule as
    the plan used, better input — never a fuzzy match on the planned string."""
    if answers is None:
        return None
    resolution = resolve(_merged_from(field, options), answers)
    if resolution.action != "fill":
        return None
    value = resolution.value
    return value[0] if isinstance(value, tuple) and value else (
        value if isinstance(value, str) else None
    )


def _select(driver, field: FieldPlan, label: str, result: FillResult,
            answers: Answers | None = None) -> str:
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
    shown = driver.selected_label(field.id)
    if not shown:
        raise FillError(f"{field.id}: selected {label!r} but the field is empty")
    return shown


def _apply_field(driver, field: FieldPlan, result: FillResult,
                 answers: Answers | None = None) -> FieldOutcome:
    before = driver.value_of(field.id)

    if field.kind == "checkbox":
        driver.set_checkbox(field.id, bool(field.value))
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

    driver.fill_text(field.id, str(field.value))
    after = driver.value_of(field.id)
    if after.strip() != str(field.value).strip():
        raise FillError(f"{field.id}: wrote {field.value!r} but the field reads {after!r}")
    return FieldOutcome(field.id, "filled", before, after)


def _attach(driver, upload: FilePlan) -> FieldOutcome:
    if not upload.path.is_file():
        raise FillError(f"{upload.id}: {upload.path} is gone")
    driver.set_files(upload.id, upload.path)
    return FieldOutcome(upload.id, "attached", "", upload.path.name)


def fill_plan(plan: Plan, driver, answers: Answers | None = None) -> FillResult:
    """Drive one plan to a filled — never submitted — form.

    Attachments go first and the page is allowed to settle, because an upload
    can rewrite fields. Everything else is then written over whatever is there.
    """
    result = FillResult(form_url=plan.form_url)
    driver.goto(plan.form_url)

    for upload in plan.files:
        try:
            result.outcomes.append(_attach(driver, upload))
        except FillError as exc:
            result.failures.append(str(exc))
            result.outcomes.append(FieldOutcome(upload.id, "failed", note=str(exc)))
    if plan.files:
        driver.settle()

    for field in plan.fields:
        try:
            result.outcomes.append(_apply_field(driver, field, result, answers))
        except FillError as exc:
            result.failures.append(str(exc))
            result.outcomes.append(FieldOutcome(field.id, "failed", note=str(exc)))

    _probe_parked_selects(driver, plan, result, answers)
    return result


def _probe_parked_selects(driver, plan: Plan, result: FillResult,
                          answers: Answers | None = None) -> None:
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
        if parked.kind != "react_select":
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
            id=parked.id, name=parked.id, label=parked.label,
            required=parked.required, kind=parked.kind, section=parked.section,
            multi=False, options=tuple(MergedOption(label=o) for o in options),
        )
        resolution = resolve(field, answers)
        if resolution.action != "fill":
            continue
        plan_field = FieldPlan(
            id=parked.id, name=parked.id, label=parked.label, kind=parked.kind,
            section=parked.section, required=parked.required, multi=False,
            value=resolution.value, tier=resolution.tier,
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

    `plan.unmapped` is required-only by construction — `answers.resolve`
    demotes an unmatched *optional* question to skip/draftable before it can
    ever reach `Plan.unmapped` (see `resolve()`'s `_park`/`_skip` call sites),
    so nothing here has to re-check `.required`; it would always be true.
    """
    recovered = set(result.recovered)
    return tuple(u.id for u in plan.unmapped if u.id not in recovered)


def submit(plan: Plan, result: FillResult, driver) -> None:
    """Click the one submit button on an already-filled form.

    The only path to a real click — `fill_plan` never reaches it, by
    construction (`test_nothing_ever_clicks_submit`). Raises
    `SubmitGuardError` instead of clicking when a required question is still
    unanswered, the fill itself failed a field, or the board renders no submit
    button at all.
    """
    blocking = blocking_questions(plan, result)
    if blocking:
        raise SubmitGuardError(f"required question(s) unresolved: {', '.join(blocking)}")
    if result.failures:
        raise SubmitGuardError(f"fill failure(s): {'; '.join(result.failures)}")
    if plan.submit_selector is None:
        raise SubmitGuardError("no submit button found on this form")
    if driver.submit_disabled_now(plan.submit_selector):
        raise SubmitGuardError("submit button is disabled")
    driver.click_submit(plan.submit_selector)


def run_one(plan: Plan, answers: Answers | None = None, *,
            headless: bool = False, submit_after: bool = False, after=None) -> FillResult:
    """Open a browser, fill the form, and — only when `submit_after` is set —
    make the one guarded click, all inside the same session. A real
    submission needs the click on the page it just filled; a fresh browser
    would see none of that work.

    `after(result)` runs with the window still open, so a caller can report and
    let someone look at the form before it closes.

    `submit_after` is the caller's decision made once per invocation (§13 —
    `--submit` is never a config default); this function does not default it
    on, and a guard refusal is recorded in `result.submit_error` rather than
    raised, so one parked role in a queue does not kill the run.
    """
    sync_playwright = _require_playwright()
    with sync_playwright() as p:
        context = _launch(p, headless=headless)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            driver = BrowserDriver(page)
            result = fill_plan(plan, driver, answers)
            if submit_after:
                try:
                    submit(plan, result, driver)
                    result.submitted = True
                except SubmitGuardError as exc:
                    result.submit_error = str(exc)
            if after is not None:
                after(result)
        finally:
            context.close()
    return result


def fill(plan: Plan, answers: Answers | None = None, *,
         headless: bool = False, after=None) -> FillResult:
    """Fill only, never submit — `run_one` with `submit_after` left at its
    default. Kept as its own name because `apply fill` (no `--submit` in
    reach at all) is a distinct, deliberately narrower CLI surface than
    `apply run`.
    """
    return run_one(plan, answers, headless=headless, after=after)
