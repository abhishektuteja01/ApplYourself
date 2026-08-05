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

from src import verticals

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_CONFIG = REPO_ROOT / "profile" / "verticals.yaml"


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
