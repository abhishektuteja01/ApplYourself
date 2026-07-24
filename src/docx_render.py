"""ATS-clean docx generation, plus cover-letter rendering.

Determinism boundary (R7): no LLM calls.

Resume path (render_resume): templated paragraph styles only -- the
template (profile/resume_template.docx) defines visual formatting; this
module just creates paragraphs in the right styles from the markdown.
Template contract — the template MUST define these 5 named paragraph styles:
  - Resume Name        (the name at top)
  - Resume Section     (SUMMARY / WORK EXPERIENCE / EDUCATION / ...)
  - Resume Job Header  (role / degree / project headers; bold via inline runs)
  - Resume Body        (contact line, summary text, skills lines)
  - Resume Bullet      (bulleted items)
Forbidden in the resume template (renderer rejects loudly):
  - Tables (ATS-unfriendly per §6.4)
  - Inline shapes (images, icons)

Cover-letter path (render_cover_letter): placeholder fill-in instead --
the user's own design (profile/cover_letter_template.docx) is preserved
byte-for-byte except for paragraphs containing an exact {{TOKEN}}, which
get their text replaced. See COVER_LETTER_REQUIRED_PLACEHOLDERS below.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import TypedDict

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph


REQUIRED_STYLES = [
    "Resume Name",
    "Resume Section",
    "Resume Job Header",
    "Resume Body",
    "Resume Bullet",
]

# Required character-type style for clickable [text](url) markdown.
REQUIRED_CHARACTER_STYLE = "Hyperlink"

# Cover-letter template contract (placeholder fill-in, not markdown render --
# the user's own .docx design is preserved untouched; only these placeholder
# paragraphs get their text replaced). {{BODY}} is special: its paragraph's
# formatting is cloned once per body paragraph, then the placeholder itself
# is removed.
COVER_LETTER_REQUIRED_PLACEHOLDERS = ["{{SALUTATION}}", "{{BODY}}"]
COVER_LETTER_OPTIONAL_PLACEHOLDERS = ["{{DATE}}", "{{CLOSING}}", "{{SIGNOFF_NAME}}"]


class TemplateMissingError(FileNotFoundError):
    """Template file does not exist on disk."""


class TemplateError(ValueError):
    """Template exists but violates the docx_render contract."""


class Run(TypedDict, total=False):
    text: str
    bold: bool
    url: str   # optional; when set, emit as a Word hyperlink (clickable,
               # Hyperlink character style applied for blue+underline)


class Block(TypedDict, total=False):
    type: str   # "name" | "section_header" | "job_header" | "body" | "bullet"
    text: str
    runs: list[Run]


# =====================================================================
# Public API
# =====================================================================

def render_resume(md_content: str, template_path: Path, out_path: Path) -> None:
    """Render resume markdown to an ATS-clean docx using the template.
    Fail loud on missing template / missing required styles / forbidden
    template elements (tables, inline shapes)."""
    if not template_path.exists():
        raise TemplateMissingError(
            f"ERROR: {template_path} missing. Author it in Word with the ATS "
            f"constraints (Calibri, single column, no tables, bold section "
            f"headers, no images/icons)."
        )
    doc = Document(str(template_path))
    _validate_template(doc, template_path)
    _clear_body(doc)
    blocks = parse_resume_md(md_content)
    _write_blocks(doc, blocks)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))


def render_cover_letter(content: dict, template_path: Path, out_path: Path) -> None:
    """Fill placeholder paragraphs in the user's own cover-letter .docx
    design, leaving every other paragraph/style/header/footer/image in
    that template byte-for-byte untouched.

    content keys:
      salutation   (str, required)  -- e.g. "Dear Hiring Manager,"
      body         (list[str], required) -- one entry per body paragraph
      date         (str, optional)
      closing      (str, optional)  -- e.g. "Sincerely,"
      signoff_name (str, optional)  -- e.g. "Jane Doe"

    Template contract: somewhere in the template body, a paragraph whose
    stripped text is EXACTLY one of COVER_LETTER_REQUIRED_PLACEHOLDERS /
    COVER_LETTER_OPTIONAL_PLACEHOLDERS marks where that content goes.
    {{SALUTATION}}/{{DATE}}/{{CLOSING}}/{{SIGNOFF_NAME}} are replaced
    in place (the paragraph's first run keeps its formatting). {{BODY}}'s
    paragraph is cloned once per entry in content['body'] (preserving its
    style/run formatting), then the placeholder paragraph is removed.
    Optional placeholders absent from the template are simply skipped --
    the template already has static text there."""
    if not template_path.exists():
        raise TemplateMissingError(
            f"ERROR: {template_path} missing. Save your cover letter design "
            f"there with placeholder paragraphs: {COVER_LETTER_REQUIRED_PLACEHOLDERS} "
            f"required, {COVER_LETTER_OPTIONAL_PLACEHOLDERS} optional."
        )
    doc = Document(str(template_path))
    _fill_cover_letter_placeholders(doc, content, template_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))


def parse_resume_md(md: str) -> list[Block]:
    """Parse the small resume.md markdown subset into typed blocks.

    Supported subset:
      **Name** or # **Name**            -> name (first such line only)
      **SECTION NAME**                  -> section_header (all-caps content)
      **Bold Prefix** rest of line      -> job_header
      * bullet  /  - bullet             -> bullet
      anything else (non-blank)         -> body

    Inline **bold** runs are split for body / bullet / job_header blocks."""
    blocks: list[Block] = []
    seen_name = False
    for raw in md.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if not seen_name:
            m = re.fullmatch(r"#?\s*\*\*(.+?)\*\*", line)
            if m:
                blocks.append({"type": "name", "text": m.group(1).strip()})
                seen_name = True
                continue
        # Bullet
        if line.startswith(("* ", "- ")):
            body = line[2:].strip()
            blocks.append({
                "type": "bullet", "text": body,
                "runs": _split_inline_bold(body),
            })
            continue
        # Section header: a line that is entirely **ALL CAPS WORDS**
        m = re.fullmatch(r"\*\*([A-Z][A-Z0-9 &/]*[A-Z])\*\*", line)
        if m:
            blocks.append({"type": "section_header", "text": m.group(1).strip()})
            continue
        # Lines starting with **...** followed by more content:
        # - Skills lines whose bold prefix ends with ":" route to body
        #   (e.g. "**Programming:** Python, ..." -> Resume Body w/ inline bold).
        # - Everything else routes to job_header (role / degree / project headers).
        m = re.match(r"\*\*(.+?)\*\*(.*)", line)
        if m and m.group(2).strip():
            bold_prefix = m.group(1).strip()
            if bold_prefix.endswith(":"):
                blocks.append({
                    "type": "body", "text": line,
                    "runs": _split_inline_bold(line),
                })
                continue
            runs: list[Run] = [
                {"text": bold_prefix, "bold": True},
            ]
            tail = m.group(2)
            if tail:
                runs.extend(_split_inline_bold(tail))
            blocks.append({"type": "job_header", "text": line, "runs": runs})
            continue
        # Plain body (may contain inline bold)
        blocks.append({
            "type": "body", "text": line,
            "runs": _split_inline_bold(line),
        })
    return blocks


# =====================================================================
# Internals
# =====================================================================

def _validate_template(doc, path: Path) -> None:
    style_names = {s.name for s in doc.styles}
    missing = [s for s in REQUIRED_STYLES if s not in style_names]
    if missing:
        raise TemplateError(
            f"Template {path} missing required paragraph styles: {missing}. "
            f"Each maps to a markdown block type — define them in Word's Styles "
            f"panel. Required set: {REQUIRED_STYLES}"
        )
    if REQUIRED_CHARACTER_STYLE not in style_names:
        raise TemplateError(
            f"Template {path} missing required character style "
            f"{REQUIRED_CHARACTER_STYLE!r}. Word ships this style by default "
            f"-- if it was removed, apply Word's built-in Hyperlink style to "
            f"any character once to recreate it, then re-save the template."
        )
    if len(doc.tables) > 0:
        raise TemplateError(
            f"Template {path} contains {len(doc.tables)} table(s), which are "
            f"ATS-unfriendly. Remove all tables from the template."
        )
    if len(doc.inline_shapes) > 0:
        raise TemplateError(
            f"Template {path} contains {len(doc.inline_shapes)} inline shape(s) "
            f"(images/icons), which are ATS-unfriendly. Remove from the template."
        )


def _paragraph_full_text(paragraph) -> str:
    """All visible text in a paragraph, including runs nested inside
    w:hyperlink and w:sdt (Word 'content control' -- the wrapper Word's
    built-in templates use for click-to-type placeholders). Plain
    paragraph.text only walks direct w:r children and misses both, which
    silently breaks placeholder detection against real Word templates."""
    return "".join(t.text or "" for t in paragraph._p.iter(qn("w:t")))


def list_cover_letter_placeholders(template_path: Path) -> set[str]:
    """Return which COVER_LETTER_REQUIRED_PLACEHOLDERS /
    COVER_LETTER_OPTIONAL_PLACEHOLDERS tokens are present in
    template_path (using _paragraph_full_text, so w:hyperlink/w:sdt
    nesting is accounted for). /cover-letter uses this to decide which
    optional fields (date/closing/signoff_name) are worth generating --
    callers must NOT re-implement detection with plain paragraph.text,
    which silently misses tokens nested in Word content controls."""
    doc = Document(str(template_path))
    all_tokens = set(COVER_LETTER_REQUIRED_PLACEHOLDERS) | set(COVER_LETTER_OPTIONAL_PLACEHOLDERS)
    return {
        text for p in doc.paragraphs
        if (text := _paragraph_full_text(p).strip()) in all_tokens
    }


def _fill_cover_letter_placeholders(doc, content: dict, path: Path) -> None:
    found: dict[str, object] = {}
    counts: dict[str, int] = {}
    all_tokens = set(COVER_LETTER_REQUIRED_PLACEHOLDERS) | set(COVER_LETTER_OPTIONAL_PLACEHOLDERS)
    for p in doc.paragraphs:
        text = _paragraph_full_text(p).strip()
        if text in all_tokens:
            counts[text] = counts.get(text, 0) + 1
            found[text] = p

    duplicates = [t for t, n in counts.items() if n > 1]
    if duplicates:
        raise TemplateError(
            f"Template {path} has more than one paragraph containing "
            f"{duplicates}. Each placeholder token must appear exactly once."
        )

    missing = [ph for ph in COVER_LETTER_REQUIRED_PLACEHOLDERS if ph not in found]
    if missing:
        raise TemplateError(
            f"Template {path} missing required placeholder paragraph(s) "
            f"{missing}. Add a paragraph containing EXACTLY that token "
            f"(on its own line, with whatever formatting you want) "
            f"wherever the generated content should go."
        )

    if not content.get("salutation"):
        raise ValueError("content['salutation'] is required and must be non-empty.")
    if not content.get("body"):
        raise ValueError("content['body'] is required and must be a non-empty list of paragraph strings.")

    simple_values = {
        "{{DATE}}": content.get("date"),
        "{{SALUTATION}}": content.get("salutation"),
        "{{CLOSING}}": content.get("closing"),
        "{{SIGNOFF_NAME}}": content.get("signoff_name"),
    }
    for token, value in simple_values.items():
        if token in found:
            # Found-in-template placeholders ALWAYS get filled, even with ""
            # for an unsupplied optional field -- leaving the raw {{TOKEN}}
            # text in a generated letter is a worse failure than a blank line.
            _set_paragraph_text(found[token], value or "")

    _expand_body_placeholder(found["{{BODY}}"], content["body"])


def _set_paragraph_text(paragraph, text: str) -> None:
    """Replace a placeholder paragraph's visible text in place, finding
    the underlying w:t regardless of w:hyperlink/w:sdt nesting (see
    _paragraph_full_text). The first w:t's run keeps its character
    formatting; if that run sits inside a Word content control (w:sdt),
    the control is unwrapped (replaced by the bare run) so no stale
    'click here to type' plumbing survives into the generated letter.
    Any other w:t left in the paragraph (e.g. a split token) is cleared."""
    p_elem = paragraph._p
    t_elems = list(p_elem.iter(qn("w:t")))
    if not t_elems:
        paragraph.add_run(text)
        return
    first_t = t_elems[0]
    run_elem = first_t.getparent()
    _unwrap_sdt_ancestor(run_elem, p_elem)
    first_t.text = text
    for t in t_elems[1:]:
        t.text = ""


def _unwrap_sdt_ancestor(run_elem, paragraph_elem) -> bool:
    """If run_elem sits inside a Word content control (w:sdt) within
    paragraph_elem, replace that w:sdt with run_elem itself (preserving
    run_elem's formatting/position) and return True. No-op (returns
    False) if run_elem is a direct/hyperlink-nested child with no w:sdt
    ancestor."""
    node = run_elem
    parent = node.getparent()
    while parent is not None and parent is not paragraph_elem:
        if parent.tag == qn("w:sdt"):
            parent.getparent().replace(parent, run_elem)
            return True
        node = parent
        parent = node.getparent()
    return False


def _expand_body_placeholder(placeholder_p, paragraphs: list[str]) -> None:
    """Clone the {{BODY}} paragraph's XML once per entry in `paragraphs`
    (preserving its style/run formatting), insert each clone immediately
    before the placeholder, set its text, then remove the now-empty
    placeholder paragraph itself."""
    anchor = placeholder_p._p
    for text in paragraphs:
        new_elem = copy.deepcopy(anchor)
        anchor.addprevious(new_elem)
        new_paragraph = Paragraph(new_elem, placeholder_p._parent)
        _set_paragraph_text(new_paragraph, text)
    anchor.getparent().remove(anchor)


def _clear_body(doc) -> None:
    """Remove all paragraphs from the document body. Styles are preserved."""
    for p in list(doc.paragraphs):
        elem = p._element
        elem.getparent().remove(elem)


def _write_blocks(doc, blocks: list[Block]) -> None:
    """Write blocks to the doc body.

    Two layout rules applied here (kept here vs in parse_resume_md so the
    markdown shape stays inspection-friendly):

    1. The first body block immediately after the name block is the contact
       line. Force-center it via WD_ALIGN_PARAGRAPH.CENTER on just that
       paragraph; the style stays Resume Body so the font / size inherit.
       Per the template contract (5 styles only), we do not add a sixth
       Resume Contact style.
    2. Any run text containing a tab character is split at the tab;
       add_run("\\t") is emitted between the pieces so Word's layout engine
       advances to the paragraph style's next tab stop. Resume Job Header
       carries a right tab stop at 19.05 cm -- this is what right-aligns
       the date on "**Title** ... <TAB> Date" job-header lines.
    """
    name_just_emitted = False
    for block in blocks:
        btype = block["type"]
        if btype == "name":
            p = doc.add_paragraph(style="Resume Name")
            p.add_run(block["text"])
            name_just_emitted = True
            continue
        if btype == "section_header":
            p = doc.add_paragraph(style="Resume Section")
            p.add_run(block["text"])
            name_just_emitted = False
            continue
        style_map = {
            "job_header": "Resume Job Header",
            "body":       "Resume Body",
            "bullet":     "Resume Bullet",
        }
        if btype not in style_map:
            raise ValueError(f"Unknown block type: {btype}")
        p = doc.add_paragraph(style=style_map[btype])
        is_contact = btype == "body" and name_just_emitted
        if is_contact:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        runs = block.get("runs") or [{"text": block["text"], "bold": False}]
        for r in runs:
            _emit_run_splitting_tabs(
                p, r["text"], bold=bool(r.get("bold")), url=r.get("url"),
            )
        name_just_emitted = False


def _emit_run_splitting_tabs(paragraph, text: str, *, bold: bool,
                              url: str | None = None) -> None:
    """Add runs to paragraph, splitting on tab characters and routing
    URL-bearing runs through the Word hyperlink path. Each "\\t" becomes
    its own non-bold non-link run so Word's layout engine advances to the
    next tab stop. Tabs inside a URL-bearing run are silently dropped --
    URLs don't sensibly contain tab stops."""
    if not text:
        return
    if url:
        # Hyperlinks are atomic in Word: a single <w:hyperlink> element with
        # a single visible run. Tabs inside link text don't make sense in
        # any resume / outreach context we care about.
        _add_hyperlink(paragraph, text, url)
        return
    if "\t" not in text:
        run = paragraph.add_run(text)
        if bold:
            run.bold = True
        return
    parts = text.split("\t")
    for i, part in enumerate(parts):
        if i > 0:
            paragraph.add_run("\t")
        if part:
            run = paragraph.add_run(part)
            if bold:
                run.bold = True


def _add_hyperlink(paragraph, text: str, url: str) -> None:
    """Insert a real Word hyperlink into the paragraph. Uses the template's
    built-in 'Hyperlink' character style for blue+underline rendering."""
    part = paragraph.part
    r_id = part.relate_to(url, RT.HYPERLINK, is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)
    new_run = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    rStyle = OxmlElement("w:rStyle")
    rStyle.set(qn("w:val"), REQUIRED_CHARACTER_STYLE)
    rPr.append(rStyle)
    new_run.append(rPr)
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    new_run.append(t)
    hyperlink.append(new_run)
    paragraph._p.append(hyperlink)


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _split_inline_bold(text: str) -> list[Run]:
    """Split text into runs at **bold** AND [text](url) boundaries.

    Name is historical -- handles bold and links now. Plain text becomes
    plain runs; **x** becomes a bold run; [t](u) becomes a link run (bold
    inside a link or a link inside bold are NOT supported in v1 -- if a
    bold span and a link span overlap, whichever started earlier wins and
    the later overlapping match is dropped)."""
    matches: list[tuple[int, int, Run]] = []
    for m in _BOLD_RE.finditer(text):
        matches.append((m.start(), m.end(), {"text": m.group(1), "bold": True}))
    for m in _LINK_RE.finditer(text):
        matches.append((m.start(), m.end(),
                        {"text": m.group(1), "bold": False, "url": m.group(2)}))
    matches.sort(key=lambda x: x[0])

    runs: list[Run] = []
    pos = 0
    for start, end, run in matches:
        if start < pos:
            continue   # overlap with a prior match; drop
        if start > pos:
            runs.append({"text": text[pos:start], "bold": False})
        runs.append(run)
        pos = end
    if pos < len(text):
        runs.append({"text": text[pos:], "bold": False})
    if not runs:
        runs.append({"text": text, "bold": False})
    return runs
