"""Opt-in coverage of the real profile/verticals.yaml.

The rest of the suite runs against the synthetic tests/fixtures/verticals.yaml,
so a rule that exists only in the live config is untested by construction —
the failure mode that silently dropped a whole search term's rows. Everything
here skips on a fresh clone, where the gitignored config is absent.

Assertions are structural only. Never pin a real search term, exclude term,
or skill weight in this file: it is committed.
"""

from pathlib import Path

import pytest

from applyourself.shared import paths, verticals

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_CONFIG = REPO_ROOT / "profile" / "verticals.yaml"
REAL_SKILLS_MASTER = REPO_ROOT / "profile" / "skills_master.md"


@pytest.fixture
def real_cfg():
    if not REAL_CONFIG.is_file():
        pytest.skip("profile/verticals.yaml is gitignored user data")
    config = verticals.load_verticals(REAL_CONFIG)
    verticals.set_config(config)
    yield config
    verticals.set_config(None)


def test_real_config_loads_under_the_strict_loader(real_cfg):
    assert real_cfg.names
    assert real_cfg.default_vertical in real_cfg.verticals


def test_every_search_term_routes_a_board_title_to_its_own_lane(real_cfg):
    """A search row keeps its term's lane; a board row or a manual clip with
    the same title goes where lane_for puts it. When they disagree, the same
    title lands in different rubrics."""
    from applyourself.shared.title_gate import lane_for

    for vertical in real_cfg.verticals.values():
        for term in vertical.search_terms + vertical.linkedin_terms + vertical.abroad_terms:
            got = lane_for(term, "", "greenhouse", real_cfg).lane
            assert got == vertical.name, f"{vertical.name} search term routes to {got!r}"


def test_every_vertical_has_its_prose_and_resume_on_disk(real_cfg):
    """Same check `verticals-check` runs, so a half-onboarded lane fails here
    rather than mid-/score."""
    for name, v in real_cfg.verticals.items():
        for fname in ("rubric.md", "tailoring.md"):
            assert (REPO_ROOT / "profile" / "verticals" / name / fname).is_file(), (
                f"{name}/{fname} missing"
            )
        assert v.resume is not None, f"{name} has no resume: block"


def test_title_gate_passes_the_lanes_own_search_terms(real_cfg):
    """A ban word that also matches a search term drops every row that term
    finds — invisible without a title-by-title read of the drop log."""
    from applyourself.shared.title_gate import lane_for

    for vertical in real_cfg.verticals.values():
        for term in vertical.search_terms + vertical.linkedin_terms + vertical.abroad_terms:
            got = lane_for(term, vertical.name, "linkedin", real_cfg)
            assert (got.lane, got.reason) == (vertical.name, ""), (
                f"{vertical.name}: a search term does not pass its own lane ({got.reason})"
            )


def test_real_resume_sources_check_clean_and_every_baseline_builds(real_cfg, monkeypatch):
    """The same source check `verticals-check` runs, plus every lane's baseline
    resume (what /score's judge reads) assembling without a problem."""
    from applyourself import resume_model

    profile = REPO_ROOT / "profile"
    for attr, name in (
        ("RESUME", "resume.yaml"),
        ("BULLETS", "bullets.md"),
        ("SKILLS", "skills_master.md"),
    ):
        if not (profile / name).is_file():
            pytest.skip(f"profile/{name} is gitignored user data")
        monkeypatch.setitem(paths._overrides, attr, profile / name)
    src = resume_model.load_sources()
    assert resume_model.check_sources(src, real_cfg) == []
    for lane, v in real_cfg.verticals.items():
        if v.resume is not None:
            built = resume_model.build(src, lane, resume_model.baseline_plan(src, lane))
            assert built.problems == [], (lane, [p.as_dict() for p in built.problems])
