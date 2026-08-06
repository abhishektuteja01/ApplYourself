"""The two committed .docx templates are the repo's only tracked binaries.

They exist because a Word template cannot be a commented text file, and they are
allowlisted by name in scripts/pii_scan.sh -- which means the text scan that
protects every other tracked file does not protect these two. These tests are
that protection instead:

  - contract: both still satisfy the renderers that consume them, so a fresh
    clone can render a resume and a cover letter without opening Word.
  - guard: neither carries text beyond an approved set, and neither carries an
    author in its core properties. Re-saving either in Word stamps the editor's
    name into docProps/core.xml, which is exactly what the PII gate exists to
    stop and exactly what the allowlist would let through.
"""
from __future__ import annotations

import importlib.util
import re
import zipfile
from pathlib import Path

import pytest
from docx import Document

from src.docx_cover_letter import (
    COVER_LETTER_OPTIONAL_PLACEHOLDERS,
    COVER_LETTER_REQUIRED_PLACEHOLDERS,
    render_cover_letter,
)
from src.docx_render import (
    REQUIRED_CHARACTER_STYLE,
    REQUIRED_STYLES,
    render_resume,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RESUME_TEMPLATE = REPO_ROOT / "profile" / "resume_template.example.docx"
COVER_TEMPLATE = REPO_ROOT / "profile" / "cover_letter_template.example.docx"
GENERATOR = REPO_ROOT / "scripts" / "make_example_templates.py"

# Hardcoded on purpose: an independent statement of what these files are allowed
# to contain, not a restatement of the generator. test_generator_text_matches_
# approved_set then cross-checks the two, so drift in either direction fails.
APPROVED_RESUME_TEXT = {
    "YOUR NAME (TEMPLATE)",
    "City, ST | phone | email | portfolio",
    "SECTION HEADER",
    "Employer or Project — Title, Dates",
    "Restyle each of these five paragraph styles in Word.",
    "Bullet text renders in this style.",
}
APPROVED_COVER_TEXT = set(COVER_LETTER_REQUIRED_PLACEHOLDERS) | set(
    COVER_LETTER_OPTIONAL_PLACEHOLDERS
)

# Only fields python-docx's CoreProperties actually exposes. "company" and
# "manager" are app.xml properties, not core properties -- asserting them here
# would raise AttributeError, and assigning them in the generator would silently
# scrub nothing. They are checked against app.xml instead.
# The run-text element, and ONLY it. A bare <w:t[^>]*> also matches <w:tbl>,
# <w:tc>, <w:tr> and <w:tab/>, which swallows table markup into the "text" and
# makes both the failure messages and the comparison unreliable.
_W_T_RE = re.compile(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", re.S)

CORE_PROPERTIES_MUST_BE_EMPTY = (
    "author",
    "last_modified_by",
    "title",
    "subject",
    "comments",
    "category",
    "keywords",
)
APP_PROPERTIES_MUST_BE_EMPTY = ("Company", "Manager", "HyperlinkBase")

# Exact part list both templates must have. An allowlist, not a denylist: the
# parts that carry identity (docProps/custom.xml, word/comments.xml,
# word/people.xml, word/footnotes.xml, word/embeddings/*) are exactly the ones
# nobody thinks to check for. customXml/item1.xml is python-docx's inherited
# empty bibliography.
EXPECTED_PARTS = {
    "[Content_Types].xml",
    "_rels/.rels",
    "customXml/_rels/item1.xml.rels",
    "customXml/item1.xml",
    "customXml/itemProps1.xml",
    "docProps/app.xml",
    "docProps/core.xml",
    "word/_rels/document.xml.rels",
    "word/document.xml",
    "word/fontTable.xml",
    "word/numbering.xml",
    "word/settings.xml",
    "word/styles.xml",
    "word/stylesWithEffects.xml",
    "word/theme/theme1.xml",
    "word/webSettings.xml",
}


def _load_generator():
    spec = importlib.util.spec_from_file_location("make_example_templates", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _document_text(path: Path) -> set[str]:
    """Every non-empty body paragraph string, as python-docx sees it.

    Used for the contract tests, which care about what the RENDERERS read.
    Never use it as the PII guard: see _all_package_text.
    """
    doc = Document(str(path))
    return {p.text.strip() for p in doc.paragraphs if p.text.strip()}


def _all_package_text(path: Path) -> set[str]:
    """Every <w:t> string in every XML part of the archive.

    The PII guard has to be closed by construction, and Document.paragraphs is
    not: it reads direct w:r/w:hyperlink children of top-level w:p elements in
    word/document.xml only. Text in a table cell, a content control (w:sdt), a
    tracked insertion (w:ins), a smart tag, a text box (w:txbxContent), a
    footnote, or a comment is invisible to it — and render_cover_letter
    preserves all of those verbatim into a generated letter.
    """
    found: set[str] = set()
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if not name.endswith(".xml"):
                continue
            xml = zf.read(name).decode("utf-8")
            found |= {
                text.strip()
                for text in _W_T_RE.findall(xml)
                if text.strip()
            }
    return found


class TestFilesExist:
    def test_both_templates_are_committed(self):
        assert RESUME_TEMPLATE.exists(), f"{RESUME_TEMPLATE} missing"
        assert COVER_TEMPLATE.exists(), f"{COVER_TEMPLATE} missing"

    def test_both_are_valid_zip_archives(self):
        for path in (RESUME_TEMPLATE, COVER_TEMPLATE):
            assert zipfile.is_zipfile(path), f"{path} is not a valid .docx"


class TestResumeTemplateContract:
    def test_declares_every_required_style(self):
        names = {s.name for s in Document(str(RESUME_TEMPLATE)).styles}
        missing = [s for s in REQUIRED_STYLES if s not in names]
        assert not missing, f"template missing paragraph styles: {missing}"
        assert REQUIRED_CHARACTER_STYLE in names

    def test_has_no_tables_or_inline_shapes(self):
        doc = Document(str(RESUME_TEMPLATE))
        assert not doc.tables, "tables are ATS-hostile and the renderer rejects them"
        assert not doc.inline_shapes, "inline shapes are rejected by the renderer"

    def test_renders_a_real_resume(self, tmp_path):
        """The example_primary lane resume is markdown a new clone actually has."""
        md = (
            REPO_ROOT
            / "profile/verticals/example_primary/resume_example_primary.md"
        ).read_text(encoding="utf-8")
        out = tmp_path / "resume.docx"
        render_resume(md, RESUME_TEMPLATE, out)
        doc = Document(str(out))
        used = {p.style.name for p in doc.paragraphs}
        assert used <= set(REQUIRED_STYLES), f"unexpected styles rendered: {used}"
        assert "Resume Bullet" in used and "Resume Name" in used

    def test_demo_text_never_reaches_a_rendered_resume(self, tmp_path):
        """render_resume clears the body, so the style samples are safe to ship.
        If that ever stops being true, the template's demo text starts appearing
        in real resumes."""
        out = tmp_path / "resume.docx"
        render_resume("**Real Name**\n\nCity, ST", RESUME_TEMPLATE, out)
        rendered = _document_text(out)
        assert not (rendered & APPROVED_RESUME_TEXT), (
            f"template demo text leaked into the rendered resume: "
            f"{sorted(rendered & APPROVED_RESUME_TEXT)}"
        )


class TestCoverLetterTemplateContract:
    def test_every_placeholder_is_present_as_its_own_paragraph(self):
        paragraphs = _document_text(COVER_TEMPLATE)
        for token in COVER_LETTER_REQUIRED_PLACEHOLDERS:
            assert token in paragraphs, f"required placeholder {token} missing"
        for token in COVER_LETTER_OPTIONAL_PLACEHOLDERS:
            assert token in paragraphs, f"optional placeholder {token} missing"

    def test_contains_nothing_but_placeholders(self):
        """Non-placeholder paragraphs are preserved verbatim into every letter,
        so any instructional or identity text here ships to an employer."""
        extra = _document_text(COVER_TEMPLATE) - APPROVED_COVER_TEXT
        assert not extra, f"template carries non-placeholder text: {sorted(extra)}"

    def test_has_no_tables_shapes_or_headers(self):
        """render_cover_letter preserves everything that is not a placeholder
        paragraph. A letterhead table is the classic way identity text reaches
        every letter while staying invisible to a paragraph-level scan."""
        doc = Document(str(COVER_TEMPLATE))
        assert not doc.tables, "a table here ships verbatim in every letter"
        assert not doc.inline_shapes, "a shape here ships verbatim in every letter"
        for i, section in enumerate(doc.sections):
            for label, part in (("header", section.header), ("footer", section.footer)):
                text = " ".join(p.text for p in part.paragraphs).strip()
                assert not text, f"section {i + 1} {label} carries text: {text!r}"

    def test_renders_a_real_cover_letter(self, tmp_path):
        out = tmp_path / "cl.docx"
        render_cover_letter(
            {
                "salutation": "Dear Hiring Manager,",
                "body": ["First paragraph.", "Second paragraph."],
                "date": "January 1, 2026",
                "closing": "Sincerely,",
                "signoff_name": "Your Name",
            },
            COVER_TEMPLATE,
            out,
        )
        texts = [p.text for p in Document(str(out)).paragraphs if p.text.strip()]
        assert texts == [
            "January 1, 2026",
            "Dear Hiring Manager,",
            "First paragraph.",
            "Second paragraph.",
            "Sincerely,",
            "Your Name",
        ]
        assert not (set(texts) & set(COVER_LETTER_REQUIRED_PLACEHOLDERS)), (
            "an unfilled placeholder survived into the output"
        )


class TestNoPIILeak:
    """The allowlist in pii_scan.sh disables the text scan for these two files.
    These assertions replace it."""

    @pytest.mark.parametrize(
        "path,approved",
        [
            (RESUME_TEMPLATE, APPROVED_RESUME_TEXT),
            (COVER_TEMPLATE, APPROVED_COVER_TEXT),
        ],
        ids=["resume", "cover_letter"],
    )
    def test_text_is_confined_to_the_approved_set(self, path, approved):
        """Scans every XML part, not just body paragraphs — a letterhead table or
        a tracked-change run would otherwise pass this and still ship."""
        extra = _all_package_text(path) - approved
        assert not extra, (
            f"{path.name} gained text outside its approved set: {sorted(extra)}. "
            f"If this is intentional, update the approved set AND confirm the new "
            f"text carries no personal information."
        )

    @pytest.mark.parametrize(
        "path", [RESUME_TEMPLATE, COVER_TEMPLATE], ids=["resume", "cover_letter"]
    )
    def test_carries_no_authored_revisions_or_comments(self, path):
        """w:author appears on tracked changes and comments and carries a real
        name. It is not text, so the scan above cannot see it."""
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if not name.endswith(".xml"):
                    continue
                xml = zf.read(name).decode("utf-8")
                assert "w:author" not in xml, f"{path.name}:{name} carries w:author"

    @pytest.mark.parametrize(
        "path", [RESUME_TEMPLATE, COVER_TEMPLATE], ids=["resume", "cover_letter"]
    )
    def test_package_parts_are_an_exact_allowlist(self, path):
        """A denylist of known-bad parts cannot hold a file whose only other
        guard is disabled. Any new part — docProps/custom.xml, word/comments.xml,
        word/people.xml, word/embeddings/* — must be reviewed deliberately."""
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
        assert names == EXPECTED_PARTS, (
            f"{path.name} part list changed.\n"
            f"  added:   {sorted(names - EXPECTED_PARTS)}\n"
            f"  removed: {sorted(EXPECTED_PARTS - names)}"
        )

    @pytest.mark.parametrize(
        "path", [RESUME_TEMPLATE, COVER_TEMPLATE], ids=["resume", "cover_letter"]
    )
    def test_core_properties_carry_no_identity(self, path):
        cp = Document(str(path)).core_properties
        for field in CORE_PROPERTIES_MUST_BE_EMPTY:
            value = getattr(cp, field) or ""
            assert not value.strip(), (
                f"{path.name} core property {field!r} is {value!r}. Word stamps "
                f"this on re-save; regenerate with "
                f"scripts/make_example_templates.py instead."
            )

    @pytest.mark.parametrize(
        "path", [RESUME_TEMPLATE, COVER_TEMPLATE], ids=["resume", "cover_letter"]
    )
    def test_app_properties_carry_no_identity(self, path):
        with zipfile.ZipFile(path) as zf:
            app = zf.read("docProps/app.xml").decode("utf-8")
        for field in APP_PROPERTIES_MUST_BE_EMPTY:
            match = re.search(rf"<{field}>(.*?)</{field}>", app, re.S)
            value = match.group(1).strip() if match else ""
            assert not value, f"{path.name} app property {field} is {value!r}"

    @pytest.mark.parametrize(
        "path", [RESUME_TEMPLATE, COVER_TEMPLATE], ids=["resume", "cover_letter"]
    )
    def test_no_embedded_images_headers_or_thumbnail(self, path):
        """An image, header or thumbnail could carry a signature block the
        paragraph scan above would never see. render_resume clears the body but
        preserves headers and footers, so a header is a live leak path."""
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
        assert not [n for n in names if n.startswith("word/media/")], (
            f"{path.name} embeds media"
        )
        assert not [n for n in names if "header" in n or "footer" in n], (
            f"{path.name} carries a header/footer, which renderers preserve"
        )
        assert "docProps/thumbnail.jpeg" not in names, (
            f"{path.name} carries an embedded thumbnail image; regenerate with "
            f"scripts/make_example_templates.py, which strips it"
        )

    @pytest.mark.parametrize(
        "path", [RESUME_TEMPLATE, COVER_TEMPLATE], ids=["resume", "cover_letter"]
    )
    def test_thumbnail_relationship_is_gone_too(self, path):
        """A dangling relationship to a removed part is a malformed package."""
        with zipfile.ZipFile(path) as zf:
            rels = zf.read("_rels/.rels").decode("utf-8")
        assert "thumbnail" not in rels


class TestGeneratorStaysInSync:
    def test_generator_text_matches_approved_set(self):
        """Drift detector in both directions: the generator cannot quietly add a
        line the guard does not know about, and the guard cannot rot."""
        module = _load_generator()
        generated = {text for _style, text in module.STYLE_DEMOS}
        assert generated == APPROVED_RESUME_TEXT

    def test_generator_placeholder_order_matches_source_of_truth(self):
        module = _load_generator()
        assert set(module.COVER_PLACEHOLDER_ORDER) == APPROVED_COVER_TEXT

    def test_generator_declares_only_styles_the_renderer_requires(self):
        module = _load_generator()
        styles_written = {style for style, _text in module.STYLE_DEMOS}
        assert styles_written <= set(REQUIRED_STYLES)
