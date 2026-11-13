"""The browser drivers `fill.py` drives a form through, one per board family
(Greenhouse's base, Lever, Ashby), and the selectors, timeouts and page markers
they read. A driver types, picks, ticks, uploads and reads back; `fill.submit()`
is the one caller of its click."""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

from applyourself.apply.domscan import scan_form
from applyourself.apply.plan import Plan
from applyourself.apply.reconcile import MergedField, MergedOption
from applyourself.apply.reconcile import is_numeric as _is_numeric

log = logging.getLogger(__name__)


FORM_SELECTOR = "#application-form"
# Attribute form, not "#id": a checkbox group's id carries a literal "[]".
FIELD = '{form} [id="{id}"]'
# Lever fields carry no id at all — MergedField.id IS the DOM `name` (lever.py
# never aliases the two apart, exactly so this selector keeps working).
FIELD_BY_NAME = '{form} [name="{id}"]'

# react-select's rendered parts, as they appear on every board sampled.
SELECT_OPTION = ".select__option"
#: Lever renders no react-select at all: its location autocomplete drops
#: suggestions into this container. `.select__option` never appears on a Lever
#: board, so the base driver's option selectors can only ever come back empty
#: there.
LEVER_OPTION = ".dropdown-results > *"
#: The field Lever actually posts for a location: the visible text input is a
#: search box, and this hidden input holds the structured place the suggestion
#: click resolved to. Text with no suggestion picked leaves it empty, and the
#: board rejects the submission ("error verifying your application").
LEVER_SELECTED_LOCATION = "#selected-location"
#: Lever boards can carry a cookie-consent banner whose buttons overlay the
#: form's own fields. A warmed profile has already dismissed it; a fresh one
#: has it sitting on top of the first few questions.
COOKIE_ACCEPT = ".cc-window .cc-allow"
#: Ashby's consent dialog blocks the fill the same way; its accept button is
#: the one reading "Accept All".
ASHBY_COOKIE_ACCEPT = ('div[role="dialog"][aria-label*="Cookie"] button', "Accept All")
SELECT_SINGLE_VALUE = ".select__single-value"
SELECT_MULTI_VALUE = ".select__multi-value__label"

# Positive acknowledgements only. Matching none of these means "unconfirmed",
# never "failed" — see `BrowserDriver.submission_confirmed`. Extend, never
# invert.
CONFIRMATION_MARKERS = (
    "text=/thank you for applying/i",
    "text=/application (?:has been )?(?:submitted|received)/i",
    # Ashby's wording, verbatim: "Your application was successfully submitted."
    # The optional adverb between "was" and the verb is load-bearing.
    "text=/your application was (?:successfully )?(?:submitted|sent)/i",
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
    "please try again when they",  # "...when they're finished"
    "updating your application",
    # Ashby can answer the submit request 200 (GraphQL's success-status
    # convention, not the application's) and still re-render the form with
    # this banner — a request marker proves the click did something, never
    # that the board accepted it.
    "your form needs corrections",
    "missing entry for required field",
)
# Text a board renders when it received the application and turned it down —
# a spam flag, a duplicate, a past decision. Unlike a refusal, clicking again
# cannot help, so this ends the attempt. Measured on Ashby (33a514eb, 335009f3,
# 35e0852e): the submit request answers 200 and the page says this instead.
SUBMIT_REJECTED_MARKERS = (
    "we couldn't submit your application",
    "we couldn’t submit your application",
)


# The request a board fires when it actually accepts an application. Measured
# from a real manual submit, unlike CONFIRMATION_MARKERS: a 2xx response to one
# of these is evidence the click did something, where rendered text only ever
# said the page looked right. Keyed by ats, and absent for a board nobody has
# watched yet — an unknown board falls back to the text markers.
SUBMIT_REQUEST_MARKERS = {
    # The submit mutations only (`ApiSubmitSingleApplicationFormAction`,
    # `ApiSubmitMultipleFormsAction`). The same endpoint also carries
    # `ApiSetFormValue` autosaves, which fire around the click and are not a
    # submission (8475e862 was reported submitted on two autosaves alone).
    "ashby": ("jobs.ashbyhq.com/api/non-user-graphql?op=ApiSubmit",),
    # `job-boards.greenhouse.io/embed/job_app` — the iframe's own GET, loaded
    # once per page view — contains this same substring (it's the `job-`
    # prefix, not a different path), so the URL alone is not unique. Gated by
    # SUBMIT_REQUEST_METHOD below.
    "greenhouse": ("boards.greenhouse.io/embed/",),
    # Measured off a real submission (cae17aa7): the form carries
    # `method="POST"` and no `action`, so it posts to the very URL the page was
    # fetched from. The path cannot tell the submit from the page load, which
    # is why this is gated on POST below — the page load is a GET. Measured at
    # the same time: the only POST to this host around the click was the
    # submit itself; the captcha and any board-embedded widgets post to their
    # own hosts.
    "lever": ("jobs.lever.co/",),
}
#: Set to log every non-GET response around the submit click, for a board with
#: no marker yet. How the `lever` marker above was measured; keep it for the
#: next unmarked board rather than guessing one.
LOG_ALL_REQUESTS_ENV = "APPLY_LOG_ALL_REQUESTS"

# Per-ats method a matched marker must also carry.
SUBMIT_REQUEST_METHOD = {
    "ashby": "POST",
    "greenhouse": "POST",
    "lever": "POST",
}
#: Boards whose submit is answered with a redirect rather than a 2xx body.
#: Lever's is a native form POST and returns **302** to its own /thanks page,
#: so the 2xx-only rule dropped the one response that proves the submission
#: landed. Kept per-ats and not relaxed globally: for the XHR boards a 3xx is
#: not a shape anything has measured, and treating one as success there would
#: be a guess.
SUBMIT_REQUEST_REDIRECT_OK = frozenset({"lever"})


# Ashby names the exact field it thinks is unanswered, one per line: "Missing
# entry for required field: LinkedIn Profile". Generic — matched against
# whatever `submission_refused()` read, not gated to one ats — so a board that
# renders the same wording gets the same recovery for free.
MISSING_FIELD_PATTERN = re.compile(r"missing entry for required field:\s*(.+)", re.IGNORECASE)


# How often to re-check a server-backed listbox while waiting for its results.
_OPTION_POLL_MS = 250


class FillError(Exception):
    """A widget did not take the value, or the form is not what was planned."""


def refused_field_names(rows: list[dict]) -> tuple[str, ...]:
    """The field names to report from one `invalid_fields` DOM read.

    `rows` is every validating control in the form, each as
    `{name, type, checked, valueMissing, invalid, fallback, wrapper, tag}`:
    `fallback` is the control's id, `wrapper` the id (else label) of the field
    its nearest wrapper holds — a react-select's hidden `requiredInput` has no
    name or id of its own — and `tag` the last resort. Pure, so the suite can
    cover the grouping the browser read cannot.

    A required checkbox *group* is satisfied by one tick, but Greenhouse marks
    every box in the group `required`, so the seven unticked siblings stay
    `valueMissing` and the raw read refuses a form the board would accept.
    Such a group is dropped. Three conditions narrow that, each guarding a
    case where dropping would hide a real refusal:

    - Only on a shared non-empty `name`. `el.name || el.id || el.tagName`
      keys every nameless control to the same "", so grouping on that would
      let one ticked nameless box clear unrelated empty fields.
    - Only when every reported member is a checkbox. A shared name is not a
      shared type: Lever's pronouns group is ten checkboxes plus a free-text
      `customPronounsTextField`, and
      that text input being required-and-empty must still refuse.
    - Only for `valueMissing`. A pattern or `type=email` failure is not an
      unticked sibling and is never satisfied by one.

    Names are deduped, first-seen order kept: the raw read repeats a group's
    name once per unticked box.
    """
    checked_names = {
        r["name"]
        for r in rows
        if r.get("name") and r.get("type") == "checkbox" and r.get("checked")
    }
    reported: dict[str, list[dict]] = {}
    for row in rows:
        if not row.get("invalid"):
            continue
        key = row.get("name") or ""
        reported.setdefault(key, []).append(row)

    out: list[str] = []
    for key, members in reported.items():
        satisfied_group = (
            key
            and key in checked_names
            and all(m.get("type") == "checkbox" and m.get("valueMissing") for m in members)
        )
        if satisfied_group:
            continue
        for member in members:
            label = (
                member.get("name")
                or member.get("fallback")
                or member.get("wrapper")
                or member.get("tag")
                or ""
            )
            if label and label not in out:
                out.append(label)
    return tuple(out)


class BrowserDriver:
    """Everything the fill sequence does to a page, in one swappable object.

    Split out so `fill_plan` — the ordering, the overwrite rule, the asserts —
    is testable without a browser.
    """

    def __init__(self, page):
        self.page = page
        # field id -> where it really is, for the ids that are aliases
        # (`MergedField.dom_path`). Only Ashby's driver reads these.
        self._dom_paths: dict[str, str] = {}
        self._dom_controls: dict[str, str] = {}

    def learn_dom_aliases(self, plan: Plan) -> None:
        """Take the plan's `dom_path`/`dom_control` before the fill starts."""
        for field in plan.fields:
            if field.dom_path:
                self._dom_paths[field.id] = field.dom_path
            if field.dom_control:
                self._dom_controls[field.id] = field.dom_control

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

    #: The board's cookie-banner accept buttons, tried in order, as (selector,
    #: button text); "" takes any button the selector finds. None: no banner.
    cookie_accept: tuple[tuple[str, str], ...] = ()

    def dismiss_cookie_banner(self) -> bool:
        """Accept the consent banner if one is up. Returns whether it clicked.
        No banner is the normal case on a warmed profile, not an error, and
        costs no wait: `count()` and `is_visible()` return at once.

        The visible button, not the first one: a board renders the banner's
        markup more than once (a wide and a narrow variant), and only one copy
        is displayed. `.first` resolved to a hidden copy and the click waited
        out its whole timeout on an element that would never be visible.
        """
        for selector, text in self.cookie_accept:
            buttons = self.page.locator(selector)
            if text and buttons.count():
                buttons = buttons.filter(has_text=text)
            for i in range(buttons.count()):
                button = buttons.nth(i)
                try:
                    if not button.is_visible():
                        continue
                    button.click(timeout=OPTION_TIMEOUT)
                except Exception as exc:  # noqa: BLE001 - a banner we cannot click is not fatal
                    log.debug("cookie banner button %d not clickable (%s)", i, exc)
                    continue
                return True
        return False

    def set_yesno(self, field_id: str, label: str) -> None:
        """Boards that render a boolean as a two-button toggle rather than a
        checkbox. Only Ashby does; the base has no such widget."""
        raise FillError(f"{field_id}: this board has no yes/no toggle widget")

    def goto(self, url: str) -> None:
        """Navigate, then clear the cookie banner before anything is written.
        Its buttons render *over* the first questions, so a click on a field
        underneath lands on the banner instead."""
        self.page.goto(url, wait_until="domcontentloaded")
        self.page.wait_for_selector(FORM_SELECTOR, timeout=FORM_TIMEOUT)
        self.dismiss_cookie_banner()

    def settle(self, timeout: int = OPTION_TIMEOUT, floor_ms: int = 0) -> None:
        """Let an upload's XHR finish before anything is read back.

        `floor_ms` waits before the readiness check rather than after it: a
        board that has not yet issued its parse request is idle by every
        measure, so networkidle alone returns immediately and proves nothing.
        """
        if floor_ms:
            self.page.wait_for_timeout(floor_ms)
        try:
            self.page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:  # noqa: BLE001 - a busy page is not a failure
            log.debug("networkidle did not settle; continuing")

    def value_of(self, field_id: str) -> str:
        el = self._locator(field_id)
        if el.count() == 0:
            return ""
        try:
            value = el.first.input_value(timeout=1000)
        except Exception as exc:  # noqa: BLE001 - not an <input>, e.g. a fieldset
            log.debug("%s: no input value (%s)", field_id, exc)
            value = ""
        if value:
            return value
        # A react-select keeps its input empty and shows the choice in a sibling.
        return self.selected_label(field_id)

    def selected_label(self, field_id: str) -> str:
        container = self._locator(field_id).locator(
            "xpath=ancestor::*[contains(@class,'select__container')][1]"
        )
        if container.count() == 0:
            return ""
        parts: list[str] = []
        for selector in (SELECT_SINGLE_VALUE, SELECT_MULTI_VALUE):
            nodes = container.first.locator(selector)
            parts.extend(nodes.nth(i).inner_text().strip() for i in range(nodes.count()))
        return ", ".join(p for p in parts if p)

    def is_expanded(self, field_id: str) -> bool:
        return self._locator(field_id).first.get_attribute("aria-expanded") == "true"

    def _upload_group(self, field_id: str):
        """The widget wrapping this file input, or an empty locator."""
        return self.page.locator(f"div.file-upload[aria-labelledby='upload-label-{field_id}']")

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
                    with self.page.expect_file_chooser(timeout=UPLOAD_CHOOSER_TIMEOUT) as chooser:
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
            except Exception as exc:  # noqa: BLE001 - detached/re-rendered node
                log.debug("%s: upload widget unreadable (%s)", field_id, exc)
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
        by the time this runs, and the marker list only ever covers wordings
        someone has seen. A new one should *extend* it — never invert the
        default, which would turn "assume submitted" into "assume failed" and
        reintroduce the duplicate-application path.
        """
        for marker in CONFIRMATION_MARKERS:
            try:
                if self.page.locator(marker).count() > 0:
                    return True
            except Exception as exc:  # noqa: BLE001 - a bad marker is not a failure
                log.debug("confirmation marker %r unreadable (%s)", marker, exc)
                continue
        try:
            # Greenhouse lands on /embed/job_app/confirmation after a submit
            # (d4e8be80: "Thank you for your interest in career opportunities").
            return "/confirmation" in (self.page.url or "")
        except Exception as exc:  # noqa: BLE001 - an unreadable url confirms nothing
            log.debug("page url unreadable (%s)", exc)
            return False

    def watch_submit_requests(self, ats: str, sink: list[str]) -> None:
        """Record 2xx responses to the board's submit endpoint into `sink`.

        Registered before the click, because the response is the only proof the
        click did anything. A board nobody has watched yet has no marker, so
        the sink stays empty and confirmation falls back to page text.
        """
        if os.environ.get(LOG_ALL_REQUESTS_ENV):
            self._log_all_responses()

        markers = SUBMIT_REQUEST_MARKERS.get(ats, ())
        if not markers:
            return
        required_method = SUBMIT_REQUEST_METHOD.get(ats)

        ceiling = 400 if ats in SUBMIT_REQUEST_REDIRECT_OK else 300

        def on_response(response) -> None:
            if response.status >= ceiling:
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

    def _log_all_responses(self) -> None:
        """Log every non-GET response, for measuring an unmarked board.

        Logs only. Nothing here reaches the `sink` that `result.submit_requests`
        is built from, and that separation is the whole point: an unmeasured
        board must not acquire a confirmation signal by being watched loosely.
        A marker matched too broadly is how a click that did nothing gets
        reported as a submission.
        """
        # Nothing in this package configures logging (printing belongs to
        # apply_cli), so an unconfigured root logger would swallow every line
        # this flag exists to produce. Self-sufficient rather than depending on
        # a caller to raise the level.
        if not any(isinstance(h, logging.StreamHandler) for h in log.handlers):
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            log.addHandler(handler)
        log.setLevel(logging.INFO)

        def on_any(response) -> None:
            method = response.request.method
            if method == "GET":
                return
            log.info("REQUEST %s %s %s", method, response.status, response.url)

        try:
            self.page.on("response", on_any)
        except Exception:  # noqa: BLE001 - a driver without events is not a failure
            log.debug("could not attach the all-requests logger")

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
            text = self.page.locator("body").inner_text(timeout=UPLOAD_READBACK_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - unreadable page is not a refusal
            log.debug("page text unreadable for the refusal check (%s)", exc)
            return ""
        lowered = text.casefold()
        for marker in SUBMIT_REFUSED_MARKERS:
            if marker.casefold() in lowered:
                return marker
        return ""

    def submission_rejected(self) -> str:
        """The board's reason for turning a received application down, or "".

        Returns the line after the rejection heading ("...flagged as possible
        spam", "...already received an application from you"), or the heading
        itself when nothing follows it.
        """
        try:
            text = self.page.locator("body").inner_text(timeout=UPLOAD_READBACK_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - unreadable page is not a rejection
            log.debug("page text unreadable for the rejection check (%s)", exc)
            return ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for i, line in enumerate(lines):
            if any(m in line.casefold() for m in SUBMIT_REJECTED_MARKERS):
                return (lines[i + 1] if i + 1 < len(lines) else line)[:300]
        return ""

    def missing_field_labels(self) -> tuple[str, ...]:
        """The exact labels the board just named as unanswered, or ().

        Measured on b9a009ad (Ashby): every field it named was already correct
        in the DOM — right value, right radio checked — both right after
        `fill_plan` and in the post-refusal screenshot. The mismatch lives in
        whatever internal state the board's own validation reads, not in
        anything a pre-click read-back can see, so guessing which field might
        have drifted and skip-if-already-right (`_reverify_fields`) cannot
        catch it. Reading the board's own complaint and forcing a fresh write
        on exactly those fields — even though they already read correctly — is
        the one recovery that reacts to what actually happened rather than a
        guess made in advance.
        """
        try:
            text = self.page.locator("body").inner_text(timeout=UPLOAD_READBACK_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - unreadable page names nothing
            log.debug("page text unreadable for missing fields (%s)", exc)
            return ()
        return tuple(m.strip() for m in MISSING_FIELD_PATTERN.findall(text) if m.strip())

    def attached_files(self, field_id: str) -> tuple[str, ...] | None:
        """Filenames the input holds, or None if that cannot be determined.

        `input_value()` refuses a file input, so this reads `.files` directly.
        A read, not a write, so the "setting .value will not update React
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
        except Exception as exc:  # noqa: BLE001 - detached/re-rendered node
            log.debug("%s: attached files unreadable (%s)", field_id, exc)
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
        # A missing box would wait out Playwright's 30s and raise a plain
        # TimeoutError, which escapes `fill_plan`'s per-field isolation.
        try:
            el.wait_for(state="attached", timeout=OPTION_TIMEOUT)
        except Exception as exc:  # absent node
            raise FillError(f"{field_id}: not on the page") from exc
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
        except Exception as exc:  # noqa: BLE001 - an empty listbox is an answer
            log.debug("no listbox options (%s)", exc)
            return ()
        nodes = self.page.locator(SELECT_OPTION)
        return tuple(nodes.nth(i).inner_text().strip() for i in range(nodes.count()))

    def select_native(self, field_id: str, label: str) -> None:
        """A real `<select>` — no typing, no listbox, `select_option()` just
        works. Lever's fields carry no react-select at all."""
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
        Lever's EEOC race question.

        `label` is stripped before comparing everywhere in this file, not just
        the DOM side: a work-authorization option's own rendered text can
        carry a trailing space (Ashby's "Are you authorized to work in the
        country you currently reside in?" renders "Yes " / "No ", per
        `tests/apply/fixtures/work_auth_labels.jsonl`), and a configured
        answer copied from that corpus carries the same space. The DOM text
        was already stripped before this comparison; the answer was not,
        which is what let "yes " fail to match "yes".
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

    def clear_captcha_token(self) -> None:
        """Empty the hidden response token before the click that asks for a new
        one. Without this, a token from an earlier solve is still sitting there
        and `wait_for_captcha` returns immediately on it."""
        self.page.evaluate(
            "() => { const el = document.getElementById('hcaptchaResponseInput');"
            " if (el) el.value = ''; }"
        )

    def wait_for_captcha(self, timeout_ms: int = 600_000) -> None:
        """Block until a human solves the hCaptcha challenge Lever's own JS
        just opened — proxied by the hidden response token going non-empty.
        Unattended submission is not possible on a board that renders one:
        this is the wait, not a bypass.

        Only ever called after `clear_captcha_token`, because "non-empty" is
        not the same question as "freshly solved". An hCaptcha token is
        single-use and expires about two minutes after it is minted, and a
        Lever board pops its challenge mid-fill — the moment the location
        search box is touched — so by the time the form is finished the token
        from that solve is spent. Waiting on non-empty alone returned at once
        and the dead token was posted, which the board rejects with "there was
        an error verifying your application".
        """
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

    def invalid_fields(self) -> tuple[str, ...]:
        """Required controls the browser itself already refuses, by name.

        Constraint validation runs before any of the board's own script, so a
        form in this state cannot submit: the click fires, nothing leaves the
        browser, and the page does not even navigate. Read through the form's
        own `checkValidity()` rather than guessed from the plan — a field the
        plan calls optional can be `required` in the DOM the moment another
        answer makes it so (a self-ID signature becomes required as soon as the
        disability question is answered).

        An empty tuple on a board with no `<form>` element: nothing to check is
        not the same as something invalid, and Ashby renders no form at all.

        The DOM read returns rows, not names: a required checkbox group's
        unticked siblings are `valueMissing` even once the group is answered,
        and deciding that needs the ticked sibling's state too. Grouping lives
        in `refused_field_names`, which is pure and covered by the suite.
        """
        rows = self.page.evaluate(
            """(sel) => {
                const form = document.querySelector(sel);
                if (!form) return [];
                const FIELD = '[role="combobox"][id], input[id]:not([type="hidden"]), '
                    + 'select[id], textarea[id]';
                const wrapper = (el) => {
                    let node = el.parentElement;
                    for (let k = 0; k < 4 && node && node !== form; k++) {
                        const inner = [...node.querySelectorAll(FIELD)].find(x => x !== el);
                        if (inner) return inner.id;
                        const label = node.querySelector("label");
                        if (label && label.textContent.trim()) return label.textContent.trim();
                        node = node.parentElement;
                    }
                    return "";
                };
                return [...form.elements]
                    .filter(el => el.willValidate)
                    .map(el => ({
                        name: el.name || "",
                        type: el.type || "",
                        checked: !!el.checked,
                        valueMissing: !!(el.validity && el.validity.valueMissing),
                        invalid: !el.checkValidity(),
                        fallback: el.id || "",
                        wrapper: el.id || el.name ? "" : wrapper(el),
                        tag: el.tagName,
                    }));
            }""",
            FORM_SELECTOR,
        )
        return refused_field_names(rows)

    def required_fields(self) -> tuple[MergedField, ...]:
        """Every required field the page shows right now, read by the same DOM
        scan the plan was built from. Labels and kinds only: no API enrichment,
        and a react-select's options are read by opening it."""
        return tuple(
            MergedField(
                id=f.id,
                name=f.name,
                label=f.label,
                required=True,
                kind=f.kind,
                section=f.section,
                multi=f.multi,
                options=tuple(MergedOption(label=o.label, value=o.value) for o in f.options),
                max_length=f.max_length,
            )
            for f in scan_form(self.page.content()).fields
            if f.required
        )

    def submit_disabled_now(self, selector: str) -> bool:
        """Re-read `aria-disabled` live — `plan.submit_disabled` is a scan-time
        snapshot and every field write since then can have changed it."""
        return self.page.locator(selector).first.get_attribute("aria-disabled") == "true"

    def click_submit(self, selector: str) -> None:
        self.page.locator(selector).first.click()


class LeverBrowserDriver(BrowserDriver):
    """Same sequence, different selector: Lever's fields carry no `id`
    attribute at all, so every lookup is by `name` instead."""

    cookie_accept = ((COOKIE_ACCEPT, ""),)

    def _locator(self, field_id: str):
        return self.page.locator(FIELD_BY_NAME.format(form=FORM_SELECTOR, id=field_id))

    def _checkbox_options(self, field_id: str):
        """Lever has no fieldset wrapper: the name selector already resolves to
        the checkboxes themselves, so the base implementation's
        `.locator('input[type=checkbox]')` searches *inside* an <input> and
        finds nothing. Match on the option's own value/label instead.

        Only the checkboxes, because a shared `name` is not a shared type: the
        pronouns group renders ten checkboxes plus a free-text
        `customPronounsTextField` under that one name, and `is_checked()` on
        the text input raises "Not a checkbox or radio button" — which failed
        the whole role. The same decoy problem `set_checkbox` handles, on the
        group path. `scan_lever_form` already reads options from
        `input[@type="checkbox"]` alone, so the plan never names the text one.
        """
        boxes = self._locator(field_id)
        for i in range(boxes.count()):
            box = boxes.nth(i)
            if (box.get_attribute("type") or "") != "checkbox":
                continue
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

    def _radio_options(self, field_id: str):
        """A custom-card radio labels with <span class=
        "application-answer-alternative">, where the base implementation reads
        the EEO question's "eeo-option-text" and finds nothing -- every
        card radio then matched the empty string and failed. The first sibling
        span rather than either class name, so a rename does not break it
        again, with the same `value` fallback as `_checkbox_options` above.
        """
        radios = self._locator(field_id)
        for i in range(radios.count()):
            radio = radios.nth(i)
            span = radio.locator("xpath=following-sibling::span[1]")
            text = span.first.inner_text().strip() if span.count() else ""
            yield radio, (text or radio.get_attribute("value") or "")

    def check_radio_group(self, field_id: str, label: str) -> None:
        for radio, text in self._radio_options(field_id):
            value = radio.get_attribute("value") or ""
            if label.strip().casefold() in (value.casefold(), text.casefold()):
                radio.check()
                return
        raise FillError(f"{field_id}: no radio labelled {label!r}")

    def set_checkbox(self, field_id: str, checked: bool) -> None:
        """Lever pairs a lone checkbox with a hidden `value="0"` input under the
        same name, so an unchecked box still posts a value. The base
        implementation's `.first` resolves to that hidden decoy and Playwright
        refuses it with "Not a checkbox or radio button". Take the real input.
        """
        boxes = self._locator(field_id)
        for i in range(boxes.count()):
            box = boxes.nth(i)
            if (box.get_attribute("type") or "") == "checkbox":
                box.check() if checked else box.uncheck()
                return
        raise FillError(f"{field_id}: no checkbox input")

    def required_fields(self) -> tuple[MergedField, ...]:
        """Not read on Lever: its form is not the Greenhouse markup the scan
        reads. The browser's own `invalid_fields` check still guards the click."""
        return ()

    # --- location autocomplete ------------------------------------------------
    # Lever's location field is a search box over a place taxonomy, not a text
    # field: the visible input filters, and the click on a suggestion is what
    # populates the hidden `selectedLocation` the board actually posts. None of
    # the base driver's option selectors match here (see LEVER_OPTION).

    def visible_options(self) -> tuple[str, ...]:
        try:
            self.page.wait_for_selector(LEVER_OPTION, timeout=OPTION_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - an empty suggestion list is an answer
            log.debug("no location suggestions (%s)", exc)
            return ()
        nodes = self.page.locator(LEVER_OPTION)
        return tuple(nodes.nth(i).inner_text().strip() for i in range(nodes.count()))

    def click_option(self, label: str) -> bool:
        options = self.page.locator(LEVER_OPTION)
        for i in range(options.count()):
            node = options.nth(i)
            if node.inner_text().strip().casefold() == label.strip().casefold():
                node.click()
                return True
        return False

    def is_expanded(self, field_id: str) -> bool:
        """No `aria-expanded` on this widget — the suggestion container holding
        children is the only signal that it is still open."""
        return self.page.locator(LEVER_OPTION).count() > 0

    def selected_label(self, field_id: str) -> str:
        """The chosen location, but only once it is structurally chosen.

        Returns "" while the hidden `selectedLocation` is empty even though the
        visible input already shows the typed text, so `_select`'s own
        verification is what catches a location that was typed but never
        resolved — rather than this reporting a filled field the board will
        refuse. Fields other than the location have no hidden partner and read
        straight off the input.
        """
        el = self._locator(field_id).first
        shown = (el.input_value() or "").strip()
        hidden = self.page.locator(LEVER_SELECTED_LOCATION)
        if not hidden.count():
            return shown
        if not (hidden.first.input_value() or "").strip():
            return ""
        return shown


#: Ashby's question title; a required one also carries a hashed `_required_` class.
ASHBY_TITLE_CLASS = "ashby-application-form-question-title"
ASHBY_REQUIRED_CLASS = "_required_"

# One row per required field entry: its path, title, a kind where the control
# names it ("select" where only `resolve_kind` can tell), and the labels of
# its radio or checkbox options.
_REQUIRED_ENTRIES_SCRIPT = """(entries, cls) => {
    const out = [];
    const seen = new Set();
    for (const el of entries) {
        const path = el.getAttribute('data-field-path');
        const title = el.querySelector('.' + cls.title);
        if (!path || seen.has(path) || !title) continue;
        if (!(title.className || '').includes(cls.required)) continue;
        seen.add(path);
        let kind = 'select';
        const text = el.querySelector('input:not([type="radio"]):not([type="checkbox"])');
        if (el.querySelector('input[type="file"]')) kind = 'file';
        else if (el.querySelector('textarea')) kind = 'textarea';
        else if (text && !el.querySelector('[role="combobox"]')) {
            const t = (text.getAttribute('type') || 'text').toLowerCase();
            kind = t === 'number' ? 'number' : t === 'tel' ? 'tel' : 'text';
        }
        const options = [...el.querySelectorAll('input[type="radio"], input[type="checkbox"]')]
            .map(box => {
                const label = box.id && el.querySelector(`label[for="${box.id}"]`);
                return (label ? label.textContent : box.name || '').trim();
            })
            .filter(Boolean);
        out.push({path, label: title.textContent.trim(), kind, options});
    }
    return out;
}"""


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
    cookie_accept = (ASHBY_COOKIE_ACCEPT,)
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

        `field_id` is the DOM's own `data-field-path`, unless the plan gave
        this field a `dom_path` — the education sub-fields are aliased to
        Greenhouse ids that match no Ashby element. The two file fields are
        aliased to the canonical `resume` / `cover_letter` ids everywhere else
        in the pipeline, so callers that upload a file pass `FilePlan.name` —
        the real path `ashby.py` carried through — rather than `.id`.
        """
        return self.page.locator(
            self.ENTRY.format(id=self._dom_paths.get(field_id, field_id))
        ).first

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
        return self._entry(field_id).locator(self._control_for(field_id))

    def _control_for(self, field_id: str) -> str:
        """`dom_control` when the plan named one, else every control in the
        entry. The education block is one entry holding eight."""
        return self._dom_controls.get(field_id) or self.CONTROL

    def goto(self, url: str) -> None:
        """No `<form>` to wait for — the base would time out on every board.
        The first field entry is the equivalent readiness signal, and it only
        exists once the client-side render has run."""
        self.page.goto(url, wait_until="domcontentloaded")
        self.page.wait_for_selector("[data-field-path]", timeout=FORM_TIMEOUT)
        self.dismiss_cookie_banner()

    def resolve_kind(self, field_id: str, planned: str) -> str:
        """What this field renders as right now, falling back to the plan.

        Reinterpretation only ever applies to `planned in ("select",
        "combobox")` — the two API types Ashby is documented to render
        ambiguously: `select` as a radio group at 1/3/5 options, a combobox
        at 11/24/194, or a lone checkbox for a single acknowledgement;
        `combobox` (the Location field) as either the enumerated or the
        server-backed flavour, both driven the same way. Every other planned
        kind is trusted as-is: a `text`-planned field can carry unrelated
        nested controls in the same entry — e.g. a `Phone` field with an
        SMS-consent Yes/No radio pair bolted onto its own `data-field-path`
        — and those must never override the field's real, unambiguous kind.
        A field not on the page (a conditional question, say) also keeps its
        planned kind and fails later in the ordinary way rather than being
        silently reinterpreted here.
        """
        entry = self._entry(field_id)
        if entry.count() == 0 or planned not in ("select", "combobox"):
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
        if boxes.count() == 1:
            # A lone checkbox with no companion toggle buttons is a consent
            # tick, not the widget its API type implied. Observed live: a
            # single-option `ValueSelect` (-> planned "select") that Ashby
            # actually renders as `<input type="checkbox">` for an
            # acknowledgement question ("I have read and understood the
            # Arbitration Agreement..."). `select_option()` against it raises
            # "Element is not a <select> element"; `fill._as_kind` turns the
            # one choice into a tick.
            return "checkbox"
        return planned

    def required_fields(self) -> tuple[MergedField, ...]:
        """Every field entry whose title is marked required, keyed by its
        `data-field-path`. A choice widget's kind comes from `resolve_kind`;
        radio and checkbox options are their labels."""
        rows = self.page.eval_on_selector_all(
            "[data-field-path]",
            _REQUIRED_ENTRIES_SCRIPT,
            {"title": ASHBY_TITLE_CLASS, "required": ASHBY_REQUIRED_CLASS},
        )
        out = []
        for row in rows:
            kind = row["kind"]
            if kind == "select":
                kind = self.resolve_kind(row["path"], kind)
            options = ("Yes", "No") if kind == "yesno" else tuple(row["options"])
            out.append(
                MergedField(
                    id=row["path"],
                    name=row["path"],
                    label=row["label"],
                    required=True,
                    kind=kind,
                    section="questions",
                    multi=kind == "checkbox_group",
                    options=tuple(MergedOption(label=o) for o in options),
                )
            )
        return tuple(out)

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
        except Exception as exc:  # noqa: BLE001 - an empty listbox is an answer
            log.debug("no listbox options (%s)", exc)
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
        except Exception as exc:  # noqa: BLE001 - a fieldset has no value
            log.debug("%s: no input value (%s)", field_id, exc)
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
        form submit — and a plain Playwright click misses that handler on
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
