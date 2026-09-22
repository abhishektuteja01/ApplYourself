"""Opt-in coverage of the real profile/verticals.yaml.

The rest of the suite runs against the synthetic tests/fixtures/verticals.yaml,
so a rule that exists only in the live config is untested by construction —
the failure mode that silently dropped a whole search term's rows. Everything
here skips on a fresh clone, where the gitignored config is absent.

Assertions are structural only. Never pin a real search term, exclude term,
or skill weight in this file: it is committed.
"""

import re
from pathlib import Path

import pytest

from src import verticals

REPO_ROOT = Path(__file__).resolve().parent.parent
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


@pytest.fixture
def real_skills_master():
    if not REAL_SKILLS_MASTER.is_file():
        pytest.skip("profile/skills_master.md is gitignored user data")
    text = REAL_SKILLS_MASTER.read_text(encoding="utf-8")
    leans_by_id: dict[str, set[str]] = {}
    for block in text.split("\n## ")[1:]:
        skill_id, _, body = block.partition("\n")
        match = re.search(r"vertical_lean:\s*\[(.*?)\]", body)
        leans_by_id[skill_id.strip()] = (
            {v.strip() for v in match.group(1).split(",") if v.strip()} if match else set()
        )
    return leans_by_id


def test_real_config_loads_under_the_strict_loader(real_cfg):
    assert real_cfg.names
    assert real_cfg.default_vertical in real_cfg.verticals


def test_every_search_term_classifies_into_its_own_lane(real_cfg):
    """discovery.py tags a row by the search term that found it; cleaning.py
    falls back to the title classifier. When they disagree, a manual clip and
    a scraped row for the same title land in different rubrics."""
    from src.discovery.cleaning import classify_vertical_from_title

    for vertical in real_cfg.verticals.values():
        for term in vertical.search_terms + vertical.linkedin_terms:
            got = classify_vertical_from_title(term)
            assert got == vertical.name, (
                f"{vertical.name} search term classifies as {got!r}"
            )


def test_every_vertical_has_its_prose_and_resume_on_disk(real_cfg):
    """Same check `verticals-check` runs, so a half-onboarded lane fails here
    rather than mid-/score."""
    for name, v in real_cfg.verticals.items():
        for fname in ("rubric.md", "tailoring.md"):
            assert (REPO_ROOT / "profile" / "verticals" / name / fname).is_file(), (
                f"{name}/{fname} missing"
            )
        assert (REPO_ROOT / v.resume_file).is_file(), f"{name} resume_file missing"


def test_scored_by_stamps_are_unique_per_vertical(real_cfg):
    """The stamp lands in scored.parquet.scored_by_model; a shared one makes
    auto-skips unattributable."""
    stamps = [v.disqualifier_scored_by for v in real_cfg.verticals.values()]
    assert all(stamps) and len(set(stamps)) == len(stamps)


def test_title_gate_terms_do_not_exclude_the_lanes_own_search_terms(real_cfg):
    """An exclude term that also matches a search term drops every row that
    term finds — invisible without a title-by-title read of the drop log."""
    import re

    for vertical in real_cfg.verticals.values():
        if not vertical.title_exclude_terms:
            continue
        exclude_rx = re.compile(
            "|".join(rf"\b{re.escape(t)}\b" for t in vertical.title_exclude_terms),
            re.IGNORECASE,
        )
        for term in vertical.search_terms:
            if vertical.title_strong_keep_terms and re.search(
                "|".join(rf"\b{re.escape(t)}\b"
                         for t in vertical.title_strong_keep_terms),
                term, re.IGNORECASE,
            ):
                continue
            assert not exclude_rx.search(term), (
                f"{vertical.name}: a search term trips its own title_exclude_terms"
            )


def test_disqualifier_title_phrases_do_not_match_the_lanes_own_search_terms(real_cfg):
    """A title disqualifier that fires on the lane's own search term auto-skips
    every row that term finds, before any judge sees it."""
    for vertical in real_cfg.verticals.values():
        phrases = vertical.disqualifier_title_phrases or ()
        for phrase in phrases:
            for term in vertical.search_terms:
                assert phrase.lower() not in term.lower(), (
                    f"{vertical.name}: title disqualifier {phrase!r} matches its "
                    f"own search term {term!r}"
                )


def test_skill_vertical_lean_tags_are_backed_by_the_layout(real_cfg, real_skills_master):
    """/tailor's ranking rule (tailor.md 'Skills section' step) treats
    vertical_lean as a rank tiebreaker inside a layout line's already-eligible
    SKILL-ID set, never as the gate on whether the skill appears at all — the
    layout line in tailoring.md is the only thing that gates inclusion. A
    vertical_lean tag naming a vertical whose tailoring.md never lists that
    SKILL-ID anywhere is dead metadata: it reads as coverage but renders on
    no resume."""
    layout_ids_by_vertical: dict[str, set[str]] = {}
    for name in real_cfg.names:
        layout_path = REPO_ROOT / "profile" / "verticals" / name / "tailoring.md"
        if not layout_path.is_file():
            continue
        layout_ids_by_vertical[name] = set(
            re.findall(r"SKILL-[A-Z0-9-]+", layout_path.read_text(encoding="utf-8"))
        )

    violations = sorted(
        f"{skill_id} -> {vertical}"
        for skill_id, leans in real_skills_master.items()
        for vertical in leans
        if vertical in layout_ids_by_vertical
        and skill_id not in layout_ids_by_vertical[vertical]
    )
    assert not violations, (
        "vertical_lean claims a vertical whose tailoring.md layout never lists "
        f"the skill (fix by dropping the tag or adding the skill to that "
        f"vertical's layout line): {violations}"
    )
