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
  `submit()` can block for a human rather than clicking blind.

Pure functions over an HTML string for the scan and the page's company and
title; network lives in `load_board`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from lxml import html as lxml_html
from lxml.etree import ParserError

from src.apply.domscan import DomScanError
from src.apply.greenhouse import ApplyUrlError, PostingExpired
from src.apply.reconcile import MergedField, MergedOption, Reconciled
from src.shared import job_boards
from src.shared.http_client import CareersError, fetch_text

FORM_ID = "application-form"

# Shared board table: host variants and posting shape live in one place.
_URL = job_boards.get_source("lever").posting_url_re

_INPUT_TEXT_TYPES = {"text", "email", "tel", "url", "search"}
_WS = re.compile(r"\s+")

# og:title / <title> read "<company> - <posting title>".
_TITLE_SEPARATOR = " - "


def _class_token(name: str) -> str:
    """XPath predicate: `@class` holds `name` as a whole token, so
    "required" does not match "not-required-hint"."""
    return f'contains(concat(" ", normalize-space(@class), " "), " {name} ")'


class LeverScanError(DomScanError):
    """The rendered Lever form is not shaped the way this scanner can read."""


@dataclass(frozen=True)
class Posting:
    slug: str
    posting_id: str
    region: str = ""   # "eu" for jobs.eu.lever.co; the form lives on that host

    @property
    def token(self) -> str:
        """Duck-typed for `plan_for_board`, which reads `.posting.token` with
        no idea which ATS it came from."""
        return self.posting_id

    @property
    def form_url(self) -> str:
        host = "jobs.eu.lever.co" if self.region == "eu" else "jobs.lever.co"
        return f"https://{host}/{self.slug}/{self.posting_id}/apply"


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
        if "lever.co" in (urlparse(text).hostname or ""):
            raise ApplyUrlError(f"Lever URL with no posting id: {text}")
        raise ApplyUrlError(f"not a Lever posting URL: {text}")
    return Posting(slug=match.group("slug"), posting_id=match.group("posting_id"),
                   region=(match.group("region") or "").lower())


def _text(el) -> str:
    return _WS.sub(" ", "".join(el.itertext())).strip()


def _document(page_html: str):
    """Parse a page, or raise `LeverScanError` (a `DomScanError`, so one bad
    page fails its role rather than the whole run)."""
    if not page_html or not page_html.strip():
        raise LeverScanError("empty document")
    try:
        return lxml_html.fromstring(page_html)
    except (ParserError, ValueError) as exc:
        raise LeverScanError(f"could not parse the document: "
                             f"{type(exc).__name__}: {exc}") from exc


def page_identity(page_html: str) -> tuple[str, str]:
    """(company, title) as the apply page states them, "" where it does not.

    The title is the posting header's `<h2>`. The company is og:title (or
    `<title>`) minus that title, else the header logo's alt text.
    """
    doc = _document(page_html)
    headers = doc.xpath('//*[contains(@class, "posting-header")]//h2')
    title = _text(headers[0]) if headers else ""

    og = doc.xpath('//meta[@property="og:title"]/@content')
    titles = doc.xpath("//title")
    heading = _WS.sub(" ", (og[0] if og else "") or
                      (_text(titles[0]) if titles else "")).strip()

    company = ""
    if heading and title and heading.endswith(_TITLE_SEPARATOR + title):
        company = heading[: -len(_TITLE_SEPARATOR + title)].strip()
    elif _TITLE_SEPARATOR in heading:
        company, rest = (part.strip() for part in heading.split(_TITLE_SEPARATOR, 1))
        title = title or rest
    if not company:
        alts = doc.xpath('//*[contains(@class, "main-header-logo")]//img/@alt')
        if alts:
            company = re.sub(r"\s+logo$", "", alts[0].strip(), flags=re.IGNORECASE)
    return company, title


def _label_text(question_el) -> str:
    labels = question_el.xpath('.//*[contains(@class, "application-label")]')
    if labels:
        label = labels[0]
        # Strip the required-marker span's own text ("✱") — it is not part of
        # the label a person reads, same treatment as Greenhouse's asterisk.
        parts = [label.text or ""]
        for child in label:
            if "required" in (child.get("class") or "").split():
                continue
            parts.append(_text(child))
            parts.append(child.tail or "")
        return _WS.sub(" ", "".join(parts)).strip()
    # Lever's consent checkboxes (`consent[store]`, `consent[marketing]`) carry
    # no `.application-label` at all — the statement a person reads is a
    # `<span>` sibling of the checkbox inside the same wrapping `<label>`.
    spans = question_el.xpath('.//input[@type="checkbox"]/parent::label/span')
    if spans:
        return _text(spans[0])
    return ""


def _has_required_marker(question_el) -> bool:
    """Lever's asterisk `<span class="required">`, or a `required-field`
    wrapper on a card. Whole class tokens only."""
    return bool(question_el.xpath(
        f'.//span[{_class_token("required")}] | .//*[{_class_token("required-field")}]'
    ))


def _is_eeoc(question_el) -> bool:
    node = question_el.getparent()
    while node is not None:
        classes = (node.get("class") or "").split()
        if any("eeo-section" in c for c in classes):
            return True
        node = node.getparent()
    return False


#: The hidden input a structured autocomplete resolves into. Its presence in
#: the same question is what separates Lever's location search box from a
#: plain text field.
STRUCTURED_PARTNER = "selectedLocation"


def _has_structured_partner(question_el) -> bool:
    return bool(
        question_el.xpath(
            f'.//input[@type="hidden" and @name="{STRUCTURED_PARTNER}"]'
        )
    )


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
    required = _has_required_marker(question_el)

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
    # same way radios do. Common on Lever, as `pronouns`, `consent[store]`
    # (GDPR storage consent) or a custom card field.
    checkboxes = question_el.xpath('.//input[@type="checkbox"]')
    if checkboxes:
        name = checkboxes[0].get("name") or ""
        if not name:
            return None
        required = required or any(c.get("required") is not None for c in checkboxes)
        if len(checkboxes) == 1:
            # A lone box is a consent tick, not a one-option group — real
            # boards pair it with a hidden `value="0"` fallback and give the
            # checkbox itself `value="1"`, the standard HTML checked/unchecked
            # idiom, so a truthy `value` does not make it a group of one.
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
            # The location question looks like a text input and is not one: it
            # is a search box over a place taxonomy, and the board posts the
            # structured `selectedLocation` its suggestion click resolves,
            # not the typed text. Typing alone leaves that hidden partner
            # empty and the submission is refused. `react_select` is the kind
            # fill.py drives as type-and-pick; the Lever driver supplies the
            # selectors, since Lever renders no react-select markup.
            #
            # Detected by the hidden partner rather than by the field's name,
            # so a board that renames the visible input still resolves.
            if _has_structured_partner(question_el):
                kind, options = "react_select", ()
            else:
                kind, options = "text", ()
        else:
            raise LeverScanError(f"unknown input type {raw!r} on name={name!r}")

    return MergedField(
        id=name, name=name, label=label, required=required,
        kind=kind, section=section, multi=False, options=options,
    )


def scan_lever_form(page_html: str) -> Reconciled:
    """Read every fillable control out of a rendered Lever application form."""
    form = _form(_document(page_html))

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


def _form(doc):
    forms = doc.xpath(f'//form[@id="{FORM_ID}"]')
    if not forms:
        raise LeverScanError(f"no <form id={FORM_ID!r}> in the document")
    return forms[0]


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
    doc = _document(page_html)
    return bool(doc.xpath('//*[@id="h-captcha" or contains(@class, "h-captcha")]'))


def fetch_form(posting: Posting, timeout: int = 30) -> str:
    """The rendered apply page. Raises PostingExpired on 404 — same ordinary
    outcome as a stale Greenhouse token."""
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
    scan = _submit_scan(_form(_document(html)))
    reconciled = scan_lever_form(html)
    company, title = page_identity(html)
    return LeverBoard(
        posting=posting,
        slug=posting.slug,
        scan=scan,
        schema=_Schema(company_name=company, title=title),
        reconciled=reconciled,
        html=html,
        requires_captcha=has_captcha(html),
    )
