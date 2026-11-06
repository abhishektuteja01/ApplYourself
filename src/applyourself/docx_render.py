"""ATS-clean resume docx generation.

Determinism boundary (R7): no LLM calls.

render_resume uses templated paragraph styles only: the template
(profile/resume_template.docx) defines the visual formatting, and this module
writes resume_model's typed blocks as paragraphs in the matching styles. The
template MUST define these 5 named paragraph styles:
  - Resume Name        (the name at top)
  - Resume Section     (SUMMARY / WORK EXPERIENCE / EDUCATION / ...)
  - Resume Job Header  (role / degree / project headers; bold via inline runs)
  - Resume Body        (contact line, summary text, skills lines)
  - Resume Bullet      (bulleted items)
Forbidden in the template (rejected loudly): tables (ATS-unfriendly) and inline
shapes (images, icons).

Cover letters are the opposite operation — preserve the user's design and fill
placeholders — and live in src/applyourself/docx_cover_letter.py.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TypedDict

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from applyourself.resume_model import Block
from applyourself.shared import paths
from applyourself.shared.parquet_io import locked

REQUIRED_STYLES = [
    "Resume Name",
    "Resume Section",
    "Resume Job Header",
    "Resume Body",
    "Resume Bullet",
]

# Required character-type style for clickable link runs.
REQUIRED_CHARACTER_STYLE = "Hyperlink"


class TemplateMissingError(FileNotFoundError):
    """Template file does not exist on disk."""


class TemplateError(ValueError):
    """Template exists but violates the docx_render contract."""


STYLE_OF = {
    "name": "Resume Name",
    "section": "Resume Section",
    "header": "Resume Job Header",
    "body": "Resume Body",
    "bullet": "Resume Bullet",
}


# =====================================================================
# Public API
# =====================================================================


def render_resume(blocks: Sequence[Block], template_path: Path, out_path: Path) -> None:
    """Render resume blocks to an ATS-clean docx using the template.
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
    _write_blocks(doc, blocks)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))


# =====================================================================
# Internals
# =====================================================================


def _validate_template(doc, path: Path) -> None:
    style_names = {s.name for s in doc.styles}
    missing = [s for s in REQUIRED_STYLES if s not in style_names]
    if missing:
        raise TemplateError(
            f"Template {path} missing required paragraph styles: {missing}. "
            f"Each maps to a resume block type — define them in Word's Styles "
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


def _write_blocks(doc, blocks: Sequence[Block]) -> None:
    """Write blocks to the doc body.

    Two layout rules applied here:

    1. The first body block immediately after the name block is the contact
       line. Force-center it via WD_ALIGN_PARAGRAPH.CENTER on just that
       paragraph; the style stays Resume Body so the font / size inherit.
       Per the template contract (5 styles only), we do not add a sixth
       Resume Contact style.
    2. Any run text containing a tab character is split at the tab;
       add_run("\\t") is emitted between the pieces so Word's layout engine
       advances to the paragraph style's next tab stop. Resume Job Header
       carries a right tab stop at 19.05 cm -- this is what right-aligns
       the date on "Title ... <TAB> Date" header lines.
    """
    name_just_emitted = False
    for block in blocks:
        if not block.runs:
            continue
        p = doc.add_paragraph(style=STYLE_OF[block.kind])
        if block.kind == "body" and name_just_emitted:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        for r in block.runs:
            _emit_run_splitting_tabs(p, r.text, bold=r.bold, url=r.url)
        name_just_emitted = block.kind == "name"


def _emit_run_splitting_tabs(paragraph, text: str, *, bold: bool, url: str | None = None) -> None:
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


# =====================================================================
# docx -> pdf
# =====================================================================

WORD_APP = Path("/Applications/Microsoft Word.app")
# Word's sandbox keeps one folder-access grant for a fixed path, so every
# conversion goes through the same staging dir.


class PdfResult(TypedDict):
    status: str  # "ok" | "skipped" | "failed"
    path: str  # the pdf, when status == "ok"
    pages: int
    detail: str


def _convert(docx_path: Path, pdf_path: Path, token: str) -> PdfResult | None:
    """One Word conversion; a failure result, or None when the pdf is in place."""
    # Plain characters only: Word cannot find a document by a name like "göteborg"
    # (-1728), as the file system and AppleScript spell such letters differently.
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{token}_{docx_path.stem}") + ".docx"
    staged_docx = paths.PDF_STAGING / name
    staged_pdf = staged_docx.with_suffix(".pdf")
    staged_pdf.unlink(missing_ok=True)
    shutil.copyfile(docx_path, staged_docx)
    # Close a namesake an earlier failed run left open, then keep the reference to
    # the document this open made: a lookup by name races the open (-1728). Ours is
    # closed whether or not the save worked.
    script = (
        'tell application "Microsoft Word"\n'
        "    repeat with i from (count of documents) to 1 by -1\n"
        "        try\n"
        f'            if (name of document i) is "{name}" then close document i saving no\n'
        "        end try\n"
        "    end repeat\n"
        "    set theDoc to missing value\n"
        f'    open POSIX file "{staged_docx}"\n'
        "    repeat 100 times\n"
        "        try\n"
        f'            if (name of active document) is "{name}" then\n'
        "                set theDoc to active document\n"
        "                exit repeat\n"
        "            end if\n"
        "        end try\n"
        "        delay 0.2\n"
        "    end repeat\n"
        f'    if theDoc is missing value then error "Word never opened {name}" number -1728\n'
        "    try\n"
        f'        save as theDoc file format format PDF file name "{staged_pdf}"\n'
        "    on error errMsg number errNum\n"
        "        try\n"
        "            close theDoc saving no\n"
        "        end try\n"
        "        error errMsg number errNum\n"
        "    end try\n"
        "    close theDoc saving no\n"
        "end tell\n"
    )
    try:
        proc = subprocess.run(
            ["osascript"],
            input=script,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        staged_docx.unlink(missing_ok=True)
        return PdfResult(status="failed", path="", pages=0, detail=f"osascript: {exc}")
    staged_docx.unlink(missing_ok=True)
    if proc.returncode != 0 or not staged_pdf.is_file() or staged_pdf.stat().st_size == 0:
        staged_pdf.unlink(missing_ok=True)
        return PdfResult(
            status="failed",
            path="",
            pages=0,
            detail=(proc.stderr or "Word produced no pdf").strip(),
        )
    shutil.move(str(staged_pdf), pdf_path)
    return None


def export_pdf(docx_path: Path, token: str) -> PdfResult:
    """Convert docx_path to a same-named .pdf beside it through Microsoft Word.

    Any older pdf is deleted first, so a failed export can never leave a stale
    one looking current. `token` makes the staging name unique per role: Word
    resolves a document by name and would export an already-open namesake.
    A failure restarts Word once and tries again (pdf_sweep).
    No Word (not macOS, or not installed) returns "skipped". With PDF_BROKER_DIR
    set, the night's helper (src/applyourself/pdf_broker.py) does the conversion."""
    queue = os.environ.get("PDF_BROKER_DIR")
    if queue:
        from applyourself import pdf_broker

        return pdf_broker.request(Path(queue), docx_path, token)
    return convert_locally(docx_path, token)


# Quits Word only when no document is open, so a restart never closes the user's work.
_RESTART_WORD = """
tell application "Microsoft Word"
    if (count of documents) > 0 then error "other documents are open in Word"
    quit saving no
end tell
repeat 60 times
    if application "Microsoft Word" is not running then return
    delay 0.5
end repeat
error "Word did not quit"
"""


def _restart_word() -> str:
    """Quit Word so the next conversion starts a fresh one; "" or why not."""
    try:
        proc = subprocess.run(
            ["osascript"],
            input=_RESTART_WORD,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"osascript: {exc}"
    return "" if proc.returncode == 0 else (proc.stderr or "quit failed").strip()


def pdf_sweep(jobs: Sequence[tuple[Path, str]]) -> list[PdfResult]:
    """Convert each (docx, token) to a pdf beside it, one at a time. The first
    failed conversion restarts Word once and is tried again; later ones are not."""
    for docx_path, _ in jobs:
        docx_path.with_suffix(".pdf").unlink(missing_ok=True)
    if sys.platform != "darwin" or not WORD_APP.exists():
        return [
            PdfResult(
                status="skipped",
                path="",
                pages=0,
                detail="Microsoft Word on macOS is needed for the pdf; the docx is the deliverable",
            )
            for _ in jobs
        ]
    paths.PDF_STAGING.mkdir(exist_ok=True)
    results: list[PdfResult] = []
    restarted = False
    # Word is one app: two conversions at once (parallel /tailor runs) collide with
    # -1728 "object does not exist", so they take turns.
    with locked(paths.PDF_STAGING / "word"):
        for docx_path, token in jobs:
            pdf_path = docx_path.with_suffix(".pdf")
            failed = _convert(docx_path, pdf_path, token)
            if failed and not restarted:
                restarted = True
                why = _restart_word()
                if why:
                    failed["detail"] += f"; Word not restarted: {why}"
                else:
                    failed = _convert(docx_path, pdf_path, token)
            results.append(
                failed
                or PdfResult(status="ok", path=str(pdf_path), pages=pdf_pages(pdf_path), detail="")
            )
    return results


def convert_locally(docx_path: Path, token: str) -> PdfResult:
    """export_pdf in this process: a sweep of one."""
    return pdf_sweep([(docx_path, token)])[0]


def pdf_pages(pdf_path: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(pdf_path)).pages)
