"""ATS-clean resume docx generation.

Determinism boundary (R7): no LLM calls.

render_resume uses templated paragraph styles only: the template
(profile/resume_template.docx) defines the visual formatting, and this module
creates paragraphs in the right styles from the markdown. The template MUST
define these 5 named paragraph styles:
  - Resume Name        (the name at top)
  - Resume Section     (SUMMARY / WORK EXPERIENCE / EDUCATION / ...)
  - Resume Job Header  (role / degree / project headers; bold via inline runs)
  - Resume Body        (contact line, summary text, skills lines)
  - Resume Bullet      (bulleted items)
Forbidden in the template (rejected loudly): tables (ATS-unfriendly) and inline
shapes (images, icons).

Cover letters are the opposite operation — preserve the user's design and fill
placeholders — and live in src/docx_cover_letter.py.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from src import paths
from src.parquet_io import locked

REQUIRED_STYLES = [
    "Resume Name",
    "Resume Section",
    "Resume Job Header",
    "Resume Body",
    "Resume Bullet",
]

# Required character-type style for clickable [text](url) markdown.
REQUIRED_CHARACTER_STYLE = "Hyperlink"


class TemplateMissingError(FileNotFoundError):
    """Template file does not exist on disk."""


class TemplateError(ValueError):
    """Template exists but violates the docx_render contract."""


class ResumeContentError(ValueError):
    """Resume markdown violates a render contract the template depends on."""


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


def parse_resume_md(md: str) -> list[Block]:
    """Parse the markdown resume_model.build emits into typed blocks.

      **Name**                          -> name (first such line only)
      **SECTION NAME**                  -> section_header (all-caps content)
      **Bold Prefix** rest of line      -> job_header
      * bullet                          -> bullet
      anything else (non-blank)         -> body

    Inline **bold** runs are split for body / bullet / job_header blocks."""
    blocks: list[Block] = []
    seen_name = False
    for raw in md.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if not seen_name:
            m = re.fullmatch(r"\*\*(.+?)\*\*", line)
            if m:
                blocks.append({"type": "name", "text": m.group(1).strip()})
                seen_name = True
                continue
        # Bullet
        if line.startswith("* "):
            body = line[2:].strip()
            blocks.append({
                "type": "bullet", "text": body,
                "runs": _inline_runs(body),
            })
            continue
        # Section header: a line that is entirely **ALL CAPS WORDS**
        m = re.fullmatch(r"\*\*([A-Z][A-Z0-9 &/]*[A-Z])\*\*", line)
        if m:
            blocks.append({"type": "section_header", "text": m.group(1).strip()})
            continue
        # A line that is only bold but not a section header, or a bold prefix
        # with its colon outside the bold, would silently render as the wrong
        # block type. Refuse it instead.
        if re.fullmatch(r"\*\*[^*]+\*\*:?", line) or re.match(r"\*\*[^*]+\*\*:", line):
            raise ResumeContentError(
                f"ERROR: {line!r} is neither a section header (**ALL CAPS**), a "
                f"job header (**Title** text), nor a skills line (**Label:** items)."
            )
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
                    "runs": _inline_runs(line),
                })
                continue
            runs: list[Run] = [
                {"text": bold_prefix, "bold": True},
            ]
            tail = m.group(2)
            if tail:
                runs.extend(_inline_runs(tail))
            blocks.append({"type": "job_header", "text": line, "runs": runs})
            continue
        # Plain body (may contain inline bold)
        blocks.append({
            "type": "body", "text": line,
            "runs": _inline_runs(line),
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


def _inline_runs(text: str) -> list[Run]:
    """Split text into runs at **bold** and [text](url) boundaries.

    Plain text becomes plain runs; **x** becomes a bold run; [t](u) becomes a link run (bold
    inside a link or a link inside bold are NOT supported -- if a
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


# =====================================================================
# docx -> pdf
# =====================================================================

WORD_APP = Path("/Applications/Microsoft Word.app")
# Word's sandbox keeps one folder-access grant for a fixed path, so every
# conversion goes through the same staging dir.
PDF_STAGING = paths.REPO_ROOT / ".pdf_staging"


class PdfResult(TypedDict):
    status: str        # "ok" | "skipped" | "failed"
    path: str          # the pdf, when status == "ok"
    pages: int
    detail: str


def _convert(docx_path: Path, pdf_path: Path, token: str) -> PdfResult | None:
    """One Word conversion; a failure result, or None when the pdf is in place."""
    name = f"{token}_{docx_path.stem}.docx"
    staged_docx = PDF_STAGING / name
    staged_pdf = staged_docx.with_suffix(".pdf")
    staged_pdf.unlink(missing_ok=True)
    shutil.copyfile(docx_path, staged_docx)
    # A namesake left open by an earlier failed run makes `document "<name>"` fail
    # with -1728, so close any first; and close ours whether or not the save worked.
    script = (
        'tell application "Microsoft Word"\n'
        '    repeat with i from (count of documents) to 1 by -1\n'
        '        try\n'
        f'            if (name of document i) is "{name}" then close document i saving no\n'
        '        end try\n'
        '    end repeat\n'
        f'    open POSIX file "{staged_docx}"\n'
        f'    set theDoc to document "{name}"\n'
        '    try\n'
        f'        save as theDoc file format format PDF file name "{staged_pdf}"\n'
        '    on error errMsg number errNum\n'
        '        try\n'
        '            close theDoc saving no\n'
        '        end try\n'
        '        error errMsg number errNum\n'
        '    end try\n'
        '    close theDoc saving no\n'
        'end tell\n'
    )
    try:
        proc = subprocess.run(["osascript"], input=script, text=True, encoding="utf-8",
                              capture_output=True, timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        staged_docx.unlink(missing_ok=True)
        return PdfResult(status="failed", path="", pages=0, detail=f"osascript: {exc}")
    staged_docx.unlink(missing_ok=True)
    if proc.returncode != 0 or not staged_pdf.is_file() or staged_pdf.stat().st_size == 0:
        staged_pdf.unlink(missing_ok=True)
        return PdfResult(status="failed", path="", pages=0,
                         detail=(proc.stderr or "Word produced no pdf").strip())
    shutil.move(str(staged_pdf), pdf_path)
    return None


def export_pdf(docx_path: Path, token: str) -> PdfResult:
    """Convert docx_path to a same-named .pdf beside it through Microsoft Word.

    Any older pdf is deleted first, so a failed export can never leave a stale
    one looking current. `token` makes the staging name unique per role: Word
    resolves a document by name and would export an already-open namesake.
    No Word (not macOS, or not installed) returns "skipped"."""
    pdf_path = docx_path.with_suffix(".pdf")
    pdf_path.unlink(missing_ok=True)
    if sys.platform != "darwin" or not WORD_APP.exists():
        return PdfResult(status="skipped", path="", pages=0,
                         detail="Microsoft Word on macOS is needed for the pdf; the docx is the deliverable")
    PDF_STAGING.mkdir(exist_ok=True)
    # Word is one app: two conversions at once (parallel /tailor runs) collide with
    # -1728 "object does not exist", so they take turns.
    with locked(PDF_STAGING.parent / f"{PDF_STAGING.name.lstrip('.')}_word"):
        failed = _convert(docx_path, pdf_path, token)
    if failed:
        return failed
    return PdfResult(status="ok", path=str(pdf_path), pages=pdf_pages(pdf_path), detail="")


def pdf_pages(pdf_path: Path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(str(pdf_path)).pages)
