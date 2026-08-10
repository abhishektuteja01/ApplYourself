"""Ashby application form: read the rendered DOM into a merged field set.

Same reason as Lever (`lever.py`) for skipping `schema.py`/`reconcile.py`
entirely: no public question API, so the rendered form is the only source of
truth and `MergedField`s are emitted straight from the scan.

**Scan only — no fill driver (§12a).** Three of Ashby's control shapes cannot
be verified from a static fixture: the open-state markup of the `role`
`combobox` (location), the post-click state of the yes/no toggle, and the
checkbox-group selector path via a fieldset one level below its own
`data-field-path`. Building `fill.py` support for these against guesses
rather than an observed live form would be the exact kind of speculative work
this repo's plan (§12a) tries to avoid. `_DRIVER_NAMES` in `fill.py` has no
"ashby" entry, so `apply plan` works fully for an Ashby posting and
`apply fill`/`apply run` refuse loudly (`FillError`) until a driver exists.

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
  them (§12a; a real DOM selector for these two is a driver-side problem, not
  a scan-side one).
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
from urllib.parse import urlparse

from lxml import html as lxml_html

from src.apply.domscan import DomScanError
from src.apply.greenhouse import ApplyUrlError, PostingExpired
from src.apply.reconcile import MergedField, MergedOption, Reconciled
from src.discovery.sources.ats.http import CareersError, fetch_text

ROOT_ID = "form"

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

_INPUT_TEXT_TYPES = {"text", "email", "tel", "url", "search"}
_WS = re.compile(r"\s+")

# The two file fields' raw systemfield ids -> the ids answers.py/plan.py
# already know how to defer to /tailor's output (§12a).
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
    def form_url(self) -> str:
        return f"https://jobs.ashbyhq.com/{self.slug}/{self.job_id}"


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
        submit_selector=f'#{ROOT_ID} .{_SUBMIT_CLASS}',
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


def load_board(url: str, timeout: int = 30) -> AshbyBoard:
    """Posting URL -> an Ashby board, `plan_for_board`-shaped. One GET."""
    posting = parse_posting(url)
    html = fetch_form(posting, timeout=timeout)
    doc = lxml_html.fromstring(html)
    roots = doc.xpath(f'//*[@id="{ROOT_ID}"]')
    if not roots:
        raise AshbyScanError(f'no element with id="{ROOT_ID}" in the document')
    scan = _submit_scan(roots[0])
    reconciled = scan_ashby_form(html)
    return AshbyBoard(
        posting=posting,
        slug=posting.slug,
        scan=scan,
        schema=_Schema(company_name="", title=""),
        reconciled=reconciled,
        html=html,
        requires_captcha=False,
    )
