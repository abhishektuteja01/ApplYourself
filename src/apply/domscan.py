"""Greenhouse application form: read the rendered DOM into a field inventory.

The source of truth for what must be filled. The question API is enrichment on
top of this (clean labels, select option lists) — it omits `country`, the EEOC
block, the education block and the demographic block entirely.

Pure function over an HTML string: no browser, no network.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from lxml import html as lxml_html

FORM_ID = "application-form"

# Widget kinds the scanner emits. A rendered control that maps to none of these
# raises rather than being dropped — a missed required field is a failed submit.
KINDS = frozenset({
    "text",
    "number",
    "tel",
    "textarea",
    "file",
    "react_select",
    "checkbox",
    "checkbox_group",
})

# input[type] -> kind. Types absent here raise.
_INPUT_TYPES = {
    "text": "text",
    "email": "text",
    "url": "text",
    "search": "text",
    "tel": "tel",
    "number": "number",
    "file": "file",
    "checkbox": "checkbox",
}

# Sections, resolved from the nearest ancestor container. Everything the
# employer authored lands in "questions"; the other three are Greenhouse's own
# blocks and are answered structurally, never from a keyword rule.
_SECTIONS = (
    ("eeoc", ("eeoc__container", "eeoc__question__wrapper")),
    ("demographic", ("demographic--container",)),
    ("education", ("education--container", "education--form")),
    # Note the single dash on the form class — Greenhouse spells the education
    # pair "education--form" and the employment one "employment-form".
    ("employment", ("employment--container", "employment-form")),
)
_SECTION_IDS = {"demographic-section": "demographic"}

# Two independent signals have to agree on which Greenhouse-owned block a field
# belongs to: the container class above, and the shape of the id. If Greenhouse
# renames a container, the class lookup silently drops the whole block into
# `questions`, where an answer rule would keyword-match fields the employer never
# authored — which is exactly how the employment block was missed. Disagreement
# is an error, not a fallback.
_BLOCK_ID_SHAPES = (
    ("education", re.compile(
        r"^(?:school|degree|discipline|start-year|end-year|start-month|end-month)--\d+$")),
    ("employment", re.compile(
        r"^(?:company-name|title|start-date-month|start-date-year|end-date-month"
        r"|end-date-year|current-role)-\d+(?:_\d+)?$")),
    ("eeoc", re.compile(r"^(?:gender|hispanic_ethnicity|veteran_status|disability_status)$")),
    ("demographic", re.compile(r"^\d+$")),
)

_WS = re.compile(r"\s+")


class DomScanError(Exception):
    """The rendered form is not shaped the way the scanner can read."""


@dataclass(frozen=True)
class DomOption:
    value: str
    label: str


@dataclass(frozen=True)
class DomField:
    id: str            # verbatim, including any "[]" — this is the selector
    name: str          # "[]" stripped, so it matches the API's field name
    label: str
    required: bool
    kind: str
    section: str
    multi: bool = False
    options: tuple[DomOption, ...] = ()   # checkbox_group only


@dataclass(frozen=True)
class FormScan:
    fields: tuple[DomField, ...]
    submit_selector: str | None
    submit_disabled: bool

    def by_id(self, field_id: str) -> DomField | None:
        for f in self.fields:
            if f.id == field_id:
                return f
        return None

    @property
    def required_ids(self) -> tuple[str, ...]:
        return tuple(f.id for f in self.fields if f.required)


def _text(el) -> str:
    """Visible text, minus aria-hidden descendants.

    Greenhouse marks the required asterisk `<span aria-hidden="true">*</span>`,
    so a naive text_content() returns "First Name*".
    """
    parts: list[str] = []
    if el.text:
        parts.append(el.text)
    for child in el:
        if child.get("aria-hidden") == "true" or child.get("class") == "required":
            pass
        else:
            parts.append(_text(child))
        if child.tail:
            parts.append(child.tail)
    return _WS.sub(" ", "".join(parts)).strip().rstrip("*").strip()


def _classes(el) -> set[str]:
    return set((el.get("class") or "").split())


def _ancestors(el):
    node = el.getparent()
    while node is not None:
        yield node
        node = node.getparent()


def _is_hidden(el) -> bool:
    """aria-hidden marks Greenhouse's decoy inputs (the `requiredInput` spans
    that carry `required` but are not fields). Visually hidden is different and
    must NOT be skipped — the file inputs are `class="visually-hidden"`.
    """
    if el.get("aria-hidden") == "true":
        return True
    return any(a.get("aria-hidden") == "true" for a in _ancestors(el))


def _section(el) -> str:
    for anc in _ancestors(el):
        mapped = _SECTION_IDS.get(anc.get("id") or "")
        if mapped:
            return mapped
        classes = _classes(anc)
        for name, markers in _SECTIONS:
            if classes.intersection(markers):
                return name
    return "questions"


def _label_for(form, el, field_id: str) -> str:
    if field_id:
        found = form.xpath(f'.//label[@for="{field_id}"]')
        # The file inputs carry a throwaway label ("Attach"); their real one is
        # on the wrapping role=group. Handled by the caller, which passes a
        # label in explicitly.
        if found:
            return _text(found[0])
    labelled_by = el.get("aria-labelledby")
    if labelled_by:
        texts = []
        for ref in labelled_by.split():
            node = form.xpath(f'.//*[@id="{ref}"]')
            if node:
                texts.append(_text(node[0]))
        if any(texts):
            return _WS.sub(" ", " ".join(texts)).strip()
    return (el.get("aria-label") or "").strip()


def _required(el) -> bool:
    if el.get("aria-required") == "true":
        return True
    return el.get("required") is not None


def _is_multi_select(el) -> bool:
    """react-select renders an identical input for single and multi; the only
    tell is the value-container class on an ancestor."""
    for anc in _ancestors(el):
        if "select__value-container--is-multi" in _classes(anc):
            return True
        if "select" in _classes(anc) or "select__container" in _classes(anc):
            break
    return False


def _file_group(el):
    """The role=group wrapper that carries a file input's real label and
    required flag."""
    for anc in _ancestors(el):
        if anc.get("role") == "group" and "file-upload" in _classes(anc):
            return anc
    return None


def _scan_checkbox_group(form, fieldset) -> DomField:
    group_id = fieldset.get("id") or ""
    legends = fieldset.xpath("./legend")
    label = _text(legends[0]) if legends else ""
    options = []
    name = ""
    for box in fieldset.xpath('.//input[@type="checkbox"]'):
        box_id = box.get("id") or ""
        name = name or (box.get("name") or "")
        opt_label = ""
        if box_id:
            found = form.xpath(f'.//label[@for="{box_id}"]')
            if found:
                opt_label = _text(found[0])
        options.append(DomOption(value=box.get("value") or "", label=opt_label))
    if not options:
        raise DomScanError(f"checkbox fieldset {group_id!r} has no options")
    return DomField(
        id=group_id,
        name=(name or group_id).removesuffix("[]"),
        label=label,
        required=_required(fieldset),
        kind="checkbox_group",
        section=_section(fieldset),
        multi=True,
        options=tuple(options),
    )


def scan_form(page_html: str) -> FormScan:
    """Read every fillable control out of a rendered application form."""
    if not page_html or not page_html.strip():
        raise DomScanError("empty document")
    doc = lxml_html.fromstring(page_html)
    forms = doc.xpath(f'//form[@id="{FORM_ID}"]')
    if not forms:
        raise DomScanError(f"no <form id={FORM_ID!r}> in the document")
    form = forms[0]

    fields: list[DomField] = []
    grouped: set = set()
    for fieldset in form.xpath('.//fieldset[@id][contains(@class, "checkbox")]'):
        grouped.update(fieldset.xpath('.//input[@type="checkbox"]'))

    # One union xpath so fields come back in document order.
    controls = form.xpath(
        './/fieldset[@id][contains(@class, "checkbox")] | .//input | .//textarea | .//select'
    )
    for el in controls:
        if el in grouped or _is_hidden(el):
            continue

        field_id = el.get("id") or ""
        tag = el.tag

        if tag == "fieldset":
            fields.append(_scan_checkbox_group(form, el))
            continue

        if el.get("role") == "combobox":
            kind = "react_select"
        elif tag == "textarea":
            kind = "textarea"
        elif tag == "select":
            kind = "react_select"
        else:
            raw = (el.get("type") or "text").lower()
            if raw == "hidden":
                continue
            if raw not in _INPUT_TYPES:
                raise DomScanError(f"unknown input type {raw!r} on id={field_id!r}")
            kind = _INPUT_TYPES[raw]

        if not field_id:
            # Nothing can be filled without a selector. Loud only when it
            # matters — an unlabelled optional decoy is not worth failing on.
            if _required(el):
                raise DomScanError(f"required {kind} field has no id: {lxml_html.tostring(el)[:200]!r}")
            continue

        if kind == "file":
            group = _file_group(el)
            if group is None:
                raise DomScanError(f"file input {field_id!r} has no role=group wrapper")
            labelled_by = group.get("aria-labelledby") or ""
            node = form.xpath(f'.//*[@id="{labelled_by}"]') if labelled_by else []
            label = _text(node[0]) if node else ""
            required = _required(group)
        else:
            label = _label_for(form, el, field_id)
            required = _required(el)

        fields.append(DomField(
            id=field_id,
            name=(el.get("name") or field_id).removesuffix("[]"),
            label=label,
            required=required,
            kind=kind,
            section=_section(el),
            multi=_is_multi_select(el) if kind == "react_select" else False,
        ))

    for field in fields:
        for block, pattern in _BLOCK_ID_SHAPES:
            if pattern.match(field.id) and field.section != block:
                raise DomScanError(
                    f"field {field.id!r} has the id shape of the {block} block but "
                    f"scanned as section {field.section!r} — its container class "
                    f"has probably been renamed"
                )

    submits = form.xpath('.//button[@type="submit"]')
    if len(submits) > 1:
        raise DomScanError(f"expected one submit button, found {len(submits)}")
    submit = submits[0] if submits else None

    return FormScan(
        fields=tuple(fields),
        submit_selector=f'#{FORM_ID} button[type="submit"]' if submit is not None else None,
        submit_disabled=bool(submit is not None and submit.get("aria-disabled") == "true"),
    )
