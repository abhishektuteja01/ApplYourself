"""Tests for src/applyourself/docx_render.py — ATS-clean docx generation.

Uses a programmatic fixture template at test setup so tests don't depend on
the user's hand-authored template (which is part of slice 3 / onboarding)."""

from __future__ import annotations

from pathlib import Path

import pytest
from docx import Document
from docx.enum.style import WD_STYLE_TYPE

from applyourself.docx_cover_letter import (
    list_cover_letter_placeholders,
    render_cover_letter,
)
from applyourself.docx_render import (
    REQUIRED_STYLES,
    TemplateError,
    TemplateMissingError,
    render_resume,
)
from applyourself.resume_model import Block, Run, inline_runs
from applyourself.shared import paths


def _name(text):
    return Block("name", (Run(text),), f"**{text}**")


def _section(text):
    return Block("section", (Run(text),), f"**{text}**")


def _body(text):
    return Block("body", inline_runs(text), text)


def _bullet(text):
    return Block("bullet", inline_runs(text), f"* {text}")


def _header(title, tail):
    return Block("header", (Run(title, bold=True), *inline_runs(tail)), f"**{title}**{tail}")


# ---------- fixture templates ----------


def _make_template(
    path: Path,
    *,
    styles: list[str] | None = None,
    add_table: bool = False,
    include_hyperlink_style: bool = True,
) -> Path:
    """Build a minimal template with the requested styles. styles=None means
    all 5 required paragraph styles. The Hyperlink character style is added
    explicitly because python-docx's default Document() leaves it latent
    (not realised in styles.xml) until first use."""
    if styles is None:
        styles = list(REQUIRED_STYLES)
    doc = Document()
    existing = {s.name for s in doc.styles}
    for name in styles:
        if name not in existing:
            doc.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
    if include_hyperlink_style and "Hyperlink" not in {s.name for s in doc.styles}:
        doc.styles.add_style("Hyperlink", WD_STYLE_TYPE.CHARACTER)
    if add_table:
        doc.add_table(rows=2, cols=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


# ---------- fail-loud paths ----------


def test_render_fails_loud_on_missing_template(tmp_path):
    out = tmp_path / "resume.docx"
    nope = tmp_path / "does_not_exist.docx"
    with pytest.raises(TemplateMissingError) as exc:
        render_resume([_name("Name")], nope, out)
    msg = str(exc.value)
    # Must be actionable: name the file + the ATS constraints to author
    assert "does_not_exist.docx missing" in msg
    assert "ATS" in msg
    assert "Calibri" in msg
    assert not out.exists()


def test_render_fails_loud_on_missing_style(tmp_path):
    # Build a template missing "Resume Bullet"
    template = _make_template(
        tmp_path / "tmpl.docx",
        styles=[s for s in REQUIRED_STYLES if s != "Resume Bullet"],
    )
    out = tmp_path / "resume.docx"
    with pytest.raises(TemplateError) as exc:
        render_resume([_name("Name")], template, out)
    assert "Resume Bullet" in str(exc.value)
    assert not out.exists()


def test_render_fails_loud_on_tables_in_template(tmp_path):
    template = _make_template(tmp_path / "tmpl.docx", add_table=True)
    out = tmp_path / "resume.docx"
    with pytest.raises(TemplateError) as exc:
        render_resume([_name("Name")], template, out)
    assert "table" in str(exc.value).lower()
    assert not out.exists()


# ---------- render produces valid docx ----------


def test_render_creates_non_empty_docx(tmp_path):
    template = _make_template(tmp_path / "tmpl.docx")
    out = tmp_path / "resume.docx"
    blocks = [
        _name("Jane Doe"),
        _body("contact line"),
        _section("SUMMARY"),
        _body("A short summary paragraph."),
        _section("WORK EXPERIENCE"),
        _header("Advisory Analyst", " - Acme Corp\tMay 2022 – Jul 2024"),
        _bullet("Owned the daily report."),
    ]
    render_resume(blocks, template, out)
    assert out.exists()
    assert out.stat().st_size > 0
    doc = Document(str(out))
    assert len(doc.paragraphs) > 0


def test_render_output_has_no_tables(tmp_path):
    template = _make_template(tmp_path / "tmpl.docx")
    out = tmp_path / "resume.docx"
    render_resume([_name("Name"), _bullet("bullet")], template, out)
    doc = Document(str(out))
    assert len(doc.tables) == 0
    assert len(doc.inline_shapes) == 0


def test_render_applies_required_styles_by_name(tmp_path):
    template = _make_template(tmp_path / "tmpl.docx")
    out = tmp_path / "resume.docx"
    blocks = [_name("Jane"), _section("SUMMARY"), _body("A line."), _bullet("a bullet")]
    render_resume(blocks, template, out)
    doc = Document(str(out))
    style_names = [p.style.name for p in doc.paragraphs]
    assert "Resume Name" in style_names
    assert "Resume Section" in style_names
    assert "Resume Body" in style_names
    assert "Resume Bullet" in style_names


# ---------- layout rules ----------


def test_render_emits_tab_in_job_header_paragraph(tmp_path):
    """A literal \\t in a job_header line must reach the docx as a tab
    character so the template's right tab stop catches the date."""
    template = _make_template(tmp_path / "tmpl.docx")
    out = tmp_path / "resume.docx"
    blocks = [_name("Jane"), _header("Advisory Analyst", " - Acme Corp\tMay 2022 – Jul 2024")]
    render_resume(blocks, template, out)
    doc = Document(str(out))
    # Find the job_header paragraph
    headers = [p for p in doc.paragraphs if p.style.name == "Resume Job Header"]
    assert len(headers) == 1
    # The full text of the paragraph should contain a tab character
    assert "\t" in headers[0].text
    # And it should split company-on-left from date-on-right
    left, right = headers[0].text.split("\t", 1)
    assert "Acme Corp" in left
    assert "May 2022" in right


def test_render_centers_contact_line_first_body_after_name(tmp_path):
    """The first body block immediately after the name block is the contact
    line; render must force-center just that paragraph (Resume Body style
    still, alignment override)."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    template = _make_template(tmp_path / "tmpl.docx")
    out = tmp_path / "resume.docx"
    render_resume(
        [_name("Jane"), _body("contact line"), _body("A later body line.")], template, out
    )
    doc = Document(str(out))
    body_paras = [p for p in doc.paragraphs if p.style.name == "Resume Body"]
    assert len(body_paras) == 2
    # First body after name = contact line = centered
    assert body_paras[0].alignment == WD_ALIGN_PARAGRAPH.CENTER
    # Second body = NOT forced-centered (inherits style default = None / left)
    assert body_paras[1].alignment != WD_ALIGN_PARAGRAPH.CENTER


def test_render_does_not_center_when_section_header_intervenes(tmp_path):
    """If something other than body comes right after name, no contact
    centering applies to the next body line."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    template = _make_template(tmp_path / "tmpl.docx")
    out = tmp_path / "resume.docx"
    render_resume([_name("Jane"), _section("SUMMARY"), _body("A summary line.")], template, out)
    doc = Document(str(out))
    body_paras = [p for p in doc.paragraphs if p.style.name == "Resume Body"]
    assert len(body_paras) == 1
    # The summary line is the first body, but it's NOT immediately after name
    # (the SUMMARY section header intervenes), so no centering forced.
    assert body_paras[0].alignment != WD_ALIGN_PARAGRAPH.CENTER


# ---------- inline links ----------


def test_inline_runs_split_links_and_bold():
    runs = inline_runs("The **bold** part, [link](https://x) and [me](mailto:me@x.io) here")
    assert Run("bold", bold=True) in runs
    assert Run("link", url="https://x") in runs and Run("me", url="mailto:me@x.io") in runs
    assert runs[-1] == Run(" here")


def test_render_emits_hyperlink_element_with_correct_target(tmp_path):
    """Rendered docx contains a w:hyperlink element pointing at the URL."""
    template = _make_template(tmp_path / "tmpl.docx")
    out = tmp_path / "resume.docx"
    render_resume(
        [
            _name("N"),
            _body("[Portfolio](https://janedoe.example.com/) and [Email](mailto:x@y.com)"),
        ],
        template,
        out,
    )
    doc = Document(str(out))
    W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    hlinks = doc.element.body.findall(f".//{W_NS}hyperlink")
    assert len(hlinks) == 2
    # Verify both target URLs landed in the doc's relationships
    targets = [r.target_ref for r in doc.part.rels.values() if "hyperlink" in r.reltype]
    assert any("janedoe.example.com" in t for t in targets)
    assert any(t == "mailto:x@y.com" for t in targets)


def test_render_emits_hyperlink_with_hyperlink_char_style(tmp_path):
    """Each hyperlink's inner run carries the 'Hyperlink' character style
    so Word renders it blue+underlined per the template's style def."""
    template = _make_template(tmp_path / "tmpl.docx")
    out = tmp_path / "resume.docx"
    render_resume([_name("N"), _body("[t](https://x)")], template, out)
    doc = Document(str(out))
    W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    rStyles = doc.element.body.findall(f".//{W_NS}hyperlink//{W_NS}rStyle")
    assert len(rStyles) == 1
    assert rStyles[0].get(f"{W_NS}val") == "Hyperlink"


def test_validate_template_requires_hyperlink_character_style(tmp_path):
    """Template missing the Hyperlink character style fails loud."""
    from applyourself.docx_render import _validate_template

    # Default python-docx Document() has Hyperlink. Delete it then validate.
    template = _make_template(tmp_path / "tmpl.docx")
    doc = Document(str(template))
    for style in list(doc.styles):
        if style.name == "Hyperlink":
            style.delete()
    with pytest.raises(TemplateError, match="Hyperlink"):
        _validate_template(doc, template)


# =====================================================================
# render_cover_letter — placeholder fill-in over the user's own design
# =====================================================================


def _make_cl_template(
    path: Path,
    *,
    tokens=("{{SALUTATION}}", "{{BODY}}"),
    duplicate: str | None = None,
    static_text=("Static header line",),
    body_style: str | None = None,
    body_bold: bool = False,
) -> Path:
    """A cover-letter template: static paragraphs plus one paragraph per token.
    body_style/body_bold mark the {{BODY}} paragraph so clone-formatting is
    observable in the output."""
    doc = Document()
    for line in static_text:
        doc.add_paragraph(line)
    for tok in tokens:
        if tok == "{{BODY}}" and (body_style or body_bold):
            p = doc.add_paragraph()
            if body_style:
                if body_style not in {s.name for s in doc.styles}:
                    doc.styles.add_style(body_style, WD_STYLE_TYPE.PARAGRAPH)
                p.style = doc.styles[body_style]
            run = p.add_run(tok)
            run.bold = body_bold
        else:
            doc.add_paragraph(tok)
    if duplicate:
        doc.add_paragraph(duplicate)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def _texts(path: Path) -> list[str]:
    return [p.text for p in Document(str(path)).paragraphs]


def _content(**over) -> dict:
    c = {"salutation": "Dear Hiring Manager,", "body": ["First para.", "Second para."]}
    c.update(over)
    return c


# ---------- fail-loud paths ----------


def test_cover_letter_fails_loud_on_missing_template(tmp_path):
    out = tmp_path / "cl.docx"
    with pytest.raises(TemplateMissingError) as exc:
        render_cover_letter(_content(), tmp_path / "nope.docx", out)
    msg = str(exc.value)
    assert "nope.docx missing" in msg
    # actionable: names both placeholder sets
    assert "{{SALUTATION}}" in msg and "{{BODY}}" in msg
    assert "{{DATE}}" in msg
    assert not out.exists()


@pytest.mark.parametrize(
    "tokens,missing",
    [
        (("{{BODY}}",), "{{SALUTATION}}"),
        (("{{SALUTATION}}",), "{{BODY}}"),
        ((), "{{SALUTATION}}"),
    ],
)
def test_cover_letter_missing_required_placeholder(tmp_path, tokens, missing):
    t = _make_cl_template(tmp_path / "t.docx", tokens=tokens)
    out = tmp_path / "cl.docx"
    with pytest.raises(TemplateError) as exc:
        render_cover_letter(_content(), t, out)
    assert missing in str(exc.value)
    assert not out.exists()


def test_cover_letter_duplicate_placeholder_is_rejected(tmp_path):
    """Two {{BODY}} paragraphs = ambiguous insert point; fail rather than
    guess."""
    t = _make_cl_template(tmp_path / "t.docx", duplicate="{{BODY}}")
    with pytest.raises(TemplateError, match="more than one paragraph"):
        render_cover_letter(_content(), t, tmp_path / "cl.docx")


def test_cover_letter_duplicate_optional_placeholder_is_rejected(tmp_path):
    t = _make_cl_template(
        tmp_path / "t.docx", tokens=("{{SALUTATION}}", "{{BODY}}", "{{DATE}}"), duplicate="{{DATE}}"
    )
    with pytest.raises(TemplateError, match=r"\{\{DATE\}\}"):
        render_cover_letter(_content(), t, tmp_path / "cl.docx")


@pytest.mark.parametrize(
    "bad",
    [
        {"salutation": "", "body": ["x"]},
        {"body": ["x"]},
        {"salutation": None, "body": ["x"]},
    ],
)
def test_cover_letter_requires_a_salutation(tmp_path, bad):
    t = _make_cl_template(tmp_path / "t.docx")
    with pytest.raises(ValueError, match="salutation"):
        render_cover_letter(bad, t, tmp_path / "cl.docx")


@pytest.mark.parametrize(
    "bad",
    [
        {"salutation": "Dear X,", "body": []},
        {"salutation": "Dear X,"},
        {"salutation": "Dear X,", "body": None},
    ],
)
def test_cover_letter_requires_a_nonempty_body(tmp_path, bad):
    t = _make_cl_template(tmp_path / "t.docx")
    with pytest.raises(ValueError, match="body"):
        render_cover_letter(bad, t, tmp_path / "cl.docx")


def test_cover_letter_validation_runs_before_writing(tmp_path):
    t = _make_cl_template(tmp_path / "t.docx")
    out = tmp_path / "nested" / "cl.docx"
    with pytest.raises(ValueError):
        render_cover_letter({"salutation": "Dear X,"}, t, out)
    assert not out.exists()


# ---------- happy paths ----------


def test_cover_letter_fills_salutation_and_expands_body(tmp_path):
    t = _make_cl_template(tmp_path / "t.docx")
    out = tmp_path / "cl.docx"
    render_cover_letter(_content(), t, out)
    assert _texts(out) == [
        "Static header line",
        "Dear Hiring Manager,",
        "First para.",
        "Second para.",
    ]


def test_cover_letter_removes_the_body_placeholder(tmp_path):
    t = _make_cl_template(tmp_path / "t.docx")
    out = tmp_path / "cl.docx"
    render_cover_letter(_content(), t, out)
    assert "{{BODY}}" not in "\n".join(_texts(out))


def test_cover_letter_leaves_no_raw_token_behind(tmp_path):
    t = _make_cl_template(tmp_path / "t.docx", tokens=("{{DATE}}", "{{SALUTATION}}", "{{BODY}}"))
    out = tmp_path / "cl.docx"
    render_cover_letter(_content(date="2026-06-06"), t, out)
    assert "{{" not in "\n".join(_texts(out))
    assert _texts(out) == [
        "Static header line",
        "2026-06-06",
        "Dear Hiring Manager,",
        "First para.",
        "Second para.",
    ]


def test_cover_letter_blanks_an_unsupplied_optional_placeholder(tmp_path):
    """A present-but-unfilled token becomes an empty line — never a literal
    {{DATE}} in a letter the user is about to send."""
    t = _make_cl_template(tmp_path / "t.docx", tokens=("{{DATE}}", "{{SALUTATION}}", "{{BODY}}"))
    out = tmp_path / "cl.docx"
    render_cover_letter(_content(), t, out)
    assert _texts(out) == [
        "Static header line",
        "",
        "Dear Hiring Manager,",
        "First para.",
        "Second para.",
    ]


def test_cover_letter_skips_optional_placeholders_absent_from_the_template(tmp_path):
    """The template may carry static text there instead; supplying the field
    must not inject a new paragraph."""
    t = _make_cl_template(tmp_path / "t.docx", static_text=("Static header line", "Sincerely,"))
    out = tmp_path / "cl.docx"
    render_cover_letter(_content(date="IGNORED"), t, out)
    texts = _texts(out)
    assert "IGNORED" not in texts
    assert texts == [
        "Static header line",
        "Sincerely,",
        "Dear Hiring Manager,",
        "First para.",
        "Second para.",
    ]


def test_cover_letter_preserves_untouched_static_paragraphs(tmp_path):
    t = _make_cl_template(
        tmp_path / "t.docx", static_text=("Jane Doe", "jane@example.com", "617-555-0100")
    )
    out = tmp_path / "cl.docx"
    render_cover_letter(_content(), t, out)
    assert _texts(out)[:3] == ["Jane Doe", "jane@example.com", "617-555-0100"]


def test_cover_letter_body_clones_keep_the_placeholder_formatting(tmp_path):
    """Each body paragraph inherits the {{BODY}} paragraph's style and run
    formatting — that is the whole point of cloning rather than appending."""
    t = _make_cl_template(tmp_path / "t.docx", body_style="CL Body", body_bold=True)
    out = tmp_path / "cl.docx"
    render_cover_letter(_content(), t, out)
    paras = [p for p in Document(str(out)).paragraphs if p.text in ("First para.", "Second para.")]
    assert len(paras) == 2
    for p in paras:
        assert p.style.name == "CL Body"
        assert p.runs[0].bold is True


def test_cover_letter_body_order_is_preserved(tmp_path):
    t = _make_cl_template(tmp_path / "t.docx")
    out = tmp_path / "cl.docx"
    render_cover_letter(_content(body=[f"P{i}." for i in range(6)]), t, out)
    assert _texts(out)[1:] == ["Dear Hiring Manager,"] + [f"P{i}." for i in range(6)]


def test_cover_letter_single_body_paragraph(tmp_path):
    t = _make_cl_template(tmp_path / "t.docx")
    out = tmp_path / "cl.docx"
    render_cover_letter(_content(body=["Only one."]), t, out)
    assert _texts(out) == ["Static header line", "Dear Hiring Manager,", "Only one."]


def test_cover_letter_creates_missing_output_dirs(tmp_path):
    t = _make_cl_template(tmp_path / "t.docx")
    out = tmp_path / "applications" / "example_primary" / "2026-06-06_acme" / "cl.docx"
    render_cover_letter(_content(), t, out)
    assert out.exists()


def test_cover_letter_leaves_the_template_untouched(tmp_path):
    t = _make_cl_template(tmp_path / "t.docx")
    before = t.read_bytes()
    render_cover_letter(_content(), t, tmp_path / "cl.docx")
    assert t.read_bytes() == before


def test_cover_letter_token_split_across_runs_is_still_matched(tmp_path):
    """Word often splits a typed token into several runs; matching on the
    paragraph's full text is what makes a hand-authored template work."""
    doc = Document()
    p = doc.add_paragraph()
    for chunk in ("{{SAL", "UTA", "TION}}"):
        p.add_run(chunk)
    doc.add_paragraph("{{BODY}}")
    t = tmp_path / "t.docx"
    doc.save(str(t))
    out = tmp_path / "cl.docx"
    render_cover_letter(_content(), t, out)
    assert _texts(out) == ["Dear Hiring Manager,", "First para.", "Second para."]


def test_cover_letter_surrounding_whitespace_in_a_token_is_tolerated(tmp_path):
    doc = Document()
    doc.add_paragraph("  {{SALUTATION}}  ")
    doc.add_paragraph("{{BODY}}")
    t = tmp_path / "t.docx"
    doc.save(str(t))
    out = tmp_path / "cl.docx"
    render_cover_letter(_content(), t, out)
    assert "Dear Hiring Manager," in _texts(out)


def test_cover_letter_token_with_other_text_is_not_a_placeholder(tmp_path):
    """Only a paragraph whose ENTIRE text is the token counts — otherwise the
    renderer would mangle prose that happens to mention a token."""
    t = _make_cl_template(tmp_path / "t.docx", static_text=("Use {{DATE}} in your template.",))
    out = tmp_path / "cl.docx"
    render_cover_letter(_content(date="2026-06-06"), t, out)
    assert _texts(out)[0] == "Use {{DATE}} in your template."


# ---------- placeholders Word nests: hyperlinks and table cells ----------


def test_cover_letter_placeholder_in_hyperlink_loses_the_link(tmp_path):
    """A {{SALUTATION}} the user pasted as a link was detected and filled, but
    the w:hyperlink wrapper survived — so the letter shipped its salutation as a
    live link to whatever URL the template pointed at."""
    from docx.oxml.ns import qn

    doc = Document()
    p = doc.add_paragraph()
    link = p._p.makeelement(qn("w:hyperlink"), {})
    run = p.add_run("{{SALUTATION}}")._r
    p._p.remove(run)
    link.append(run)
    p._p.append(link)
    doc.add_paragraph("{{BODY}}")
    t = tmp_path / "hyper.docx"
    doc.save(str(t))

    assert list_cover_letter_placeholders(t) == {"{{SALUTATION}}", "{{BODY}}"}

    out = tmp_path / "cl.docx"
    render_cover_letter(_content(), t, out)
    rendered = Document(str(out))
    assert "Dear Hiring Manager," in _texts(out)
    assert not any(p._p.findall(qn("w:hyperlink")) for p in rendered.paragraphs)


def test_cover_letter_placeholders_inside_a_table_cell_are_filled(tmp_path):
    """Many Word letter designs lay the page out in an invisible table.
    doc.paragraphs never enters a cell, so those placeholders were invisible and
    the render failed claiming a required token was missing."""
    doc = Document()
    table = doc.add_table(rows=2, cols=1)
    table.cell(0, 0).paragraphs[0].text = "{{SALUTATION}}"
    table.cell(1, 0).paragraphs[0].text = "{{BODY}}"
    t = tmp_path / "tbl.docx"
    doc.save(str(t))

    assert list_cover_letter_placeholders(t) == {"{{SALUTATION}}", "{{BODY}}"}

    out = tmp_path / "cl.docx"
    render_cover_letter(_content(), t, out)
    cell_texts = [
        p.text
        for row in Document(str(out)).tables[0].rows
        for cell in row.cells
        for p in cell.paragraphs
    ]
    assert cell_texts == ["Dear Hiring Manager,", "First para.", "Second para."]


def test_cover_letter_placeholder_in_a_merged_cell_is_found_once(tmp_path):
    """python-docx returns a merged cell once per column it spans; counting it
    twice raised a false 'more than one paragraph' error."""
    doc = Document()
    table = doc.add_table(rows=2, cols=2)
    merged = table.cell(0, 0).merge(table.cell(0, 1))
    merged.paragraphs[0].text = "{{SALUTATION}}"
    table.cell(1, 0).paragraphs[0].text = "{{BODY}}"
    t = tmp_path / "merged.docx"
    doc.save(str(t))
    render_cover_letter(_content(), t, tmp_path / "cl.docx")


def test_cover_letter_fills_a_placeholder_in_the_header(tmp_path):
    t = _make_cl_template(tmp_path / "t.docx")
    doc = Document(str(t))
    doc.sections[0].header.is_linked_to_previous = False
    doc.sections[0].header.paragraphs[0].text = "{{DATE}}"
    doc.save(str(t))
    assert "{{DATE}}" in list_cover_letter_placeholders(t)
    out = tmp_path / "cl.docx"
    render_cover_letter(_content(date="June 1, 2026"), t, out)
    assert Document(str(out)).sections[0].header.paragraphs[0].text == "June 1, 2026"


def test_cover_letter_blanks_tokens_in_file_properties(tmp_path):
    t = _make_cl_template(tmp_path / "t.docx")
    doc = Document(str(t))
    doc.core_properties.keywords = "{{SALUTATION}}"
    doc.save(str(t))
    out = tmp_path / "cl.docx"
    render_cover_letter(_content(), t, out)
    assert "{{" not in (Document(str(out)).core_properties.keywords or "")


@pytest.mark.parametrize(
    "over",
    [
        {"body": "A whole letter as one string."},
        {"body": ["Line one\nline two"]},
        {"body": ["Tab\tinside"]},
        {"salutation": "Dear\x0bX,"},
        {"date": 5},
    ],
)
def test_cover_letter_rejects_malformed_content(tmp_path, over):
    t = _make_cl_template(tmp_path / "t.docx")
    with pytest.raises(ValueError):
        render_cover_letter(_content(**over), t, tmp_path / "cl.docx")


# ---------- docx -> pdf ----------


def _fake_word(monkeypatch, tmp_path, *, returncode=0, write_pdf=True):
    from applyourself import docx_render

    monkeypatch.delenv("PDF_BROKER_DIR", raising=False)
    monkeypatch.setattr(docx_render.sys, "platform", "darwin")
    monkeypatch.setattr(docx_render, "WORD_APP", tmp_path)  # exists
    monkeypatch.setitem(paths._overrides, "PDF_STAGING", tmp_path / "staging")
    monkeypatch.setattr(docx_render, "pdf_pages", lambda p: 1)
    calls = []

    def run(cmd, input, **kw):
        calls.append(input)
        if write_pdf and 'file name "' in input:
            staged = input.split('file name "')[1].split('"')[0]
            Path(staged).write_bytes(b"%PDF-1.4 fake")
        import subprocess

        return subprocess.CompletedProcess(cmd, returncode, "", "boom" if returncode else "")

    monkeypatch.setattr(docx_render.subprocess, "run", run)
    return calls


def test_export_pdf_ok(monkeypatch, tmp_path):
    from applyourself.docx_render import export_pdf

    _fake_word(monkeypatch, tmp_path)
    docx = tmp_path / "A_Resume.docx"
    docx.write_bytes(b"docx")
    r = export_pdf(docx, "role1")
    assert r["status"] == "ok" and r["pages"] == 1
    assert (tmp_path / "A_Resume.pdf").is_file()
    assert [p.name for p in (tmp_path / "staging").iterdir()] == [".word.lock"]


def test_export_pdf_failure_never_leaves_a_stale_pdf(monkeypatch, tmp_path):
    """A failed re-run used to leave the previous pdf beside the new docx, and
    a leftover staging pdf passed the non-empty check."""
    from applyourself.docx_render import export_pdf

    _fake_word(monkeypatch, tmp_path, returncode=1)
    docx = tmp_path / "A_Resume.docx"
    docx.write_bytes(b"docx")
    (tmp_path / "A_Resume.pdf").write_bytes(b"old")
    r = export_pdf(docx, "role1")
    assert r["status"] == "failed" and "boom" in r["detail"]
    assert not (tmp_path / "A_Resume.pdf").exists()
    assert [p.name for p in (tmp_path / "staging").iterdir()] == [".word.lock"]


def test_export_pdf_stages_under_a_plain_name(monkeypatch, tmp_path):
    from applyourself.docx_render import export_pdf

    calls = _fake_word(monkeypatch, tmp_path)
    docx = tmp_path / "A_Resume.docx"
    docx.write_bytes(b"docx")
    assert export_pdf(docx, "2026_ai-engineer-göteborg_c52d7f9e")["status"] == "ok"
    assert 'is "2026_ai-engineer-g_teborg_c52d7f9e_A_Resume.docx"' in calls[0]
    # The reference comes from the open, never from a lookup by name (-1728).
    assert "set theDoc to active document" in calls[0]
    assert "set theDoc to document " not in calls[0]


def test_a_failed_conversion_restarts_word_once_and_retries(monkeypatch, tmp_path):
    """Word stuck in a bad state failed every role of a night with -1728."""
    import subprocess

    from applyourself import docx_render

    _fake_word(monkeypatch, tmp_path)
    outcomes = iter([1, 0, 0, 1])  # convert fails, restart, retry ok; next role fails
    calls = []

    def run(cmd, input, **kw):
        calls.append("restart" if "quit saving no" in input else "convert")
        rc = next(outcomes)
        if rc == 0 and 'file name "' in input:
            Path(input.split('file name "')[1].split('"')[0]).write_bytes(b"%PDF-1.4 fake")
        return subprocess.CompletedProcess(cmd, rc, "", "-1728" if rc else "")

    monkeypatch.setattr(docx_render.subprocess, "run", run)
    jobs = []
    for n in (1, 2):
        docx = tmp_path / f"R{n}" / "A_Resume.docx"
        docx.parent.mkdir()
        docx.write_bytes(b"docx")
        jobs.append((docx, f"role{n}"))
    results = docx_render.pdf_sweep(jobs)
    assert [r["status"] for r in results] == ["ok", "failed"]
    assert calls == ["convert", "restart", "convert", "convert"]


def test_export_pdf_skipped_without_word(monkeypatch, tmp_path):
    from applyourself import docx_render

    monkeypatch.setattr(docx_render, "WORD_APP", tmp_path / "missing.app")
    docx = tmp_path / "A_Resume.docx"
    docx.write_bytes(b"docx")
    assert docx_render.export_pdf(docx, "r")["status"] == "skipped"


def test_two_conversions_take_turns_at_word(monkeypatch, tmp_path):
    """Parallel /tailor runs share one Word; overlapping conversions failed with
    -1728 "object does not exist"."""
    import subprocess
    import threading
    import time

    from applyourself import docx_render

    monkeypatch.delenv("PDF_BROKER_DIR", raising=False)
    monkeypatch.setitem(paths._overrides, "PDF_STAGING", tmp_path / "staging")
    monkeypatch.setattr(docx_render, "WORD_APP", tmp_path)
    monkeypatch.setattr(docx_render.sys, "platform", "darwin")
    monkeypatch.setattr(docx_render, "pdf_pages", lambda p: 1)
    inside, overlaps = [0], []

    def run(cmd, input, **kw):
        inside[0] += 1
        overlaps.append(inside[0])
        time.sleep(0.05)
        staged = input.split('file name "')[1].split('"')[0]
        Path(staged).write_bytes(b"%PDF-1.4 fake")
        inside[0] -= 1
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(docx_render.subprocess, "run", run)
    results = []

    def go(n):
        docx = tmp_path / f"R{n}" / "A_Resume.docx"
        docx.parent.mkdir()
        docx.write_bytes(b"docx")
        results.append(docx_render.export_pdf(docx, f"role{n}")["status"])

    threads = [threading.Thread(target=go, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == ["ok"] * 4 and max(overlaps) == 1


def test_the_word_script_closes_an_open_namesake_first_and_always_closes_its_own(
    monkeypatch, tmp_path
):
    from applyourself.docx_render import export_pdf

    calls = _fake_word(monkeypatch, tmp_path)
    docx = tmp_path / "A_Resume.docx"
    docx.write_bytes(b"docx")
    export_pdf(docx, "role1")
    script = calls[0]
    assert script.index("repeat with i from (count of documents)") < script.index("open POSIX file")
    assert "on error errMsg number errNum" in script
    assert script.count("close theDoc saving no") == 2


# ---------- the night's pdf helper ----------


def _helper(monkeypatch, tmp_path):
    """A helper serving tmp_path/q from a thread, Word faked; returns (queue, stop)."""
    import os
    import threading

    from applyourself import pdf_broker

    _fake_word(monkeypatch, tmp_path)
    monkeypatch.setattr(pdf_broker, "POLL_SECONDS", 0.01)
    queue = tmp_path / "q"
    stop = threading.Event()
    monkeypatch.setattr(pdf_broker, "_alive", lambda pid: pid == os.getpid() and not stop.is_set())
    t = threading.Thread(target=pdf_broker.serve, args=(queue, os.getpid()), daemon=True)
    t.start()
    while not (queue / pdf_broker.PID_FILE).is_file():
        pass
    monkeypatch.setenv("PDF_BROKER_DIR", str(queue))
    return queue, stop, t


def test_export_pdf_goes_through_the_helper_when_one_is_up(monkeypatch, tmp_path):
    from applyourself import docx_render

    queue, stop, t = _helper(monkeypatch, tmp_path)
    docx = tmp_path / "A_Resume.docx"
    docx.write_bytes(b"docx")
    r = docx_render.export_pdf(docx, "role1")
    stop.set()
    t.join()
    assert r["status"] == "ok" and r["pages"] == 1
    assert (tmp_path / "A_Resume.pdf").is_file()
    assert sorted(p.name for p in queue.iterdir()) == ["broker.pid"]


def test_a_helper_that_is_gone_fails_fast_and_leaves_no_stale_pdf(monkeypatch, tmp_path):
    from applyourself import docx_render

    queue = tmp_path / "q"
    queue.mkdir()
    (queue / "broker.pid").write_text("999999999", encoding="utf-8")
    monkeypatch.setenv("PDF_BROKER_DIR", str(queue))
    docx = tmp_path / "A_Resume.docx"
    docx.write_bytes(b"docx")
    (tmp_path / "A_Resume.pdf").write_bytes(b"old")
    r = docx_render.export_pdf(docx, "role1")
    assert r["status"] == "failed" and "not running" in r["detail"]
    assert not (tmp_path / "A_Resume.pdf").exists()
    assert not list(queue.glob("*.req"))


def test_a_helper_that_never_answers_times_out_and_withdraws(monkeypatch, tmp_path):
    import os

    from applyourself import pdf_broker

    monkeypatch.setattr(pdf_broker, "POLL_SECONDS", 0.01)
    queue = tmp_path / "q"
    queue.mkdir()
    (queue / "broker.pid").write_text(str(os.getpid()), encoding="utf-8")  # alive, never serves
    docx = tmp_path / "A_Resume.docx"
    docx.write_bytes(b"docx")
    r = pdf_broker.request(queue, docx, "role1", wait=0.05)
    assert r["status"] == "failed" and "no result" in r["detail"]
    assert not list(queue.glob("*.req"))


def test_a_bad_request_gets_a_failed_result_and_the_helper_goes_on(monkeypatch, tmp_path):
    from applyourself import docx_render

    queue, stop, t = _helper(monkeypatch, tmp_path)
    (queue / "bad.req").write_text("not json", encoding="utf-8")
    docx = tmp_path / "A_Resume.docx"
    docx.write_bytes(b"docx")
    r = docx_render.export_pdf(docx, "role1")
    stop.set()
    t.join()
    assert r["status"] == "ok"
    import json

    assert json.loads((queue / "bad.res").read_text(encoding="utf-8"))["status"] == "failed"
