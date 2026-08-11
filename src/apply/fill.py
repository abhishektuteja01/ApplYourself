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
import time
from dataclasses import dataclass, field as dc_field, replace
from datetime import datetime
from pathlib import Path

from src import paths
from src.apply import ashby
from src.apply.answers import Answers, resolve
from src.apply.plan import FieldPlan, FilePlan, Plan
from src.apply.reconcile import MergedField, MergedOption

log = logging.getLogger(__name__)

USER_DATA_DIR = paths.REPO_ROOT / ".apply_profile"

FORM_SELECTOR = "#application-form"
# Attribute form, not "#id": a checkbox group's id carries a literal "[]".
FIELD = '{form} [id="{id}"]'
# Lever fields carry no id at all — MergedField.id IS the DOM `name` (lever.py
# never aliases the two apart, exactly so this selector keeps working).
FIELD_BY_NAME = '{form} [name="{id}"]'

# react-select's rendered parts, as they appear on every board sampled.
SELECT_CONTAINER = ".select__container"
SELECT_OPTION = ".select__option"
SELECT_SINGLE_VALUE = ".select__single-value"
SELECT_MULTI_VALUE = ".select__multi-value__label"

# Positive acknowledgements only. Matching none of these means "unconfirmed",
# never "failed" — see `BrowserDriver.submission_confirmed`. Provisional until
# a real submission is observed; extend, do not invert.
CONFIRMATION_MARKERS = (
    "text=/thank you for applying/i",
    "text=/application (?:has been )?(?:submitted|received)/i",
    "text=/your application was (?:submitted|sent)/i",
    "text=/we(?:'ve| have) received your application/i",
)

# How long to wait for a listbox to open, in ms. Generous: these are remote
# taxonomies on some boards.
OPTION_TIMEOUT = 5000
FORM_TIMEOUT = 30000
# Short: the input is often gone by now, and waiting the default 30s per
# upload for a node that will never come back stalls a whole queue run.
UPLOAD_READBACK_TIMEOUT = 2000
# How long to wait for the widget's own file chooser to open after the click.
UPLOAD_CHOOSER_TIMEOUT = 5000
# How long to wait for the widget to show a filename or an error. The upload is
# still in flight when the check runs, so this is a poll, not a single read.
UPLOAD_VERDICT_TIMEOUT = 15000
# Text a board renders when it refused a file. Unlike CONFIRMATION_MARKERS,
# absence here is NOT treated as failure either — this list only ever turns an
# unverifiable upload into a definite one. The asymmetry runs the opposite way
# from submission: a false "submitted" duplicates an application, but a false
# "attached" sends one with no resume, which costs the role outright.
UPLOAD_ERROR_MARKERS = (
    "uploadFile",
    "Cannot read properties",
    "There was an error",
)
# Text a board renders when it declined the submit click itself. Measured, not
# guessed: the first entry is Ashby's, verbatim, from a real refused click.
#
# This is the board stating it did not accept the click, which is why acting on
# it is safe where inverting CONFIRMATION_MARKERS would not be. Absence still
# means "assume submitted"; only an explicit refusal reopens the question.
SUBMIT_REFUSED_MARKERS = (
    "please try again when they",       # "...when they're finished"
    "updating your application",
    # Measured live on Take2 (Ashby): the submit request fired (a 200 to
    # ApiSubmitSingleApplicationFormAction — GraphQL's success-status
    # convention, not the application's), but the board's own validation
    # rejected it and re-rendered the form with this banner. Without this
    # marker `submission_refused()` found nothing and the request-marker
    # guard alone let it through: a request marker proves the click did
    # something, never that the board accepted it.
    "your form needs corrections",
    "missing entry for required field",
)
# How many times to click when the board keeps refusing. A refusal is positive
# evidence nothing was sent, so a retry cannot duplicate an application.
SUBMIT_ATTEMPTS = 2
# The request a board fires when it actually accepts an application. Measured
# from a real manual submit, unlike CONFIRMATION_MARKERS: a 2xx response to one
# of these is evidence the click did something, where rendered text only ever
# said the page looked right. Keyed by ats, and absent for a board nobody has
# watched yet — an unknown board falls back to the text markers.
SUBMIT_REQUEST_MARKERS = {
    "ashby": ("ApiSubmitSingleApplicationFormAction",),
    # Measured on observe.ai: `boards.greenhouse.io/embed/<board>/jobs/<id>`.
    # `job-boards.greenhouse.io/embed/job_app` — the iframe's own GET, loaded
    # once per page view — contains this same substring (it's the `job-`
    # prefix, not a different path), so the URL alone is not unique. Gated by
    # SUBMIT_REQUEST_METHOD below.
    "greenhouse": ("boards.greenhouse.io/embed/",),
}
# Per-ats method a matched marker must also carry. Ashby's op= name never
# appears on any request but the real submit, so it needs no gate; absent
# here means "URL match alone is enough."
SUBMIT_REQUEST_METHOD = {
    "greenhouse": "POST",
}
# How long to let the board answer the click before reading the page.
SUBMIT_SETTLE_MS = 5000
# How often to re-check a server-backed listbox while waiting for its results.
_OPTION_POLL_MS = 250
# How long to let a react-select's chosen value land before calling it stuck.
# Ashby's React state update can lag a click by a beat (§ live on Cohere).
SELECT_SETTLE_TIMEOUT = 2000


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
    """The submit button was clicked. Set the instant the click returns, before
    anything post-click can fail — an exception after the click must not lose
    the fact that we applied, or the role stays `tailored` and the next run
    applies to the same board again."""

    confirmed: bool = False
    """The board positively acknowledged the application. Absence is NOT
    evidence of failure: no confirmation page has been observed live yet, so
    this only ever goes true on a positive match. A submitted-but-unconfirmed
    role is reported as such and still transitions to `applied`, because a
    duplicate application is the worse failure."""

    submit_requests: list[str] = dc_field(default_factory=list)
    """2xx responses to the board's own submit endpoint, seen after the click.
    The one signal that cannot be faked by a page that merely looks right —
    Ashby's button rendered, enabled and clickable, fired nothing at all."""

    evidence: Path | None = None
    """Where the post-submit page was written. The record that lets
    `CONFIRMATION_MARKERS` be extended from what a board actually renders
    rather than from another guess."""

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


def _is_numeric(value: str) -> bool:
    """What `input[type=number]` will accept. Deliberately not a currency or
    unit parser — stripping "$" or "k" off an answer would submit a number the
    user never wrote."""
    try:
        float(str(value).strip())
    except (TypeError, ValueError):
        return False
    return True


class BrowserDriver:
    """Everything the fill sequence does to a page, in one swappable object.

    Split out so `fill_plan` — the ordering, the overwrite rule, the asserts —
    is testable without a browser.
    """

    def __init__(self, page):
        self.page = page

    def _locator(self, field_id: str):
        return self.page.locator(FIELD.format(form=FORM_SELECTOR, id=field_id))

    def resolve_kind(self, field_id: str, planned: str) -> str:
        """The kind to actually drive this field as.

        Greenhouse and Lever declare a widget and render it, so the planned
        kind stands. Ashby does not: one API type renders as a radio group or a
        combobox depending on how many options it carries, and the cutoff is
        Ashby's own UI decision, not something the payload states. Overridden
        there to read the live DOM instead of guessing a threshold.
        """
        return planned

    def set_yesno(self, field_id: str, label: str) -> None:
        """Boards that render a boolean as a two-button toggle rather than a
        checkbox. Only Ashby does; the base has no such widget."""
        raise FillError(f"{field_id}: this board has no yes/no toggle widget")

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

    def _upload_group(self, field_id: str):
        """The widget wrapping this file input, or an empty locator."""
        return self.page.locator(
            f"div.file-upload[aria-labelledby='upload-label-{field_id}']"
        )

    def set_files(self, field_id: str, path: Path) -> None:
        """Upload through the widget's own control rather than the input.

        Greenhouse renders `input[type=file]` as `visually-hidden` and wires
        the upload to an Attach button. Writing straight to the input fires the
        change handler against a component the click would have initialized, so
        the page renders "Cannot read properties of undefined (reading
        'uploadFile')" and the file never leaves the machine — while
        `input.files` still reads back populated, which is why this looked
        green for so long.

        Falls back to the direct write whenever the widget or its button is
        absent, or the click opens no chooser. That fallback is the path Lever
        and Ashby pass on today, and it stays their path untouched.
        """
        group = self._upload_group(field_id)
        if group.count():
            button = group.first.get_by_role("button", name="Attach")
            if button.count():
                try:
                    with self.page.expect_file_chooser(
                        timeout=UPLOAD_CHOOSER_TIMEOUT
                    ) as chooser:
                        button.first.click()
                    chooser.value.set_files(str(path))
                    return
                except Exception:  # noqa: BLE001 - no chooser; try the input
                    log.debug("%s: Attach opened no file chooser", field_id)
        self._locator(field_id).first.set_input_files(str(path))

    def upload_shows(self, field_id: str, filename: str) -> bool | None:
        """True when the widget displays `filename`, False when it displays an
        upload error, None when neither can be read.

        This exists because `attached_files` cannot answer the question.
        `set_input_files` populates `input.files` whether or not the board
        accepted the file, so the input reads back correct on a failed upload.
        Verified live on Greenhouse: the widget resets to its Attach/Dropbox
        prompt and renders the error, while the input still holds the name.
        """
        group = self._upload_group(field_id)
        if not group.count():
            return None
        # Polled, not read once. The upload is still in flight when `_attach`
        # asks, so a single read shows neither the filename nor the error and
        # every upload would come back unverified — including the failures this
        # check exists to catch.
        deadline = time.monotonic() + UPLOAD_VERDICT_TIMEOUT / 1000
        while True:
            try:
                text = group.first.inner_text(timeout=UPLOAD_READBACK_TIMEOUT)
            except Exception:  # noqa: BLE001 - detached/re-rendered node
                return None
            if filename in text:
                return True
            if any(marker in text for marker in UPLOAD_ERROR_MARKERS):
                return False
            if time.monotonic() >= deadline:
                return None
            self.page.wait_for_timeout(_OPTION_POLL_MS)

    def submission_confirmed(self) -> bool:
        """Positive evidence that the board accepted the application.

        Absence is deliberately NOT failure. `result.submitted` is already true
        by the time this runs, and no live confirmation page has been observed
        yet (phase 1's real-submission checkpoint is still deferred), so the
        marker list is provisional. A real submission should *extend* it —
        never invert the default, which would turn "assume submitted" into
        "assume failed" and reintroduce the duplicate-application path.
        """
        for marker in CONFIRMATION_MARKERS:
            try:
                if self.page.locator(marker).count() > 0:
                    return True
            except Exception:  # noqa: BLE001 - a bad marker is not a failure
                continue
        return False

    def watch_submit_requests(self, ats: str, sink: list[str]) -> None:
        """Record 2xx responses to the board's submit endpoint into `sink`.

        Registered before the click, because the response is the only proof the
        click did anything. A board nobody has watched yet has no marker, so
        the sink stays empty and confirmation falls back to page text.
        """
        markers = SUBMIT_REQUEST_MARKERS.get(ats, ())
        if not markers:
            return
        required_method = SUBMIT_REQUEST_METHOD.get(ats)

        def on_response(response) -> None:
            if response.status >= 300:
                return
            if not any(marker in response.url for marker in markers):
                return
            if required_method and response.request.method != required_method:
                return
            sink.append(f"{response.status} {response.url}")

        try:
            self.page.on("response", on_response)
        except Exception:  # noqa: BLE001 - a driver without events is not a failure
            log.debug("could not watch responses for %s", ats)

    def wait(self, ms: int) -> None:
        """Let the page act. Separate from `settle` because a board answering a
        submit click may never go network-idle."""
        self.page.wait_for_timeout(ms)

    def submission_refused(self) -> str:
        """The board's own words for declining the click, or "".

        Read after the click, unlike `submission_confirmed`'s optimism: this
        one turns a recorded submission back off, so it only ever fires on text
        the board actually rendered.
        """
        try:
            text = self.page.locator("body").inner_text(
                timeout=UPLOAD_READBACK_TIMEOUT)
        except Exception:  # noqa: BLE001 - unreadable page is not a refusal
            return ""
        lowered = text.casefold()
        for marker in SUBMIT_REFUSED_MARKERS:
            if marker.casefold() in lowered:
                return marker
        return ""

    def attached_files(self, field_id: str) -> tuple[str, ...] | None:
        """Filenames the input holds, or None if that cannot be determined.

        `input_value()` refuses a file input, so this reads `.files` directly.
        A read, not a write, so §2b's "setting .value will not update React
        state" caveat does not apply.

        **None and () mean different things, and conflating them blocks real
        submissions.** Verified live on a Greenhouse board: uploading to
        `cover_letter` detaches the input — React replaces the subtree — so
        the node is gone a moment later even though the file landed. Only a
        node that is present *and* readable *and* empty is evidence of a
        failed upload; anything else is unverifiable, which is not the same
        as failed.
        """
        try:
            el = self._locator(field_id)
            if el.count() == 0:
                return None
            names = el.first.evaluate(
                "node => node.files ? Array.from(node.files, f => f.name) : []",
                timeout=UPLOAD_READBACK_TIMEOUT,
            )
        except Exception:  # noqa: BLE001 - detached/re-rendered node
            return None
        return tuple(names or ())

    def fill_text(self, field_id: str, value: str) -> None:
        el = self._locator(field_id).first
        # `input[type=number]` refuses non-numeric text, and Playwright raises
        # a plain Error rather than a FillError — which escapes the per-field
        # isolation in `fill_plan` and takes the whole role down with it. Seen
        # live on an Ashby compensation question whose configured answer is a
        # sentence. Refuse it here instead, so it is one failed field.
        if (el.get_attribute("type") or "") == "number" and not _is_numeric(value):
            raise FillError(
                f"{field_id}: the board wants a number and the answer is text: {value!r}"
            )
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

    def select_native(self, field_id: str, label: str) -> None:
        """A real `<select>` — no typing, no listbox, `select_option()` just
        works. Lever's fields carry no react-select at all (§12a)."""
        self._locator(field_id).first.select_option(label=label)

    def selected_option_label(self, field_id: str) -> str:
        """The visible text of the chosen `<option>` — `input_value()` would
        return its `value` attribute instead, which Lever's own options don't
        always match (the veteran options are full sentences with short
        internal values)."""
        checked = self._locator(field_id).first.locator("option:checked")
        return checked.first.inner_text().strip() if checked.count() else ""

    def _radio_options(self, field_id: str):
        """Yield `(radio_locator, visible_label)` for every radio in the
        same-named group — the shared read used by both `check_radio_group`
        (write) and `checked_radio_label` (read-back)."""
        group = self.page.locator(f'input[type="radio"][name="{field_id}"]')
        for i in range(group.count()):
            radio = group.nth(i)
            text = radio.locator(
                'xpath=following-sibling::span[contains(@class,"eeo-option-text")][1]'
            )
            label = text.first.inner_text().strip() if text.count() else ""
            yield radio, label

    def check_radio_group(self, field_id: str, label: str) -> None:
        """One radio in a same-named group, matched on its visible label —
        Lever's EEOC race question (§12a).

        `label` is stripped before comparing everywhere in this file, not just
        the DOM side: a work-authorization option's own rendered text can
        carry a trailing space (Ashby's "Are you authorized to work in the
        country you currently reside in?" renders "Yes " / "No ", per
        `tests/apply/fixtures/work_auth_labels.jsonl`), and a configured
        answer copied from that corpus carries the same space. The DOM text
        was already stripped before this comparison; the answer was not,
        which is what let "yes " fail to match "yes" live on Cohere.
        """
        for radio, text in self._radio_options(field_id):
            if text.casefold() == label.strip().casefold():
                radio.check()
                return
        raise FillError(f"{field_id}: no radio labelled {label!r}")

    def checked_radio_label(self, field_id: str) -> str:
        """The visible label of whichever radio in the group is currently
        checked, or "" if none is."""
        for radio, text in self._radio_options(field_id):
            if radio.is_checked():
                return text
        return ""

    def wait_for_captcha(self, timeout_ms: int = 600_000) -> None:
        """Block until a human solves the hCaptcha challenge Lever's own JS
        just opened — proxied by the hidden response token going non-empty.
        Unattended submission is not possible on a board that renders one
        (§12a, §12c): this is the wait, not a bypass."""
        self.page.wait_for_function(
            "document.getElementById('hcaptchaResponseInput')?.value?.length > 0",
            timeout=timeout_ms,
        )

    def _checkbox_options(self, field_id: str):
        """Yield `(checkbox_locator, visible_label)` for every box in the
        fieldset — the shared read used by both `check_group_option` (write)
        and `checked_group_labels` (read-back)."""
        fieldset = self._locator(field_id).first
        boxes = fieldset.locator('input[type="checkbox"]')
        for i in range(boxes.count()):
            box = boxes.nth(i)
            box_id = box.get_attribute("id") or ""
            labels = fieldset.locator(f'label[for="{box_id}"]') if box_id else None
            text = labels.first.inner_text().strip() if labels and labels.count() else ""
            yield box, text

    def check_group_option(self, field_id: str, label: str) -> None:
        """Tick one box in a checkbox fieldset, matched on its visible label."""
        for box, text in self._checkbox_options(field_id):
            if text.casefold() == label.strip().casefold():
                box.check()
                return
        raise FillError(f"{field_id}: no checkbox labelled {label!r}")

    def checked_group_labels(self, field_id: str) -> tuple[str, ...]:
        """The visible labels of every box in the fieldset that is currently
        checked."""
        return tuple(text for box, text in self._checkbox_options(field_id) if box.is_checked())

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
            if node.inner_text().strip().casefold() == label.strip().casefold():
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


class LeverBrowserDriver(BrowserDriver):
    """Same sequence, different selector: Lever's fields carry no `id`
    attribute at all, so every lookup is by `name` instead (§12a)."""

    def _locator(self, field_id: str):
        return self.page.locator(FIELD_BY_NAME.format(form=FORM_SELECTOR, id=field_id))

    def _checkbox_options(self, field_id: str):
        """Lever has no fieldset wrapper: the name selector already resolves to
        the checkboxes themselves, so the base implementation's
        `.locator('input[type=checkbox]')` searches *inside* an <input> and
        finds nothing. Match on the option's own value/label instead.
        """
        boxes = self._locator(field_id)
        for i in range(boxes.count()):
            box = boxes.nth(i)
            value = box.get_attribute("value") or ""
            text = ""
            box_id = box.get_attribute("id")
            if box_id:
                lab = self.page.locator(f'label[for="{box_id}"]')
                if lab.count():
                    text = lab.first.inner_text().strip()
            # `value` participates in the match (a Lever board can leave the
            # label empty), but only `text` is fit to report back — reporting
            # a raw option value as the "shown" label would never match a
            # configured answer that names the human-readable text instead.
            yield box, (text or value)

    def check_group_option(self, field_id: str, label: str) -> None:
        for box, text in self._checkbox_options(field_id):
            value = box.get_attribute("value") or ""
            if label.strip().casefold() in (value.casefold(), text.casefold()):
                box.check()
                return
        raise FillError(f"{field_id}: no checkbox labelled {label!r}")


class AshbyBrowserDriver(BrowserDriver):
    """Ashby renders none of Greenhouse's markup, so most selectors change.

    Four differences drive every override here, all observed live:

    1. **There is no `<form>` element** and no stable control `id` for a
       combobox. `data-field-path` on the field-entry wrapper is the id space
       the scanner already speaks, so every lookup starts there.
    2. **The API type does not name the widget.** One `select` renders as a
       radio group at 1/3/5 options and as a combobox at 11/24/194 — a UI
       threshold Ashby owns and can change. `resolve_kind` reads the DOM.
    3. **A combobox does not open on clicking its input** — `aria-expanded`
       stays false. The chevron button opens it, and the panel it opens is a
       document-level `[role=listbox]`, not a descendant of the field.
    4. **A boolean is two buttons plus a display-only checkbox.** The hidden
       checkbox reads false both when untouched and when No is chosen, so it
       cannot express the answer; the chosen button's `_active_` class can.
    """

    ENTRY = '[data-field-path="{id}"]'
    #: Controls that carry a value. Deliberately not `button` — the chevron.
    CONTROL = "input, textarea, select"
    #: The chevron that opens a combobox. Its class hash changes on deploy, so
    #: it is found positionally: the only button inside a combobox's field.
    COMBOBOX = 'input[role="combobox"]'
    #: Floated to document level, so it is never scoped to the field entry.
    LISTBOX = '[role="listbox"] [role="option"]'
    #: Ashby's CSS-module hashes change on every deploy (`_active_1svni_57`),
    #: so match the stable prefix. Absence reads as "not selected", which
    #: parks — never as a silent success. Always tag-scoped to `button`: an
    #: open listbox marks its highlighted option `_active_` too, and that is a
    #: `div` the user has not chosen.
    ACTIVE = '[class*="_active_"]'

    def _entry(self, field_id: str):
        """The field-entry wrapper — what a group or a combobox is found under.

        The two file fields reach here under the canonical `resume` /
        `cover_letter` ids that `ashby.py` aliases them to, but the DOM still
        keys them by systemfield path. Undo the alias, or every Ashby upload
        waits out the full locator timeout on a field that is on the page.
        """
        path = ashby.DOM_FIELD_PATHS.get(field_id, field_id)
        return self.page.locator(self.ENTRY.format(id=path)).first

    def _upload_group(self, field_id: str):
        """Ashby's field entry doubles as the upload widget — a landed file
        renders as "<name> / Replace" inside it. Pointing `upload_shows` here
        gets Ashby the same real verification Greenhouse has, instead of the
        `input.files` readback that reports success on a refused upload."""
        return self._entry(field_id)

    def _locator(self, field_id: str):
        """The value-carrying control inside the entry.

        Text, file and textarea fields also carry `id` == the field path, but
        going through the entry works for every kind including the combobox,
        which carries neither `id` nor `name`.
        """
        return self._entry(field_id).locator(self.CONTROL)

    def goto(self, url: str) -> None:
        """No `<form>` to wait for — the base would time out on every board.
        The first field entry is the equivalent readiness signal, and it only
        exists once the client-side render has run."""
        self.page.goto(url, wait_until="domcontentloaded")
        self.page.wait_for_selector("[data-field-path]", timeout=FORM_TIMEOUT)

    def resolve_kind(self, field_id: str, planned: str) -> str:
        """What this field renders as right now, falling back to the plan.

        Only widget shape is read, never labels or options — the answer is
        still whatever the plan resolved. A field that is not on the page (a
        conditional question, say) keeps its planned kind and fails later in
        the ordinary way rather than being silently reinterpreted here.
        """
        entry = self._entry(field_id)
        if entry.count() == 0:
            return planned
        if entry.locator(self.COMBOBOX).count():
            # Both flavours — the enumerated one and the server-backed location
            # autocomplete — are driven by typing and picking, which is exactly
            # what the react_select path does.
            return "react_select"
        if entry.locator('input[type="radio"]').count():
            return "radio_group"
        boxes = entry.locator('input[type="checkbox"]')
        if boxes.count() > 1:
            return "checkbox_group"
        if boxes.count() == 1 and entry.locator("button").count() >= 2:
            return "yesno"
        return planned

    # --- combobox -----------------------------------------------------------

    def open_options(self, field_id: str) -> tuple[str, ...]:
        """Click the chevron, not the input.

        Clicking the input leaves `aria-expanded` false and opens nothing —
        the single reason this widget was unreadable before. The opened list
        is complete rather than virtualised (194 countries all present), so
        one read is the whole taxonomy.
        """
        self._entry(field_id).locator("button").first.click()
        return self.visible_options()

    def visible_options(self) -> tuple[str, ...]:
        try:
            self.page.wait_for_selector(self.LISTBOX, timeout=OPTION_TIMEOUT)
        except Exception:  # noqa: BLE001 - an empty listbox is an answer
            return ()
        nodes = self.page.locator(self.LISTBOX)
        return tuple(nodes.nth(i).inner_text().strip() for i in range(nodes.count()))

    def click_option(self, label: str) -> bool:
        """Wait for the option to actually be offered, then click it.

        The base scans whatever is on screen right now, which is fine for a
        client-side filter. Ashby's location field queries a server on every
        keystroke, so straight after typing the list is still the previous
        query's — or empty. `_select`'s retry path types a second candidate and
        clicks with no wait of its own, and that raced: the option was offered
        a moment later and the role failed with it listed in the error.
        """
        deadline = OPTION_TIMEOUT
        while True:
            options = self.page.locator(self.LISTBOX)
            for i in range(options.count()):
                node = options.nth(i)
                if node.inner_text().strip().casefold() == label.strip().casefold():
                    node.click()
                    return True
            if deadline <= 0:
                return False
            self.page.wait_for_timeout(_OPTION_POLL_MS)
            deadline -= _OPTION_POLL_MS

    def type_into(self, field_id: str, value: str) -> None:
        el = self._entry(field_id).locator(self.COMBOBOX).first
        el.click()
        el.fill("")
        el.type(value, delay=25)

    def is_expanded(self, field_id: str) -> bool:
        combo = self._entry(field_id).locator(self.COMBOBOX)
        if combo.count() == 0:
            return False
        return combo.first.get_attribute("aria-expanded") == "true"

    def selected_label(self, field_id: str) -> str:
        """A chosen combobox holds the option text as its own value, and a
        chosen toggle marks the button. Neither uses Greenhouse's
        `.select__single-value` sibling, which is what the base reads."""
        entry = self._entry(field_id)
        if entry.count() == 0:
            return ""
        combo = entry.locator(self.COMBOBOX)
        if combo.count():
            return (combo.first.input_value() or "").strip()
        active = entry.locator(f"button{self.ACTIVE}")
        return active.first.inner_text().strip() if active.count() else ""

    def value_of(self, field_id: str) -> str:
        el = self._locator(field_id)
        if el.count() == 0:
            return ""
        # A toggle's checkbox and a group's boxes carry no meaningful value:
        # an unvalued checkbox reads as the HTML default "on" whether or not it
        # is checked, which would report every untouched toggle as prefilled.
        if (el.first.get_attribute("type") or "") in ("checkbox", "radio"):
            return self.selected_label(field_id)
        try:
            value = el.first.input_value(timeout=1000)
        except Exception:  # noqa: BLE001 - a fieldset has no value
            value = ""
        return value or self.selected_label(field_id)

    # --- toggle and groups --------------------------------------------------

    def set_yesno(self, field_id: str, label: str) -> None:
        entry = self._entry(field_id)
        buttons = entry.locator("button")
        for i in range(buttons.count()):
            button = buttons.nth(i)
            if button.inner_text().strip().casefold() == label.strip().casefold():
                button.click()
                return
        raise FillError(f"{field_id}: no yes/no button labelled {label!r}")

    def check_group_option(self, field_id: str, label: str) -> None:
        """Each option's own `name` is its label text, and a `<label for=…>`
        repeats it. Two independent matches, neither of them a hashed class."""
        self._check_labelled(field_id, "checkbox", label)

    def check_radio_group(self, field_id: str, label: str) -> None:
        """Same shape as the checkbox group — `-labeled-radio-N` instead of
        `-labeled-checkbox-N`. The base looks for Lever's `eeo-option-text`
        sibling, which Ashby does not render."""
        self._check_labelled(field_id, "radio", label)

    def checked_radio_label(self, field_id: str) -> str:
        return next(
            (text for box, text in self._labelled_options(field_id, "radio") if box.is_checked()),
            "",
        )

    def checked_group_labels(self, field_id: str) -> tuple[str, ...]:
        return tuple(
            text for box, text in self._labelled_options(field_id, "checkbox") if box.is_checked()
        )

    def _labelled_options(self, field_id: str, input_type: str):
        """Yield `(box_locator, label)` for every radio/checkbox in this
        field's entry — the shared read behind `_check_labelled` (write) and
        the `checked_*` read-backs, matched the same way: `name` first, then
        the sibling `<label for=…>`."""
        entry = self._entry(field_id)
        boxes = entry.locator(f'input[type="{input_type}"]')
        for i in range(boxes.count()):
            box = boxes.nth(i)
            name = (box.get_attribute("name") or "").strip()
            text = name
            box_id = box.get_attribute("id")
            if box_id:
                labels = entry.locator(f'label[for="{box_id}"]')
                if labels.count():
                    text = labels.first.inner_text().strip() or name
            yield box, text

    def _check_labelled(self, field_id: str, input_type: str, label: str) -> None:
        for box, text in self._labelled_options(field_id, input_type):
            if label.strip().casefold() == text.casefold():
                box.check()
                return
        raise FillError(f"{field_id}: no {input_type} labelled {label!r}")

    def click_submit(self, selector: str) -> None:
        """Ashby's submit handler is wired to React's `onClick`, not a native
        form submit (§ASHBY: the field-entry root is a `<div role="tabpanel">`,
        not a `<form>`) — and a plain Playwright click misses that handler on
        the first try more often than not, a documented Playwright/React gap
        (upstream playwright#26340) rather than anything Ashby-specific.

        First attempt stays the base click, scrolled into view. `submit()`
        only calls this again when that attempt fired no request at all, so
        the retry escalates to `focus()` + a real `Enter` keypress — a
        genuinely trusted keyboard event, confirmed elsewhere to reach a
        handler a synthetic click did not. Escalating only on retry (never
        both in one call) keeps a click that did land from also being
        double-fired by the Enter.
        """
        button = self.page.locator(selector).first
        button.scroll_into_view_if_needed()
        self._submit_clicks = getattr(self, "_submit_clicks", 0) + 1
        if self._submit_clicks == 1:
            button.click()
        else:
            button.focus()
            self.page.keyboard.press("Enter")


_DRIVER_NAMES = {
    "greenhouse": "BrowserDriver",
    "lever": "LeverBrowserDriver",
    "ashby": "AshbyBrowserDriver",
}


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
    # The click has already returned and the option is chosen, but Ashby's
    # React state update can lag it by a beat — read immediately after and
    # `selected_label` can still come back empty for a value that lands a
    # moment later. Live on Cohere's `_systemfield_location`: the exact same
    # field, read again right after this raised, held the right value.
    shown = driver.selected_label(field.id)
    deadline = SELECT_SETTLE_TIMEOUT
    while not shown and deadline > 0:
        driver.wait(_OPTION_POLL_MS)
        shown = driver.selected_label(field.id)
        deadline -= _OPTION_POLL_MS
    if not shown:
        raise FillError(f"{field.id}: selected {label!r} but the field is empty")
    return shown


def _apply_field(driver, field: FieldPlan, result: FillResult,
                 answers: Answers | None = None) -> FieldOutcome:
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
    after = driver.value_of(field.id)
    if after.strip() != str(field.value).strip():
        raise FillError(f"{field.id}: wrote {field.value!r} but the field reads {after!r}")
    return FieldOutcome(field.id, "filled", before, after)


def _attach(driver, upload: FilePlan) -> FieldOutcome:
    if not upload.path.is_file():
        raise FillError(f"{upload.id}: {upload.path} is gone")
    driver.set_files(upload.id, upload.path)
    # The one write that used to return success unread. An input that ends up
    # empty leaves failures[] empty, so the submit guard passes and the
    # application goes out with no resume attached (§9 step 4).
    #
    # Confirmatory, never punitive: `None` means the input could not be read
    # back (it was detached by a re-render — observed live on a Greenhouse
    # cover_letter field), which is not evidence the upload failed. Treating
    # it as failure would block submissions that are perfectly fine.
    # The widget outranks the input. A board that rendered an upload error
    # refused the file no matter what `input.files` says, and a board showing
    # the filename accepted it no matter whether the input survived.
    shown = driver.upload_shows(upload.id, upload.path.name)
    if shown is False:
        raise FillError(
            f"{upload.id}: the board rejected {upload.path.name} — "
            f"its upload widget rendered an error"
        )
    if shown is True:
        return FieldOutcome(upload.id, "attached", "", upload.path.name)

    held = driver.attached_files(upload.id)
    if held is None:
        return FieldOutcome(
            upload.id, "attached", "", upload.path.name,
            note="attached, unverified: the input was replaced after upload",
        )
    if not held:
        raise FillError(f"{upload.id}: set {upload.path.name} but the input holds no file")
    if upload.path.name not in held:
        raise FillError(
            f"{upload.id}: set {upload.path.name} but the input holds {', '.join(held)}"
        )
    return FieldOutcome(upload.id, "attached", "", upload.path.name)


def fill_plan(plan: Plan, driver, answers: Answers | None = None,
              result: FillResult | None = None) -> FillResult:
    """Drive one plan to a filled — never submitted — form.

    Attachments go first and the page is allowed to settle, because an upload
    can rewrite fields. Everything else is then written over whatever is there.
    """
    result = result if result is not None else FillResult(form_url=plan.form_url)
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
            id=parked.id, name=parked.id, label=parked.label,
            required=parked.required, kind=parked.kind, section=parked.section,
            multi=parked.multi,
            options=tuple(MergedOption(label=o) for o in options),
        )
        resolution = resolve(field, answers)
        if resolution.action != "fill":
            continue
        plan_field = FieldPlan(
            id=parked.id, name=parked.id, label=parked.label, kind=parked.kind,
            section=parked.section, required=parked.required, multi=parked.multi,
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


def _reverify_toggle_fields(driver, plan: Plan) -> None:
    """Re-check every yes/no toggle, radio group and checkbox group
    immediately before the click, and re-apply any that drifted since
    `fill_plan` set it.

    Live on Take2 (Ashby): a toggle read back correctly right when
    `_apply_field` set it, then came back cleared by submit time — a
    *different* field each retry ("legally authorized to work", then "4 days
    a week"), which rules out one bad selector or field id and points at
    something external resetting a toggle asynchronously after our own fill
    already finished. The board's own "Autofill from resume" feature
    (visible on this exact form) is the leading suspect, but the fix does not
    depend on knowing the cause: whatever cleared it, this is the last chance
    to notice before the irreversible click.

    `radio_group` and `checkbox_group` get the same treatment on the same
    theory, not because either has been caught drifting — nothing has been.
    But nothing rules it out either, and re-checking a field that never moved
    costs one extra read; missing one that did costs a wrong answer under the
    user's name after the click can no longer be undone.
    """
    for field in plan.fields:
        if field.kind == "yesno":
            want = str(field.value)
            shown = driver.selected_label(field.id)
            if shown.strip().casefold() == want.strip().casefold():
                continue
            try:
                driver.set_yesno(field.id, want)
            except FillError:
                continue  # best-effort; an unresolved drift still fails at the click

        elif field.kind == "radio_group":
            want = (field.value if isinstance(field.value, tuple) else (field.value,))[0]
            shown = driver.checked_radio_label(field.id)
            if shown.strip().casefold() == str(want).strip().casefold():
                continue
            try:
                driver.check_radio_group(field.id, str(want))
            except FillError:
                continue

        elif field.kind == "checkbox_group":
            wanted = field.value if isinstance(field.value, tuple) else (field.value,)
            shown = {s.strip().casefold() for s in driver.checked_group_labels(field.id)}
            for label in wanted:
                if str(label).strip().casefold() in shown:
                    continue
                try:
                    driver.check_group_option(field.id, str(label))
                except FillError:
                    continue


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

    driver.watch_submit_requests(plan.ats, result.submit_requests)
    _reverify_toggle_fields(driver, plan)

    for attempt in range(SUBMIT_ATTEMPTS):
        # An upload still in flight makes the board refuse the click outright.
        # Observed live on Ashby, which says so in as many words.
        driver.settle()
        driver.click_submit(plan.submit_selector)
        # The click is the irreversible act. Record it before anything else
        # runs: a captcha timeout, a driver error, a navigation hiccup — none
        # of them may erase the fact that an application went out.
        result.submitted = True
        if plan.requires_captcha:
            # Lever renders hCaptcha on every form (§12a) — the click above
            # only opened the challenge. Block for a human to solve it; there
            # is no bypass and none is in scope (§12c).
            driver.wait_for_captcha()
        # Judged only after the board has had time to answer. Reading straight
        # off the click caught pages mid-navigation and reported a perfectly
        # good submission as unconfirmed.
        driver.wait(SUBMIT_SETTLE_MS)

        # A board we can watch settles it outright: no request to its submit
        # endpoint means the click did nothing, whatever the page looks like.
        if SUBMIT_REQUEST_MARKERS.get(plan.ats) and not result.submit_requests:
            result.submitted = False
            result.submit_error = (
                f"clicked {plan.submit_selector} but {plan.ats} never called its "
                f"submit endpoint — nothing was sent"
            )
            log.info("submit fired no request (attempt %d)", attempt + 1)
            continue

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

    # A recorded submit response is confirmation on its own — it is the board
    # accepting the application, not a page that happens to read like it did.
    result.confirmed = result.submitted and (
        bool(result.submit_requests) or driver.submission_confirmed()
    )
    result.evidence = capture_submit_evidence(driver, plan)


def capture_submit_evidence(driver, plan) -> Path | None:
    """Write what the board rendered after the click, and return the text path.

    `CONFIRMATION_MARKERS` is guessed — no live confirmation page had ever been
    read when it was written, and the first real submission matched none of it.
    The page that would have settled the question was on screen and closed
    unrecorded. This keeps it: the next submission is what turns that list from
    a guess into something measured.

    Diagnostics only. It runs after the click, never decides whether one
    happens, and every failure here is swallowed — losing a screenshot must not
    turn a submitted application into a reported error.
    """
    try:
        stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        out_dir = paths.APPLICATIONS / "apply_runs" / "evidence"
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = out_dir / f"{stamp}_{plan.job_id or 'unknown'}"

        page = driver.page
        text = page.locator("body").inner_text(timeout=UPLOAD_READBACK_TIMEOUT)
        note = (
            f"# post-submit capture\n"
            f"job_id: {plan.job_id}\n"
            f"company: {plan.company}\n"
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


def run_one(plan: Plan, answers: Answers | None = None, *, sink: list | None = None,
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
            if submit_after:
                try:
                    submit(plan, result, driver)
                except SubmitGuardError as exc:
                    # Refusal — raised before the click, so `submitted` is
                    # still False and nothing went out.
                    result.submit_error = str(exc)
                except Exception as exc:  # noqa: BLE001
                    # Anything else may have fired after the click. `submit`
                    # sets `result.submitted` the instant the click returns, so
                    # that flag — not this handler — decides whether an
                    # application went out.
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


def fill(plan: Plan, answers: Answers | None = None, *,
         headless: bool = False, after=None, sink: list | None = None) -> FillResult:
    """Fill only, never submit — `run_one` with `submit_after` left at its
    default. Kept as its own name because `apply fill` (no `--submit` in
    reach at all) is a distinct, deliberately narrower CLI surface than
    `apply run`.
    """
    return run_one(plan, answers, headless=headless, after=after, sink=sink)
