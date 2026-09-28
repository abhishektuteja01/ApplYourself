"""Tests for src/resume_model.py — source checks and resume assembly.

Everything runs against the committed example world (profile/*.example.*),
copied into tmp_path when a test needs to break it."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src import paths, resume_model, verticals

PROFILE = paths.PROFILE


@pytest.fixture
def world(tmp_path, monkeypatch):
    """The example sources in tmp_path, with the example lanes configured."""
    for name, target in (("resume.example.yaml", "resume.yaml"),
                         ("bullets.example.md", "bullets.md"),
                         ("skills_master.example.md", "skills_master.md")):
        shutil.copy(PROFILE / name, tmp_path / target)
    monkeypatch.setattr(resume_model, "RESUME", tmp_path / "resume.yaml")
    monkeypatch.setattr(resume_model, "BULLETS", tmp_path / "bullets.md")
    monkeypatch.setattr(resume_model, "SKILLS", tmp_path / "skills_master.md")
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
    (tmp / "skills_master.md").write_text(text[:start] + text[end + 1:], encoding="utf-8")
    assert resume_model.check_sources(resume_model.load_sources(), cfg) == []


def test_deleting_a_bullet_names_every_pointer_to_fix(world):
    tmp, cfg = world
    text = (tmp / "bullets.md").read_text(encoding="utf-8")
    start = text.index("## B-WID-03")
    end = text.index("\n## ", start + 1)
    (tmp / "bullets.md").write_text(text[:start] + text[end + 1:], encoding="utf-8")
    problems = resume_model.check_sources(resume_model.load_sources(), cfg)
    assert any("B-WID-03 is not in bullets.md" in p for p in problems)


@pytest.mark.parametrize("file,old,new,expect", [
    ("bullets.md", "## B-WID-01", "## B-ZZZ-01", "no context 'ZZZ'"),
    ("bullets.md", "default_for: [example_primary]", "default_for: [nope]", "'nope' is not a configured lane"),
    ("skills_master.md", 'show: {example_secondary: "Programming"}', 'show: {example_secondary: "Nope"}', "'Nope' is not one of"),
    ("resume.yaml", "lanes: {example_primary: {min: 2", "lanes: {example_primary: {min: 9", "0 <= min <= max"),
    ("resume.yaml", "section: work", "section: jobs", "must be work or projects"),
])
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
    _edit(tmp / "skills_master.md", "name: Retrieval-Augmented Generation", "name: Agentic Retrieval-Augmented Generation")
    problems = resume_model.check_sources(resume_model.load_sources(), cfg)
    assert [p for p in problems if "show it twice" in p] == [
        "lane example_secondary: SKILL-SQL name 'SQL (Postgres, R)' lists SKILL-R ('R'); the resume would show it twice"]


def test_ids_in_tailoring_prose_are_flagged(world, monkeypatch, tmp_path):
    lane_dir = tmp_path / "verticals" / "example_primary"
    lane_dir.mkdir(parents=True)
    (lane_dir / "tailoring.md").write_text("Prefer B-WID-02 and SKILL-SQL.", encoding="utf-8")
    monkeypatch.setattr(verticals, "VERTICALS_DIR", tmp_path / "verticals")
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
    md = resume_model.build(src, "example_primary", resume_model.baseline_plan(src, "example_primary")).markdown
    assert "**Widget Operations Analyst** - Widget Corp\tExampletown, ST | Jan 2023 - Jun 2025" in md
    assert "**B.S. Industrial Engineering**\tSep 2019 - Dec 2022" in md
    # sections in the lane's order
    assert md.index("**WORK EXPERIENCE**") < md.index("**PROJECTS**") < md.index("**EDUCATION**")


def test_bullets_render_in_canonical_order_whatever_the_plan_order(world):
    src = resume_model.load_sources()
    plan = _plan(src, "example_primary", entries=[{"id": "B-WID-03"}, {"id": "B-WID-01"},
                                                  {"id": "B-WID-02"}, {"id": "B-SPR-01"}])
    md = resume_model.build(src, "example_primary", plan).markdown
    lines = [l for l in md.split("\n") if l.startswith("* ")]
    wid = [src.bullets[b]["canonical"][:20] for b in ("B-WID-01", "B-WID-02", "B-WID-03")]
    assert [l[2:22] for l in lines[:3]] == wid


@pytest.mark.parametrize("entries,code", [
    ([{"id": "B-NOPE-01"}], "unknown_id"),
    ([{"id": "B-EDU-01"}], "context"),
    ([{"id": "B-WID-01"}], "budget"),
])
def test_plan_problems(world, entries, code):
    src = resume_model.load_sources()
    built = resume_model.build(src, "example_primary", _plan(src, "example_primary", entries=entries))
    assert code in _codes(built)


def _reword(src, lane, bid, text):
    plan = resume_model.baseline_plan(src, lane)
    plan["entries"] = [{"id": e["id"], **({"text": text} if e["id"] == bid else {})}
                       for e in plan["entries"]]
    return plan


def test_rewording_with_licensed_words_passes(world):
    src = resume_model.load_sources()
    text = ("Owned the daily widget assembly throughput report for a national "
            "manufacturing client, reconciling line output against the production plan.")
    built = resume_model.build(src, "example_primary", _reword(src, "example_primary", "B-WID-01", text))
    assert built.problems == []
    assert any("transformation=rephrase" in t for t in built.trace)


def test_rewording_with_a_new_word_or_number_is_refused(world):
    src = resume_model.load_sources()
    text = ("Spearheaded the daily widget assembly throughput report across 9 plants.")
    built = resume_model.build(src, "example_primary", _reword(src, "example_primary", "B-WID-01", text))
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
    plan = _plan(src, "example_primary",
                 summary={"text": "Analyst with 15 years of Docker work.", "uses": ["B-WID-01"]})
    built = resume_model.build(src, "example_primary", plan)
    assert {"number", "skill"} <= _codes(built)
    ok = _plan(src, "example_primary",
               summary={"text": "Analyst who reconciles output across four plants with SQL.",
                        "uses": ["B-WID-01", "B-WID-03"]})
    assert resume_model.build(src, "example_primary", ok).problems == []


def test_skills_layout_and_alias_are_enforced(world):
    src = resume_model.load_sources()
    plan = _plan(src, "example_primary", skills={
        "Programming": ["SKILL-R"],                                  # not on this lane
        "Databases & Tools": [{"id": "SKILL-GIT", "alias": "GitHub"}],  # not a synonym
        "Nope": ["SKILL-SQL"],
    })
    codes = _codes(resume_model.build(src, "example_primary", plan))
    assert {"layout", "skill"} <= codes


def test_company_name_is_never_linted(world):
    src = resume_model.load_sources()
    plan = _plan(src, "example_primary",
                 summary={"text": "Analyst who reconciles output for Robust Widgets.", "uses": ["B-WID-01"]})
    built = resume_model.build(src, "example_primary", plan, company="Robust Widgets")
    assert "lint" not in _codes(built)


def test_keyword_coverage_is_traced(world):
    src = resume_model.load_sources()
    built = resume_model.build(src, "example_primary", resume_model.baseline_plan(src, "example_primary"),
                               keywords=("SQL", "Kubernetes"))
    assert "- Kubernetes: not landed" in built.trace
    assert any(t.startswith("- SQL: L") for t in built.trace)


def test_baseline_orders_contexts_by_the_lane_order(world):
    tmp, _ = world
    _edit(tmp / "resume.yaml", "example_secondary: {min: 1, max: 2, order: 1},",
          "example_secondary: {min: 1, max: 2, order: 5},")
    _edit(tmp / "resume.yaml", "example_secondary: {min: 1, max: 2}, example_tertiary: {min: 1, max: 2}}\n  EDU",
          "example_secondary: {min: 1, max: 2, order: 1}, example_tertiary: {min: 1, max: 2}}\n  EDU")
    src = resume_model.load_sources()
    md = resume_model.build(src, "example_secondary", resume_model.baseline_plan(src, "example_secondary")).markdown
    assert md.index("**Cog Assistant**") < md.index("**Sprocket Validator**")


def test_an_old_or_unknown_entry_key_is_named(world):
    tmp, cfg = world
    _edit(tmp / "bullets.md", "canonical: Owned", "source: Widget Corp\ncanonical: Owned")
    _edit(tmp / "skills_master.md", "name: SQL", "name: SQL\nvertical_lean: [example_primary]")
    problems = resume_model.check_sources(resume_model.load_sources(), cfg)
    assert any("B-WID-01: unknown key 'source'" in p for p in problems)
    assert any("SKILL-SQL: unknown key 'vertical_lean'" in p for p in problems)
