"""Ashby application form: read the question API into a merged field set.

**The form is client-rendered, so there is no HTML to scan.** `load_board`
reads Ashby's own anonymous GraphQL endpoint instead:

    POST https://jobs.ashbyhq.com/api/non-user-graphql?op=ApplicationForm

The collection is `fieldEntries`, and `field` is a **JSON scalar** carrying the
whole definition (`type`, `path`, `title`, `selectableValues`) in one blob
rather than as a selectable sub-object. The query was recovered from the
compiled AST in the frontend bundle (search `FormRenderParts` in
`cdn.ashbyprd.com/frontend_non_user/<hash>/assets/index-*.js`); the hash
changes on deploy, so re-read the bundle if the query stops resolving.
`field.path` is the id space everything here keys on (`_systemfield_*`, a
UUID for employer-authored questions), and the live form carries it as
`data-field-path` on each question's wrapper.

The API is not the whole story either: a board can render instructional text
under a label ("if yes, please explain briefly", "please don't write AI slop
here") that `applicationForm` never declares. `load_board` closes that gap
with `fetch_dom_enrichment`/`_with_dom_descriptions`: one headless page load
that reads every field's sibling description div generically, keyed by
`data-field-path`, rather than matching known labels. It is the one place
`apply plan`'s otherwise browser-free path opens a browser, and it degrades to
no descriptions rather than failing the plan if that browser is unavailable.

Filling is `fill.AshbyBrowserDriver`, which reads each field's widget
off the live DOM — the API type does not name it.

Every `MergedField.id`/`.name` is that path verbatim, except the resume and
cover-letter uploads, aliased to Greenhouse's "resume"/"cover_letter" so
`answers.py`'s `FILE_IDS` and `plan.py`'s `find_artifact` recognize them
. A boolean is `kind="yesno"` with a fixed Yes/No option pair, so the
A2/B0 opt-out and work-authorization logic (which reads `field.options`)
works unchanged.

`fields_from_application_form` is pure; network lives in `load_board`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from urllib.parse import unquote, urlparse

from src.apply.browser import launch, require_playwright
from src.apply.domscan import DomScanError
from src.apply.greenhouse import ApplyUrlError, PostingExpired
from src.apply.reconcile import MergedField, MergedOption, Reconciled
from src.shared import job_boards
from src.shared.http_client import fetch_json_post

log = logging.getLogger(__name__)

ROOT_ID = "form"

API_URL = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApplicationForm"

# `field` is a JSON scalar — the whole field definition comes back in it, and
# asking for sub-selections on it is a GraphQL error. Keep it bare.
APPLICATION_FORM_QUERY = (
    "query ApplicationForm($organizationHostedJobsPageName: String!, "
    "$jobPostingId: String!) { jobPosting("
    "organizationHostedJobsPageName: $organizationHostedJobsPageName, "
    "jobPostingId: $jobPostingId) { id title applicationForm { id sections "
    "{ title descriptionHtml fieldEntries { id isRequired field } } } } }"
)

# Ashby `field.type` -> the `MergedField.kind` the rest of /apply speaks.
# Measured over 150 live boards; every type below was observed. `isMany` — not
# the type name — carries cardinality: `MultiValueSelect` means "choose among
# several values", not "choose several", and came back `isMany: false` on all
# 39 occurrences. No board in the sample had `isMany: true` at all.
_TYPE_KINDS = {
    "String": "text",
    "Email": "text",
    "Phone": "text",
    "Url": "text",
    "Number": "text",
    # Not free text: the API says "Date" up front, and the rendered widget is
    # a `react-datepicker` `<input type="text">` that silently clears on blur
    # if it cannot parse what was typed — a Tier B sentence like "Immediately."
    # types in, reads back fine at fill-time, then vanishes once the field
    # loses focus, leaving the board showing its empty picker. Kept as its own
    # kind so `answers.py` resolves it as a real date rather than free text.
    "Date": "date",
    "LongText": "textarea",
    "File": "file",
    # Two buttons and a hidden checkbox in the DOM; a bare true/false here.
    # Kept as `yesno` with a fixed option pair so the existing A2/B0 opt-out
    # and work-authorization logic (which reads `field.options`) works
    # unchanged.
    "Boolean": "yesno",
    "ValueSelect": "select",
    "MultiValueSelect": "select",
    # A remote place-name taxonomy with no option list of its own — and one
    # asking a different question per board: city-level on most, country-only
    # on some.
    "Location": "combobox",
}

# The resume and cover-letter uploads alias to the ids `answers.FILE_IDS` and
# `plan.find_artifact` defer to /tailor for. Each alias goes to one field: the
# systemfield when the form has one, else the first upload whose title names
# it (some boards render the resume under a UUID path). Any other upload
# keeps its own path, even one whose title says "CV".
_FILE_ALIASES = (
    ("resume", frozenset({"_systemfield_resume"}), re.compile(r"\bresume\b|\bcv\b", re.IGNORECASE)),
    (
        "cover_letter",
        frozenset({"cover_letter", "_systemfield_coverletter"}),
        re.compile(r"cover\s*letter", re.IGNORECASE),
    ),
)

# `EducationHistory` is not a question — it is a repeating sub-form delivered
# as ONE field entry, with each sub-field's requirement declared inline:
#
#   {"type": "EducationHistory", "schoolName": "required", "degree": "optional",
#    "major": "optional", "startDate": "optional", "endDate": "optional",
#    "isRepeatable": true, "minRepeat": 1}
#
# Greenhouse sends the same block as separate `school--0`/`degree--0` controls,
# which `answers.py` already resolves, so the entry is expanded into those ids
# rather than given a composite kind of its own. Aliasing to Greenhouse's id
# space is the same trade the two file fields make.
#
# The two dates are deliberately unmapped. `education.start_year`/`end_year`
# are years and these fields want dates, so a mapping would write a wrong
# value. An unmapped sub-field is dropped while optional (the only shape
# observed) and emitted when required, where it parks loudly.
EDUCATION_HISTORY_TYPE = "EducationHistory"
_EDUCATION_SUBFIELDS = {
    "schoolName": "school--0",
    "degree": "degree--0",
    "major": "discipline--0",
}
_EDUCATION_SUBFIELD_ORDER = ("schoolName", "degree", "major", "startDate", "endDate")

# Which control inside the block's one `data-field-path`. schoolName is a
# combobox carrying no id; the other two carry `<path>-<sub>`.
_EDUCATION_CONTROLS = {
    "schoolName": 'input[role="combobox"]',
    "degree": '[id="{path}-degree"]',
    "major": '[id="{path}-major"]',
}

# Ashby renders schoolName as a combobox and the other two as plain text —
# Greenhouse makes all three selects. Typed text alone clears on blur.
_EDUCATION_KINDS = {"schoolName": "react_select"}

_URL = job_boards.get_source("ashby").posting_url_re

_QUESTION_DESCRIPTION_CLASS = "ashby-application-form-question-description"
_SUBMIT_CLASS = "ashby-application-form-submit-button"
#: Stated without the DOM in hand: the class is unhashed and resolves to
#: exactly one button on every live board measured.
SUBMIT_SELECTOR = f"#{ROOT_ID} .{_SUBMIT_CLASS}"


class AshbyScanError(DomScanError):
    """The Ashby form API returned something this module cannot represent."""


@dataclass(frozen=True)
class Posting:
    slug: str
    job_id: str

    @property
    def token(self) -> str:
        """Duck-typed for `plan_for_board`, which reads `.posting.token` with
        no idea which ATS it came from."""
        return self.job_id

    @property
    def posting_url(self) -> str:
        """The job ad. Statically identical to the form URL — both serve the
        same ~32 KB client-rendered shell — but they render differently, and
        only one of them renders the form."""
        return f"https://jobs.ashbyhq.com/{self.slug}/{self.job_id}"

    @property
    def form_url(self) -> str:
        """What a browser must open to see the application form, matching
        Greenhouse's embed URL and Lever's `/apply`. This was the bare posting
        while Ashby was plan-only and nothing navigated to it — as a driver
        target it renders the ad, and the fill times out waiting for fields
        that page never draws."""
        return f"{self.posting_url}/application"


@dataclass(frozen=True)
class _Scan:
    submit_selector: str | None
    submit_disabled: bool


@dataclass(frozen=True)
class _Schema:
    company_name: str
    title: str


@dataclass(frozen=True)
class AshbyBoard:
    """Same attribute shape as `greenhouse.BoardForm` — `plan_for_board` is
    duck-typed over it on purpose."""

    posting: Posting
    slug: str
    scan: _Scan
    schema: _Schema
    reconciled: Reconciled
    html: str
    requires_captcha: bool


def parse_posting(url: str) -> Posting:
    text = (url or "").strip()
    if not text:
        raise ApplyUrlError("no URL")
    match = _URL.match(text)
    if not match:
        if "ashbyhq.com" in (urlparse(text).hostname or ""):
            raise ApplyUrlError(f"Ashby URL with no job id: {text}")
        raise ApplyUrlError(f"not an Ashby posting URL: {text}")
    return Posting(slug=match.group("slug"), job_id=match.group("posting_id"))


def fetch_application_form(posting: Posting, timeout: int = 30) -> dict:
    """The posting's `jobPosting` node, questions included.

    A null `jobPosting` is Ashby's shape for "no such posting" — the API
    answers 200 either way — so it maps to `PostingExpired`, the same ordinary
    outcome as a stale Greenhouse token. 5 of the first 40 live URLs tried
    came back this way.
    """
    body = {
        "operationName": "ApplicationForm",
        "variables": {
            # The org segment of the URL is percent-encoded for orgs whose
            # page name has a space in it ("Hippocratic%20AI"); the API wants
            # the decoded name.
            "organizationHostedJobsPageName": unquote(posting.slug),
            "jobPostingId": posting.job_id,
        },
        "query": APPLICATION_FORM_QUERY,
    }
    payload = fetch_json_post(API_URL, body, timeout=timeout)
    if not isinstance(payload, dict):
        raise AshbyScanError(f"{posting.form_url}: API returned {type(payload).__name__}")
    if payload.get("errors"):
        messages = "; ".join(
            str(e.get("message", e)) for e in payload["errors"] if isinstance(e, dict)
        )
        raise AshbyScanError(f"{posting.form_url}: API errors: {messages}")

    job_posting = (payload.get("data") or {}).get("jobPosting")
    if not job_posting:
        raise PostingExpired(f"posting {posting.job_id} is gone: {posting.form_url}")
    if not job_posting.get("applicationForm"):
        raise AshbyScanError(
            f"{posting.form_url}: the posting exists but carries no applicationForm"
        )
    return job_posting


def _file_aliases(job_posting: dict) -> dict[str, str]:
    """File path -> "resume"/"cover_letter", for the one upload each alias
    goes to. Every other upload ("Additional Attachments", a writing sample)
    is absent here and keeps its own id: skipped when optional, parked when
    required."""
    uploads = [
        (field.get("path") or "", field.get("title") or "")
        for section in job_posting["applicationForm"].get("sections") or []
        for entry in section.get("fieldEntries") or []
        if isinstance(entry, dict)
        for field in [entry.get("field")]
        if isinstance(field, dict) and field.get("type") == "File"
    ]
    aliases: dict[str, str] = {}
    for alias, system_paths, title_re in _FILE_ALIASES:
        pick = next((p for p, _ in uploads if p in system_paths), None)
        if pick is None:
            pick = next((p for p, t in uploads if p not in aliases and title_re.search(t)), None)
        if pick is not None and pick not in aliases:
            aliases[pick] = alias
    return aliases


def _education_history_fields(field: dict, path: str) -> list[MergedField]:
    """The composite education block as the scalar fields the planner answers.

    Only the first entry, matching `answers.py`'s existing "entry 0 only"
    convention for Greenhouse's repeating blocks. `minRepeat` is 1 on the one
    board observed; a board demanding more would still fill the first and park
    nothing, which is the same exposure Greenhouse already carries.
    """
    fields = []
    for sub in _EDUCATION_SUBFIELD_ORDER:
        declared = field.get(sub)
        if declared is None:
            continue  # this board does not collect that sub-field
        required = str(declared).lower() == "required"
        if sub not in _EDUCATION_SUBFIELDS and not required:
            # An unmapped sub-field is dropped while optional rather than
            # emitted: `_resolve_repeating` parks an id it does not recognize
            # whether or not it is required, so emitting the optional dates
            # would park every board that renders them — the opposite of
            # leaving them alone. Required ones are still emitted, and park
            # loudly, which is the outcome that deserves attention.
            continue
        fields.append(
            MergedField(
                id=_EDUCATION_SUBFIELDS.get(sub, f"{path}.{sub}"),
                name=_EDUCATION_SUBFIELDS.get(sub, f"{path}.{sub}"),
                label=f"{field.get('title') or 'Education'}: {sub}",
                required=required,
                kind=_EDUCATION_KINDS.get(sub, "text"),
                # `id` above is Greenhouse's, for `answers.py`. Ashby renders the
                # whole block under one path, so the DOM lookup needs the real
                # one plus which control in it.
                dom_path=path,
                dom_control=_EDUCATION_CONTROLS.get(sub, "").format(path=path),
                # `resolve` dispatches the repeating blocks on section, not on id,
                # so this is what routes these to `answers.education` rather than
                # leaving them to the keyword rules.
                section="education",
                multi=False,
                options=(),
            )
        )
    if not fields:
        raise AshbyScanError(f"{path}: an EducationHistory field declaring no sub-fields")
    return fields


def _api_field(entry: dict, file_aliases: dict[str, str]) -> list[MergedField]:
    field = entry.get("field")
    if not isinstance(field, dict):
        raise AshbyScanError(f"fieldEntry {entry.get('id')!r} carries no field object")

    path = field.get("path") or ""
    if not path:
        raise AshbyScanError(f"fieldEntry {entry.get('id')!r} has no field.path")
    raw_type = field.get("type") or ""
    if raw_type == EDUCATION_HISTORY_TYPE:
        return _education_history_fields(field, path)
    kind = _TYPE_KINDS.get(raw_type)
    if kind is None:
        # Loud rather than guessed.
        raise AshbyScanError(
            f"unknown Ashby field type {raw_type!r} on path={path!r} ({field.get('title')!r})"
        )

    field_id = file_aliases.get(path, path) if kind == "file" else path

    if kind == "yesno":
        options = (MergedOption(label="Yes", value="yes"), MergedOption(label="No", value="no"))
    else:
        options = tuple(
            MergedOption(label=str(v.get("label", "")), value=str(v.get("value", "")))
            for v in (field.get("selectableValues") or [])
            if isinstance(v, dict)
        )

    return [
        MergedField(
            id=field_id,
            # File fields alias `id` to the canonical `resume`/`cover_letter` the
            # planner speaks, but the DOM still keys the field by its real path —
            # `_systemfield_resume` on most boards, an employer-authored UUID on
            # Nen. `name` carries that real path through to fill.py rather than
            # a static id->path table, which only ever knew the two systemfield
            # spellings and left every other real path unreachable.
            name=path if kind == "file" else field_id,
            label=field.get("title") or "",
            required=bool(entry.get("isRequired")),
            kind=kind,
            # Ashby's demographic questionnaire is a separate form this query does
            # not return — no section across 150 boards was one — so everything
            # here is an ordinary application question.
            section="questions",
            multi=bool(field.get("isMany")),
            options=options,
        )
    ]


def fields_from_application_form(job_posting: dict) -> Reconciled:
    """The API's `applicationForm` as a `Reconciled`, in the order the form
    presents it. A field repeated under the same path is kept once; two
    different fields landing on one id raise rather than dropping either."""
    file_aliases = _file_aliases(job_posting)
    seen: dict[str, str] = {}
    fields: list[MergedField] = []
    for section in job_posting["applicationForm"].get("sections") or []:
        for entry in section.get("fieldEntries") or []:
            if not isinstance(entry, dict):
                continue
            for merged in _api_field(entry, file_aliases):
                source = merged.dom_path or merged.name
                if merged.id in seen:
                    if seen[merged.id] == source:
                        continue
                    raise AshbyScanError(
                        f"two fields map to id {merged.id!r}: {seen[merged.id]!r} and {source!r}"
                    )
                seen[merged.id] = source
                fields.append(merged)

    if not fields:
        raise AshbyScanError("the application form declares no fields")
    return Reconciled(fields=tuple(fields), api_only=())


_DOM_ENRICHMENT_SCRIPT = """(elements, descriptionClass) => {
    const out = {};
    for (const el of elements) {
        const path = el.getAttribute('data-field-path');
        if (!path || out[path]) continue;
        const desc = el.querySelector('.' + descriptionClass);
        const text = desc ? desc.textContent.trim() : '';
        if (text) out[path] = text;
    }
    return out;
}"""


def fetch_dom_enrichment(url: str, timeout: int = 30) -> dict[str, str]:
    """`data-field-path` -> its sibling description text, read off the live,
    client-rendered form — the same instructional copy a person sees under a
    label but the question API never returns (a sponsorship follow-up, a "no
    AI slop" prompt). Generalized to any field on any board, never matched by
    known label text.

    One headless page load, one in-page query keyed on `data-field-path` (the
    same id space everything else here uses), no interaction. Best-effort by
    construction: `_with_dom_descriptions` swallows anything this raises,
    including the `SystemExit` `require_playwright()` raises when the driver
    is not installed, so a role simply gets no descriptions — the same
    outcome as before this existed, never a blocked plan.
    """
    sync_playwright = require_playwright()
    with sync_playwright() as p:
        context = launch(p, headless=True)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            page.wait_for_selector("[data-field-path]", timeout=timeout * 1000)
            return page.eval_on_selector_all(
                "[data-field-path]",
                _DOM_ENRICHMENT_SCRIPT,
                _QUESTION_DESCRIPTION_CLASS,
            )
        finally:
            context.close()


def _with_dom_descriptions(reconciled: Reconciled, form_url: str, timeout: int) -> Reconciled:
    """Fold `fetch_dom_enrichment` into an already-built `Reconciled`, field by
    field. Never raises: a missing browser, a timeout, or a layout this was
    never written for all just mean no descriptions get added, same as the
    API-only plan this sits on top of.
    """
    try:
        descriptions = fetch_dom_enrichment(form_url, timeout=timeout)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - enrichment only, never fatal
        log.warning("Ashby DOM enrichment skipped for %s: %s", form_url, exc)
        return reconciled
    if not descriptions:
        return reconciled
    fields = tuple(
        replace(f, description=descriptions[f.name]) if f.name in descriptions else f
        for f in reconciled.fields
    )
    return replace(reconciled, fields=fields)


def load_board(url: str, timeout: int = 30) -> AshbyBoard:
    """Posting URL -> an Ashby board, `plan_for_board`-shaped. One POST.

    The submit button is named by a stable, unhashed class, and resolves to
    exactly one element on every live board measured — so this path can state
    the selector without having the DOM in hand. It is only ever a click if
    `fill.submit` is reached, which the guard gates on a fully resolved plan.

    `submit_disabled` is False here because the API says nothing about it;
    `submit_disabled_now` re-reads the live button before any click, which is
    the check that actually matters.

    `requires_captcha` is False on evidence, not by omission: Ashby loads
    reCAPTCHA **v3** (`api.js?render=`, a `grecaptcha-badge`), which is
    invisible and score-based, with no challenge for a human to solve. That is
    a different thing from Lever's hCaptcha, which does block on a person. The
    v3 score is still a bot-detection surface — a low score can have a
    submission silently rejected — but nothing here can wait it out.

    One more network trip than the API alone needs: `_with_dom_descriptions`
    opens a headless browser to read whatever instructional text a board
    renders under a label but never declares in `applicationForm`. This
    is the one place in `/apply`'s "plan" path that touches a browser at all —
    everywhere else, `apply plan` is deliberately browser-free — and it is
    scoped to Ashby only, best-effort, and never blocks the plan if it fails.
    """
    posting = parse_posting(url)
    job_posting = fetch_application_form(posting, timeout=timeout)
    reconciled = fields_from_application_form(job_posting)
    reconciled = _with_dom_descriptions(reconciled, posting.form_url, timeout=timeout)
    return AshbyBoard(
        posting=posting,
        slug=posting.slug,
        scan=_Scan(submit_selector=SUBMIT_SELECTOR, submit_disabled=False),
        schema=_Schema(company_name=unquote(posting.slug), title=job_posting.get("title") or ""),
        reconciled=reconciled,
        html="",
        requires_captcha=False,
    )
