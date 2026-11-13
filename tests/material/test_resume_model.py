"""Tests for src/applyourself/resume_model.py — source checks and resume assembly.

Everything runs against the committed example world (profile/*.example.*),
copied into tmp_path when a test needs to break it."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from applyourself import resume_model
from applyourself.shared import paths, verticals

PROFILE = paths.PROFILE


@pytest.fixture
def world(tmp_path, monkeypatch):
    """The example sources in tmp_path, with the example lanes configured."""
    for name, target in (
        ("resume.example.yaml", "resume.yaml"),
        ("bullets.example.md", "bullets.md"),
        ("skills_master.example.md", "skills_master.md"),
    ):
        shutil.copy(PROFILE / name, tmp_path / target)
    monkeypatch.setitem(paths._overrides, "RESUME", tmp_path / "resume.yaml")
    monkeypatch.setitem(paths._overrides, "BULLETS", tmp_path / "bullets.md")
    monkeypatch.setitem(paths._overrides, "SKILLS", tmp_path / "skills_master.md")
    from tests.conftest import FIXTURE_PATH

    cfg = verticals.load_verticals(FIXTURE_PATH)
    verticals.set_config(cfg)
    yield tmp_path, cfg
    verticals.set_config(None)


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _plan(src, lane, **over):
    plan = resume_model.baseline_plan(src, lane)
    plan.update(over)
    return plan


def _codes(built):
    return {p.code for p in built.problems}


# ---------- source checks ----------


def test_deleting_a_skill_needs_no_other_edit(world):
    tmp, cfg = world
    text = (tmp / "skills_master.md").read_text(encoding="utf-8")
    start = text.index("## SKILL-DOCKER")
    end = text.index("\n## ", start + 1)
    (tmp / "skills_master.md").write_text(text[:start] + text[end + 1 :], encoding="utf-8")
    assert resume_model.check_sources(resume_model.load_sources(), cfg) == []


def test_deleting_a_bullet_names_every_pointer_to_fix(world):
    tmp, cfg = world
    text = (tmp / "bullets.md").read_text(encoding="utf-8")
    start = text.index("## B-WID-03")
    end = text.index("\n## ", start + 1)
    (tmp / "bullets.md").write_text(text[:start] + text[end + 1 :], encoding="utf-8")
    problems = resume_model.check_sources(resume_model.load_sources(), cfg)
    assert any("B-WID-03 is not in bullets.md" in p for p in problems)


@pytest.mark.parametrize(
    "file,old,new,expect",
    [
        ("bullets.md", "## B-WID-01", "## B-ZZZ-01", "no context 'ZZZ'"),
        (
            "bullets.md",
            "default_for: [example_primary]",
            "default_for: [nope]",
            "'nope' is not a configured lane",
        ),
        (
            "skills_master.md",
            'show: {example_secondary: "Programming"}',
            'show: {example_secondary: "Nope"}',
            "'Nope' is not one of",
        ),
        (
            "resume.yaml",
            "lanes: {example_primary: {min: 2",
            "lanes: {example_primary: {min: 9",
            "0 <= min <= max",
        ),
        ("resume.yaml", "section: work", "section: jobs", "must be work or projects"),
    ],
)
def test_broken_pointer_is_named(world, file, old, new, expect):
    tmp, cfg = world
    _edit(tmp / file, old, new)
    problems = resume_model.check_sources(resume_model.load_sources(), cfg)
    assert any(expect in p for p in problems), problems


def test_a_skill_name_containing_another_shown_skill_is_flagged(world):
    """SKILL-PYTHON named 'Python (…, scikit-learn, …)' beside SKILL-SKLEARN
    printed scikit-learn twice on 206 resumes."""
    tmp, cfg = world
    _edit(tmp / "skills_master.md", "name: SQL", "name: SQL (Postgres, R)")
    _edit(
        tmp / "skills_master.md",
        "name: Retrieval-Augmented Generation",
        "name: Agentic Retrieval-Augmented Generation",
    )
    problems = resume_model.check_sources(resume_model.load_sources(), cfg)
    assert [p for p in problems if "show it twice" in p] == [
        "lane example_secondary: SKILL-SQL name 'SQL (Postgres, R)' lists SKILL-R ('R'); the resume would show it twice"
    ]


def test_ids_in_tailoring_prose_are_flagged(world, monkeypatch, tmp_path):
    lane_dir = tmp_path / "verticals" / "example_primary"
    lane_dir.mkdir(parents=True)
    (lane_dir / "tailoring.md").write_text("Prefer B-WID-02 and SKILL-SQL.", encoding="utf-8")
    monkeypatch.setitem(paths._overrides, "VERTICALS_DIR", tmp_path / "verticals")
    problems = resume_model.check_sources(resume_model.load_sources(), world[1])
    assert any("tailoring.md names ids" in p for p in problems)


def test_banned_phrase_in_a_source_is_flagged_once_at_the_source(world):
    tmp, cfg = world
    _edit(tmp / "bullets.md", "canonical: Owned", "canonical: Leveraged and owned")
    problems = resume_model.check_sources(resume_model.load_sources(), cfg)
    assert any("B-WID-01: banned phrase 'leveraged'" in p for p in problems)


# ---------- assembly ----------


def test_baseline_resume_copies_fixed_lines_from_the_source(world):
    src = resume_model.load_sources()
    md = resume_model.build(
        src, "example_primary", resume_model.baseline_plan(src, "example_primary")
    ).markdown
    assert (
        "**Widget Operations Analyst** - Widget Corp\tExampletown, ST | Jan 2023 - Jun 2025" in md
    )
    assert "**B.S. Industrial Engineering**\tSep 2019 - Dec 2022" in md
    # sections in the lane's order
    assert md.index("**WORK EXPERIENCE**") < md.index("**PROJECTS**") < md.index("**EDUCATION**")


def test_bullets_render_in_canonical_order_whatever_the_plan_order(world):
    src = resume_model.load_sources()
    plan = _plan(
        src,
        "example_primary",
        entries=[{"id": "B-WID-03"}, {"id": "B-WID-01"}, {"id": "B-WID-02"}, {"id": "B-SPR-01"}],
    )
    md = resume_model.build(src, "example_primary", plan).markdown
    lines = [ln for ln in md.split("\n") if ln.startswith("* ")]
    wid = [src.bullets[b]["canonical"][:20] for b in ("B-WID-01", "B-WID-02", "B-WID-03")]
    assert [ln[2:22] for ln in lines[:3]] == wid


@pytest.mark.parametrize(
    "entries,code",
    [
        ([{"id": "B-NOPE-01"}], "unknown_id"),
        ([{"id": "B-EDU-01"}], "context"),
        ([{"id": "B-WID-01"}], "budget"),
    ],
)
def test_plan_problems(world, entries, code):
    src = resume_model.load_sources()
    built = resume_model.build(
        src, "example_primary", _plan(src, "example_primary", entries=entries)
    )
    assert code in _codes(built)


def _reword(src, lane, bid, text):
    plan = resume_model.baseline_plan(src, lane)
    plan["entries"] = [
        {"id": e["id"], **({"text": text} if e["id"] == bid else {})} for e in plan["entries"]
    ]
    return plan


def test_rewording_with_licensed_words_passes(world):
    src = resume_model.load_sources()
    text = (
        "Owned the daily widget assembly throughput report for a national "
        "manufacturing client, reconciling line output against the production plan."
    )
    built = resume_model.build(
        src, "example_primary", _reword(src, "example_primary", "B-WID-01", text)
    )
    assert built.problems == []
    assert any("transformation=rephrase" in t for t in built.trace)


def test_rewording_with_a_new_word_or_number_is_refused(world):
    src = resume_model.load_sources()
    text = "Spearheaded the daily widget assembly throughput report across 9 plants."
    built = resume_model.build(
        src, "example_primary", _reword(src, "example_primary", "B-WID-01", text)
    )
    assert {"license", "number", "lint"} <= _codes(built)


def test_final_falls_back_to_the_canonical_bullet(world):
    src = resume_model.load_sources()
    plan = _reword(src, "example_primary", "B-WID-01", "Invented a brand new claim.")
    built = resume_model.build(src, "example_primary", plan, final=True)
    assert built.problems == []
    assert built.swapped == ["B-WID-01 -> canonical"]
    assert src.bullets["B-WID-01"]["canonical"] in built.markdown


def test_written_summary_numbers_and_skills_must_trace_to_cited_bullets(world):
    src = resume_model.load_sources()
    plan = _plan(
        src,
        "example_primary",
        summary={"text": "Analyst with 15 years of Docker work.", "uses": ["B-WID-01"]},
    )
    built = resume_model.build(src, "example_primary", plan)
    assert {"number", "skill"} <= _codes(built)
    ok = _plan(
        src,
        "example_primary",
        summary={
            "text": "Analyst who reconciles output across four plants with SQL.",
            "uses": ["B-WID-01", "B-WID-03"],
        },
    )
    assert resume_model.build(src, "example_primary", ok).problems == []


def test_skills_layout_and_alias_are_enforced(world):
    src = resume_model.load_sources()
    plan = _plan(
        src,
        "example_primary",
        skills={
            "Programming": ["SKILL-R"],  # not on this lane
            "Databases & Tools": [{"id": "SKILL-GIT", "alias": "GitHub"}],  # not a synonym
            "Nope": ["SKILL-SQL"],
        },
    )
    codes = _codes(resume_model.build(src, "example_primary", plan))
    assert {"layout", "skill"} <= codes


def test_a_skills_line_in_the_plan_reorders_and_keeps_the_rest(world):
    src = resume_model.load_sources()
    base = resume_model.shown_skills(src, "example_primary")
    label = next(lab for lab, ids in base.items() if len(ids) >= 2)
    ids = base[label]
    built = resume_model.build(
        src, "example_primary", _plan(src, "example_primary", skills={label: [ids[-1]]})
    )
    line = next(ln for ln in built.markdown.splitlines() if ln.lstrip("*- ").startswith(label))
    names = [src.skills[i]["name"] for i in [ids[-1]] + ids[:-1]]
    assert all(n in line for n in names)
    assert line.index(names[0]) < line.index(names[1])


@pytest.mark.parametrize(
    "picks, problem, shown",
    [
        ([{"id": "SKILL-GIT", "alias": "VERSION CONTROL"}], None, "VERSION CONTROL"),
        (["SKILL-GIT", {"id": "SKILL-GIT", "alias": "Git"}], "SKILL-GIT is listed twice", None),
        ([{"id": "SKILL-DOCKER", "alias": "postgresql"}, "SKILL-POSTGRES"], "shown twice", None),
        ([{"id": "SKILL-DOCKER", "alias": "postgresql"}], None, "postgresql"),
    ],
)
def test_skill_picks_show_each_skill_and_text_once(world, picks, problem, shown):
    tmp, _ = world
    _edit(tmp / "skills_master.md", '["containers", ', '["containers", "PostgreSQL", ')
    src = resume_model.load_sources()
    plan = _plan(src, "example_primary", skills={"Databases & Tools": picks})
    built = resume_model.build(src, "example_primary", plan)
    if problem:
        assert any(problem in p.detail for p in built.problems), built.problems
        return
    assert built.problems == []
    line = next(ln for ln in built.markdown.splitlines() if "Databases & Tools" in ln)
    assert shown in line and line.lower().count(shown.lower()) == 1


def test_a_summary_citing_a_bullet_left_off_the_resume_fails(world):
    src = resume_model.load_sources()
    summary = {"text": "Analyst who reconciles output across four plants.", "uses": ["B-SPR-02"]}
    plan = _plan(src, "example_primary", summary=summary)
    built = resume_model.build(src, "example_primary", plan)
    assert [p.detail for p in built.problems] == [
        "B-SPR-02 is cited by the summary but not on the resume"
    ]
    final = resume_model.build(src, "example_primary", plan, final=True)
    assert final.problems == [] and final.swapped == ["summary -> lane baseline"]


def test_a_role_location_replaces_only_the_marked_contact_line(world):
    tmp, _ = world
    src = resume_model.load_sources()
    plan = resume_model.baseline_plan(src, "example_primary")
    built = resume_model.build(src, "example_primary", plan, location="Exampleburg, EX")
    contact = built.markdown.splitlines()[2]
    assert contact.startswith("Exampleburg, EX | ") and "Exampletown" not in contact
    _edit(tmp / "resume.yaml", "    location: true\n", "")
    unmarked = resume_model.build(
        resume_model.load_sources(), "example_primary", plan, location="Exampleburg, EX"
    )
    assert {p.code for p in unmarked.problems} == {"layout"}


def test_company_name_is_never_linted(world):
    src = resume_model.load_sources()
    plan = _plan(
        src,
        "example_primary",
        summary={"text": "Analyst who reconciles output for Robust Widgets.", "uses": ["B-WID-01"]},
    )
    built = resume_model.build(src, "example_primary", plan, company="Robust Widgets")
    assert "lint" not in _codes(built)


def test_keyword_coverage_is_traced(world):
    src = resume_model.load_sources()
    built = resume_model.build(
        src,
        "example_primary",
        resume_model.baseline_plan(src, "example_primary"),
        keywords=("SQL", "Kubernetes", "SQLs"),
    )
    assert "- Kubernetes: not landed" in built.trace
    assert any(t.startswith("- SQL: L") for t in built.trace)
    assert any(t.startswith("- SQLs: L") for t in built.trace)


@pytest.mark.parametrize(
    "line, keyword",
    [
        ("Built generative AI apps", "AI"),
        ("Trained machine learning models", "learning"),
        ("Deployed on Google Cloud", "Cloud"),
        ("Scheduled GPUs", "gpus"),
        ("Tracked KPIs", "KPIs"),
        ("Sold SaaS", "SAAS"),
        ("Ran KUBERNETES", "Kubernetes"),
    ],
)
def test_a_keyword_as_written_in_any_case_always_lands(line, keyword):
    """Alias folding may swallow a word ("AI" in "generative AI"): the
    as-written match, in any case, still counts it."""
    assert resume_model._keyword_trace(line, (keyword,))[-1] == f"- {keyword}: L1"


def test_a_summary_that_looks_like_a_header_stays_a_body_paragraph(world):
    """The renderer once guessed block types from markdown, so a summary opening
    in bold became a job header."""
    src = resume_model.load_sources()
    text = "**Widget** reporting owner."
    built = resume_model.build(
        src, "example_primary", _plan(src, "example_primary", summary={"text": text})
    )
    assert built.problems == []
    (block,) = [b for b in built.blocks if b.line == text]
    assert block.kind == "body" and block.runs[0] == resume_model.Run("Widget", bold=True)


def test_baseline_orders_contexts_by_the_lane_order(world):
    tmp, _ = world
    _edit(
        tmp / "resume.yaml",
        "example_secondary: {min: 1, max: 2, order: 1},",
        "example_secondary: {min: 1, max: 2, order: 5},",
    )
    _edit(
        tmp / "resume.yaml",
        "example_secondary: {min: 1, max: 2}, example_tertiary: {min: 1, max: 2}}\n  EDU",
        "example_secondary: {min: 1, max: 2, order: 1}, example_tertiary: {min: 1, max: 2}}\n  EDU",
    )
    src = resume_model.load_sources()
    md = resume_model.build(
        src, "example_secondary", resume_model.baseline_plan(src, "example_secondary")
    ).markdown
    assert md.index("**Cog Assistant**") < md.index("**Sprocket Validator**")


def test_an_old_or_unknown_entry_key_is_named(world):
    tmp, cfg = world
    _edit(tmp / "bullets.md", "canonical: Owned", "source: Widget Corp\ncanonical: Owned")
    _edit(tmp / "skills_master.md", "name: SQL", "name: SQL\nvertical_lean: [example_primary]")
    problems = resume_model.check_sources(resume_model.load_sources(), cfg)
    assert any("B-WID-01: unknown key 'source'" in p for p in problems)
    assert any("SKILL-SQL: unknown key 'vertical_lean'" in p for p in problems)


@pytest.mark.parametrize(
    "text, error",
    [
        ("## B-X-01\ncanonical: a\n\n## B-X-01\ncanonical: b\n", "f.md:4: duplicate id 'B-X-01'"),
        ("## B-X-01\ncanonical: a long claim\n  that wraps\n", "f.md:3: stray line"),
        ("## B-X-01\ntags: [a, b\n", "f.md:2: bad inline YAML in 'tags'"),
    ],
)
def test_parse_entries_names_the_line_of_a_hidden_mistake(text, error):
    with pytest.raises(resume_model.SourceError, match=re.escape(error)):
        resume_model.parse_entries(text, "f.md")
