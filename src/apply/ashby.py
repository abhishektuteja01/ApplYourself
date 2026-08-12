"""Ashby application form: read the question API into a merged field set.

**The form is client-rendered, so there is no HTML to scan.** Measured over 6
orgs: a static GET of a live posting returns the same ~32 KB shell with zero
`<form>` elements and zero `data-field-path` attributes, and `<url>` and
`<url>/application` are byte-identical. The committed `form_ashby_*.html`
fixtures are browser-DOM snapshots — they carry computed inline styles and
hashed CSS-module class names that only exist post-render — which is why the
DOM scanner below passed its tests while failing on every real URL.

`load_board` therefore reads Ashby's own anonymous GraphQL endpoint instead:

    POST https://jobs.ashbyhq.com/api/non-user-graphql?op=ApplicationForm

The collection is `fieldEntries` and `field` is a **JSON scalar**, so the
whole definition (`type`, `path`, `title`, `selectableValues`) arrives in one
blob rather than as a selectable sub-object — which is why probing GraphQL
field names never found it. Recovered from the compiled query AST in the
frontend bundle (search `FormRenderParts` in
`cdn.ashbyprd.com/frontend_non_user/<hash>/assets/index-*.js`); the hash
changes on deploy, so re-read the bundle if the query stops resolving.

`field.path` is exactly the id space this module already keyed on
(`_systemfield_*`, a UUID for employer-authored questions), so nothing
downstream had to change.

`scan_ashby_form` is kept as the only reader for a *rendered* Ashby form, and
the fixtures still exercise it, but it is no longer on the `load_board` path.

Filling is `fill.AshbyBrowserDriver` (§12a), which reads each field's widget
off the live DOM — the API type does not name it.

Three things Ashby needed that neither Greenhouse nor Lever did:

- **`data-field-path` is the universal id**, not any control's own `id`/
  `name`. Some controls (the location combobox) carry neither attribute at
  all; the diversity survey's checkboxes carry a `name` that is the option's
  own label text, not a field name. `data-field-path` sits on every question's
  wrapping element (the field-entry `<div>` itself, or the fieldset's parent
  `<div>` for the two grouped-option shapes below) and is stable across all of
  them, so every `MergedField.id`/`.name` here is that value verbatim — except
  the two file fields, aliased to Greenhouse's "resume"/"cover_letter" so
  `answers.py`'s `FILE_IDS` and `plan.py`'s `find_artifact` still recognize
  them (§12a).
- **Required is a CSS class, not an attribute.** Ashby's build hashes most
  class names per-deploy, but a `_required_<hash>` token survives as a
  detectable *prefix* on the question's `<label>` — matched by prefix, never
  the full hashed name, since the suffix is expected to change between
  deploys/versions.
- **A yes/no toggle has no native control semantics at all** — two `<button>`s
  and a hidden, unlabelled checkbox, no ARIA role anywhere. Modelled as its
  own `kind="yesno"` with a fixed Yes/No option pair, so the existing
  A2/B0 opt-out and work-authorization resolution logic (which reads
  `field.options`) works unchanged.

Pure function over an HTML string for the scan; network lives in `load_board`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

from lxml import html as lxml_html

from src.apply.domscan import DomScanError
from src.apply.greenhouse import ApplyUrlError, PostingExpired
from src.apply.reconcile import MergedField, MergedOption, Reconciled
from src.discovery.sources.ats.http import CareersError, fetch_json_post, fetch_text

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
    "Date": "text",
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
    # on some (§12a).
    "Location": "combobox",
}

# Ashby names the resume with a systemfield on most boards, but Nen (and
# presumably others) renders it as an employer-authored UUID path instead —
# the same shape the cover letter already needed a title fallback for. Both
# alias to the ids `answers.FILE_IDS` and `plan.find_artifact` already defer
# to /tailor for.
_RESUME_PATHS = frozenset({"_systemfield_resume"})
_RESUME_TITLE = re.compile(r"\bresume\b|\bcv\b", re.IGNORECASE)
_COVER_LETTER_PATHS = frozenset({"cover_letter", "_systemfield_coverletter"})
_COVER_LETTER_TITLE = re.compile(r"cover\s*letter", re.IGNORECASE)

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

_URL = re.compile(
    r"^https?://jobs\.ashbyhq\.com/(?P<slug>[^/?#]+)/(?P<job_id>[0-9a-f-]+)",
    re.IGNORECASE,
)

_FIELD_ENTRY_PREFIX = re.compile(r"^_fieldEntry_")
_REQUIRED_PREFIX = re.compile(r"^_required_")
_YESNO_TOKEN = re.compile(r"_yesno_")
_SURVEY_CLASS = "ashby-survey-form-container"
_QUESTION_TITLE_CLASS = "ashby-application-form-question-title"
_SUBMIT_CLASS = "ashby-application-form-submit-button"
#: One selector for both paths — the DOM scan and the API `load_board`, which
#: has no HTML to check. The class is unhashed and resolves to exactly one
#: button on every live board measured, so stating it without the DOM in hand
#: is a fact rather than a guess. Two spellings would be two guesses.
SUBMIT_SELECTOR = f'#{ROOT_ID} .{_SUBMIT_CLASS}'

_INPUT_TEXT_TYPES = {"text", "email", "tel", "url", "search"}
_WS = re.compile(r"\s+")

# The two file fields' raw systemfield ids -> the ids answers.py/plan.py
# already know how to defer to /tailor's output (§12a). DOM-scan path only;
# the live API path aliases dynamically in `_api_file_id` and carries the
# real DOM path through as `MergedField.name` instead of a static table.
_FILE_FIELD_IDS = {
    "_systemfield_resume": "resume",
    "_systemfield_coverletter": "cover_letter",
}


class AshbyScanError(DomScanError):
    """The rendered Ashby form is not shaped the way this scanner can read."""


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
    duck-typed over it on purpose (§12a)."""
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
    return Posting(slug=match.group("slug"), job_id=match.group("job_id"))


def _classes(el) -> list[str]:
    return (el.get("class") or "").split()


def _text(el) -> str:
    return _WS.sub(" ", "".join(el.itertext())).strip()


def _has_class_prefix(el, prefix: re.Pattern) -> bool:
    return any(prefix.match(c) for c in _classes(el))


def _is_field_entry(el) -> bool:
    return el.tag in ("div", "fieldset") and _has_class_prefix(el, _FIELD_ENTRY_PREFIX)


def _label_el(container):
    labels = container.xpath(f'.//*[contains(@class, "{_QUESTION_TITLE_CLASS}")]')
    return labels[0] if labels else None


def _field_path(container) -> str | None:
    fp = container.get("data-field-path")
    if fp:
        return fp
    parent = container.getparent()
    return parent.get("data-field-path") if parent is not None else None


def _is_demographic(container) -> bool:
    node = container.getparent()
    while node is not None:
        if _SURVEY_CLASS in _classes(node):
            return True
        node = node.getparent()
    return False


def _yesno_field(container, field_id: str, label: str, required: bool, section: str) -> MergedField:
    options = (MergedOption(label="Yes", value="yes"), MergedOption(label="No", value="no"))
    return MergedField(
        id=field_id, name=field_id, label=label, required=required,
        kind="yesno", section=section, multi=False, options=options,
    )


def _combobox_field(container, field_id: str, label: str, required: bool, section: str) -> MergedField:
    return MergedField(
        id=field_id, name=field_id, label=label, required=required,
        kind="combobox", section=section, multi=False, options=(),
    )


def _grouped_field(container, field_id: str, label: str, required: bool, section: str) -> MergedField:
    """The diversity survey's `<fieldset>` shapes: a shared-name radio group
    (single-select) or a checkbox group where each option's own `name` is its
    label text (multi-select) — never both in the same fieldset."""
    radios = container.xpath('.//input[@type="radio"]')
    if radios:
        name = radios[0].get("name") or ""
        options = []
        for radio in radios:
            labels = container.xpath(f'.//label[@for="{radio.get("id")}"]')
            opt_label = _text(labels[0]) if labels else name
            options.append(MergedOption(label=opt_label, value=radio.get("value") or opt_label))
        return MergedField(
            id=field_id, name=field_id, label=label, required=required,
            kind="radio_group", section=section, multi=False, options=tuple(options),
        )

    checkboxes = container.xpath('.//input[@type="checkbox"]')
    if checkboxes:
        options = []
        for box in checkboxes:
            box_name = box.get("name") or ""
            labels = container.xpath(f'.//label[@for="{box.get("id")}"]')
            # `value` and `label` are the same string here only because
            # Ashby's markup happens to spell a checkbox's name as its own
            # label text — answers.py's opt-out matching (_pick_option) goes
            # by label, not value, so this coincidence is not load-bearing,
            # but a future board where the two diverge would need value read
            # from somewhere else.
            opt_label = _text(labels[0]) if labels else box_name
            options.append(MergedOption(label=opt_label, value=box_name))
        return MergedField(
            id=field_id, name=field_id, label=label, required=required,
            kind="checkbox_group", section=section, multi=True, options=tuple(options),
        )

    raise AshbyScanError(f"fieldset {field_id!r}: neither radio nor checkbox inputs found")


def _select_options(select_el) -> tuple[MergedOption, ...]:
    options = []
    for opt in select_el.xpath("./option"):
        value = opt.get("value") or ""
        if not value:
            continue  # the placeholder
        options.append(MergedOption(label=_text(opt), value=value))
    return tuple(options)


def _control_field(container, field_id: str, label: str, required: bool, section: str) -> MergedField | None:
    controls = container.xpath(
        './/input[@type!="hidden" and @type!="radio" and @type!="checkbox"] '
        "| .//select | .//textarea"
    )
    if not controls:
        return None
    control = controls[0]
    required = required or control.get("required") is not None

    tag = control.tag
    if tag == "select":
        kind, options = "select", _select_options(control)
    elif tag == "textarea":
        kind, options = "textarea", ()
    else:
        raw = (control.get("type") or "text").lower()
        if raw == "file":
            kind, options = "file", ()
            field_id = _FILE_FIELD_IDS.get(field_id, field_id)
        elif raw in _INPUT_TEXT_TYPES:
            kind, options = "text", ()
        else:
            raise AshbyScanError(f"unknown input type {raw!r} on data-field-path={field_id!r}")

    return MergedField(
        id=field_id, name=field_id, label=label, required=required,
        kind=kind, section=section, multi=False, options=options,
    )


def _scan_field(container) -> MergedField | None:
    field_id = _field_path(container)
    if not field_id:
        raise AshbyScanError("a field-entry has no data-field-path, on itself or its parent")

    label_el = _label_el(container)
    label = _text(label_el) if label_el is not None else ""
    required = label_el is not None and _has_class_prefix(label_el, _REQUIRED_PREFIX)
    section = "demographic" if _is_demographic(container) else "questions"

    if container.tag == "fieldset":
        return _grouped_field(container, field_id, label, required, section)
    if any(_YESNO_TOKEN.search(c) for el in container.iter() for c in _classes(el)):
        return _yesno_field(container, field_id, label, required, section)
    if container.xpath('.//*[@role="combobox"]'):
        return _combobox_field(container, field_id, label, required, section)
    return _control_field(container, field_id, label, required, section)


def scan_ashby_form(page_html: str) -> Reconciled:
    """Read every fillable control out of a rendered Ashby application form."""
    if not page_html or not page_html.strip():
        raise AshbyScanError("empty document")
    doc = lxml_html.fromstring(page_html)
    roots = doc.xpath(f'//*[@id="{ROOT_ID}"]')
    if not roots:
        raise AshbyScanError(f'no element with id="{ROOT_ID}" in the document')
    root = roots[0]

    seen_ids: set[str] = set()
    fields: list[MergedField] = []
    for el in root.iter():
        if not _is_field_entry(el):
            continue
        field = _scan_field(el)
        if field is None:
            continue
        if field.id in seen_ids:
            continue
        seen_ids.add(field.id)
        fields.append(field)

    if not fields:
        raise AshbyScanError("no fillable fields found under the form root")
    return Reconciled(fields=tuple(fields), api_only=())


def _submit_scan(root) -> _Scan:
    submits = root.xpath(f'.//button[contains(@class, "{_SUBMIT_CLASS}")]')
    if len(submits) > 1:
        raise AshbyScanError(f"expected one submit button, found {len(submits)}")
    if not submits:
        return _Scan(submit_selector=None, submit_disabled=False)
    submit = submits[0]
    disabled = submit.get("disabled") is not None or submit.get("aria-disabled") == "true"
    return _Scan(
        submit_selector=SUBMIT_SELECTOR,
        submit_disabled=disabled,
    )


def fetch_form(posting: Posting, timeout: int = 30) -> str:
    """The rendered apply page. Raises PostingExpired on 404 — same ordinary
    outcome as a stale Greenhouse token (§14)."""
    try:
        return fetch_text(posting.form_url, timeout=timeout)
    except CareersError as exc:
        if exc.status == 404:
            raise PostingExpired(
                f"posting {posting.job_id} is gone (404): {posting.form_url}"
            ) from exc
        raise


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
        raise PostingExpired(
            f"posting {posting.job_id} is gone: {posting.form_url}"
        )
    if not job_posting.get("applicationForm"):
        raise AshbyScanError(
            f"{posting.form_url}: the posting exists but carries no applicationForm"
        )
    return job_posting


def _api_file_id(path: str, title: str) -> str:
    if path in _RESUME_PATHS or _RESUME_TITLE.search(title or ""):
        return "resume"
    if path in _COVER_LETTER_PATHS or _COVER_LETTER_TITLE.search(title or ""):
        return "cover_letter"
    # "Additional Attachments", a portfolio screenshot: a real optional upload
    # with no /tailor artifact behind it. Left under its own id so it resolves
    # normally — skipped when optional, parked when required.
    return path


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
            continue      # this board does not collect that sub-field
        required = str(declared).lower() == "required"
        if sub not in _EDUCATION_SUBFIELDS and not required:
            # An unmapped sub-field is dropped while optional rather than
            # emitted: `_resolve_repeating` parks an id it does not recognize
            # whether or not it is required, so emitting the optional dates
            # would park every board that renders them — the opposite of
            # leaving them alone. Required ones are still emitted, and park
            # loudly, which is the outcome that deserves attention.
            continue
        fields.append(MergedField(
            id=_EDUCATION_SUBFIELDS.get(sub, f"{path}.{sub}"),
            name=_EDUCATION_SUBFIELDS.get(sub, f"{path}.{sub}"),
            label=f"{field.get('title') or 'Education'}: {sub}",
            required=required,
            kind="text",
            # `resolve` dispatches the repeating blocks on section, not on id,
            # so this is what routes these to `answers.education` rather than
            # leaving them to the keyword rules.
            section="education",
            multi=False,
            options=(),
        ))
    if not fields:
        raise AshbyScanError(
            f"{path}: an EducationHistory field declaring no sub-fields"
        )
    return fields


def _api_field(entry: dict) -> list[MergedField]:
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
        # Loud rather than guessed, matching the DOM scanner's unknown-input
        # behaviour.
        raise AshbyScanError(
            f"unknown Ashby field type {raw_type!r} on path={path!r} "
            f"({field.get('title')!r})"
        )

    field_id = _api_file_id(path, field.get("title") or "") if kind == "file" else path

    if kind == "yesno":
        options = (MergedOption(label="Yes", value="yes"),
                   MergedOption(label="No", value="no"))
    else:
        options = tuple(
            MergedOption(label=str(v.get("label", "")), value=str(v.get("value", "")))
            for v in (field.get("selectableValues") or [])
            if isinstance(v, dict)
        )

    return [MergedField(
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
    )]


def fields_from_application_form(job_posting: dict) -> Reconciled:
    """The API's `applicationForm` as the same `Reconciled` the DOM scan
    produces. Deduped by id, in the order the form presents them."""
    seen: set[str] = set()
    fields: list[MergedField] = []
    for section in job_posting["applicationForm"].get("sections") or []:
        for entry in section.get("fieldEntries") or []:
            if not isinstance(entry, dict):
                continue
            for merged in _api_field(entry):
                if merged.id in seen:
                    continue
                seen.add(merged.id)
                fields.append(merged)

    if not fields:
        raise AshbyScanError("the application form declares no fields")
    return Reconciled(fields=tuple(fields), api_only=())


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
    """
    posting = parse_posting(url)
    job_posting = fetch_application_form(posting, timeout=timeout)
    reconciled = fields_from_application_form(job_posting)
    return AshbyBoard(
        posting=posting,
        slug=posting.slug,
        scan=_Scan(submit_selector=SUBMIT_SELECTOR, submit_disabled=False),
        schema=_Schema(company_name=unquote(posting.slug),
                       title=job_posting.get("title") or ""),
        reconciled=reconciled,
        html="",
        requires_captcha=False,
    )
