"""Lever application form: read the rendered DOM into a merged field set.

Unlike Greenhouse there is no public question API to reconcile against — the
form is a native, server-rendered `<form>` and everything a submission needs
(label, required, options) is already in that one HTML document. So this
module skips `schema.py`/`reconcile.py` entirely and emits `MergedField`s
straight from the scan; `answers.py`/`plan.py`/`fill.py` downstream take a
`Reconciled` however it was produced and do not know this is Lever.

Two things Greenhouse never needed to handle:

- **Fields key off `name`, not `id`.** Lever's core/custom-question inputs
  carry no `id` attribute at all, so `MergedField.id` (and `.name`) is the
  `name` attribute verbatim — unlike Greenhouse, this module never aliases
  id away from the real DOM attribute, because `fill.py`'s Lever driver
  selects elements by `name` and needs `field.id` to still be that value.
  Where a Lever field is the same identity/EEOC concept Greenhouse already
  has a resolution rule for, `answers.py` carries Lever's raw spelling
  (`"location"`, `"name"`, `"eeo[gender]"`, ...) as an additional key
  alongside Greenhouse's, rather than this module translating one into the
  other.
- **hCaptcha on every form**, with a JS-driven `type="button"` submit rather
  than a native `type="submit"`. Unattended submission is not possible on a
  Lever role; `Plan.requires_captcha` carries that forward so `fill.py`'s
  `submit()` can block for a human rather than clicking blind (§12a).

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

FORM_ID = "application-form"

_URL = re.compile(
    r"^https?://jobs\.lever\.co/(?P<slug>[^/?#]+)/(?P<posting_id>[0-9a-f-]+)",
    re.IGNORECASE,
)

_INPUT_TEXT_TYPES = {"text", "email", "tel", "url", "search"}
_WS = re.compile(r"\s+")


class LeverScanError(DomScanError):
    """The rendered Lever form is not shaped the way this scanner can read."""


@dataclass(frozen=True)
class Posting:
    slug: str
    posting_id: str

    @property
    def token(self) -> str:
        """Duck-typed for `plan_for_board`, which reads `.posting.token` with
        no idea which ATS it came from."""
        return self.posting_id

    @property
    def form_url(self) -> str:
        return f"https://jobs.lever.co/{self.slug}/{self.posting_id}/apply"


@dataclass(frozen=True)
class _Scan:
    submit_selector: str | None
    submit_disabled: bool


@dataclass(frozen=True)
class _Schema:
    company_name: str
    title: str


@dataclass(frozen=True)
class LeverBoard:
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
        if "lever.co" in (urlparse(text).hostname or ""):
            raise ApplyUrlError(f"Lever URL with no posting id: {text}")
        raise ApplyUrlError(f"not a Lever posting URL: {text}")
    return Posting(slug=match.group("slug"), posting_id=match.group("posting_id"))


def _text(el) -> str:
    return _WS.sub(" ", "".join(el.itertext())).strip()


def _label_text(question_el) -> str:
    labels = question_el.xpath('.//*[contains(@class, "application-label")]')
    if not labels:
        return ""
    label = labels[0]
    # Strip the required-marker span's own text ("✱") — it is not part of the
    # label a person reads, same treatment as Greenhouse's asterisk (§6).
    parts = [label.text or ""]
    for child in label:
        if "required" in (child.get("class") or "").split():
            continue
        parts.append(_text(child))
        parts.append(child.tail or "")
    return _WS.sub(" ", "".join(parts)).strip()


def _has_required_marker(question_el) -> bool:
    return bool(question_el.xpath('.//span[contains(@class, "required")]'))


def _is_eeoc(question_el) -> bool:
    node = question_el.getparent()
    while node is not None:
        classes = (node.get("class") or "").split()
        if any("eeo-section" in c for c in classes):
            return True
        node = node.getparent()
    return False


def _select_options(select_el) -> tuple[MergedOption, ...]:
    options = []
    for opt in select_el.xpath("./option"):
        value = opt.get("value") or ""
        if not value:
            continue  # the "Select..." placeholder
        options.append(MergedOption(label=_text(opt), value=value))
    return tuple(options)


def _scan_question(question_el) -> MergedField | None:
    section = "eeoc" if _is_eeoc(question_el) else "questions"
    label = _label_text(question_el)
    required = _has_required_marker(question_el) or bool(
        question_el.xpath('.//*[contains(@class, "required-field")]')
    )

    radios = question_el.xpath('.//input[@type="radio"]')
    if radios:
        name = radios[0].get("name") or ""
        options = []
        for radio in radios:
            spans = radio.xpath(
                'following-sibling::span[contains(@class, "eeo-option-text")][1]'
            )
            opt_label = _text(spans[0]) if spans else (radio.get("value") or "")
            options.append(MergedOption(label=opt_label, value=radio.get("value") or ""))
        required = required or any(r.get("required") is not None for r in radios)
        return MergedField(
            id=name, name=name, label=label, required=required,
            kind="radio_group", section=section, multi=False, options=tuple(options),
        )

    # Checkboxes, before the generic branch — they group by shared name the
    # same way radios do. Measured over 19 live Lever boards: 10 of them
    # render at least one, as `pronouns`, `consent[store]` (GDPR storage
    # consent) or a custom card field, and every one of those 10 used to die
    # with `unknown input type 'checkbox'`. No committed fixture had one.
    checkboxes = question_el.xpath('.//input[@type="checkbox"]')
    if checkboxes:
        name = checkboxes[0].get("name") or ""
        if not name:
            return None
        required = required or any(c.get("required") is not None for c in checkboxes)
        if len(checkboxes) == 1 and not (checkboxes[0].get("value") or ""):
            # A lone valueless box is a consent tick, not a one-option group.
            return MergedField(
                id=name, name=name, label=label, required=required,
                kind="checkbox", section=section, multi=False, options=(),
            )
        options = []
        for box in checkboxes:
            spans = box.xpath(
                'following-sibling::span[contains(@class, "eeo-option-text")][1]'
            )
            opt_label = _text(spans[0]) if spans else (box.get("value") or "")
            options.append(MergedOption(label=opt_label, value=box.get("value") or ""))
        return MergedField(
            id=name, name=name, label=label, required=required,
            kind="checkbox_group", section=section, multi=True,
            options=tuple(options),
        )

    controls = question_el.xpath(
        './/input[@type!="hidden" and @type!="radio"] | .//select | .//textarea'
    )
    if not controls:
        return None
    control = controls[0]
    name = control.get("name") or ""
    if not name:
        return None
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
        elif raw in _INPUT_TEXT_TYPES:
            kind, options = "text", ()
        else:
            raise LeverScanError(f"unknown input type {raw!r} on name={name!r}")

    return MergedField(
        id=name, name=name, label=label, required=required,
        kind=kind, section=section, multi=False, options=options,
    )


def scan_lever_form(page_html: str) -> Reconciled:
    """Read every fillable control out of a rendered Lever application form."""
    if not page_html or not page_html.strip():
        raise LeverScanError("empty document")
    doc = lxml_html.fromstring(page_html)
    forms = doc.xpath(f'//form[@id="{FORM_ID}"]')
    if not forms:
        raise LeverScanError(f"no <form id={FORM_ID!r}> in the document")
    form = forms[0]

    questions = form.xpath(
        './/*[contains(concat(" ", normalize-space(@class), " "), " application-question ")]'
    )
    seen_names: set[str] = set()
    fields: list[MergedField] = []
    for q in questions:
        field = _scan_question(q)
        if field is None:
            continue
        if field.name in seen_names:
            # A radio group's <input>s are each their own "application-question"
            # sibling in some renders — only the first carries every option.
            continue
        seen_names.add(field.name)
        fields.append(field)

    return Reconciled(fields=tuple(fields), api_only=())


def _submit_scan(form) -> _Scan:
    submits = form.xpath('.//button[@data-qa="btn-submit"]')
    if len(submits) > 1:
        raise LeverScanError(f"expected one submit button, found {len(submits)}")
    if not submits:
        return _Scan(submit_selector=None, submit_disabled=False)
    submit = submits[0]
    return _Scan(
        submit_selector=f'#{FORM_ID} [data-qa="btn-submit"]',
        submit_disabled=submit.get("aria-disabled") == "true",
    )


def has_captcha(page_html: str) -> bool:
    doc = lxml_html.fromstring(page_html)
    return bool(doc.xpath('//*[@id="h-captcha" or contains(@class, "h-captcha")]'))


def fetch_form(posting: Posting, timeout: int = 30) -> str:
    """The rendered apply page. Raises PostingExpired on 404 — same ordinary
    outcome as a stale Greenhouse token (§14)."""
    try:
        return fetch_text(posting.form_url, timeout=timeout)
    except CareersError as exc:
        if exc.status == 404:
            raise PostingExpired(
                f"posting {posting.posting_id} is gone (404): {posting.form_url}"
            ) from exc
        raise


def load_board(url: str, timeout: int = 30) -> LeverBoard:
    """Posting URL -> a Lever board, `plan_for_board`-shaped. One GET."""
    posting = parse_posting(url)
    html = fetch_form(posting, timeout=timeout)
    doc = lxml_html.fromstring(html)
    forms = doc.xpath(f'//form[@id="{FORM_ID}"]')
    if not forms:
        raise LeverScanError(f"no <form id={FORM_ID!r}> in the document")
    scan = _submit_scan(forms[0])
    reconciled = scan_lever_form(html)
    return LeverBoard(
        posting=posting,
        slug=posting.slug,
        scan=scan,
        schema=_Schema(company_name="", title=""),
        reconciled=reconciled,
        html=html,
        requires_captcha=has_captcha(html),
    )
