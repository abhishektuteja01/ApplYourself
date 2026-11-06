"""title_gate.py: which lane a title goes to, and what the gate drops."""

from __future__ import annotations

import dataclasses

import pytest

from applyourself.shared import title_gate, title_terms, verticals


def test_gate_exempts_manual_and_unknown_lanes(cfg):
    assert title_gate.lane_for("Director of Anything", "no_such_lane").passes
    assert title_gate.lane_for("Distinguished Nobody", cfg.default_vertical, "manual").passes


@pytest.mark.parametrize(
    "title, hint, lane, reason",
    [
        # fits the search term's lane: kept there, though another lane's rule matches first
        ("Widget Cog Engineer", "example_tertiary", "example_tertiary", ""),
        # does not fit it: the classifier's lane
        ("Widget Platform Analyst", "example_tertiary", "example_primary", ""),
        # fits no lane: dropped, with the reason
        ("Bakery Clerk", "example_tertiary", "", "no_lane"),
        # a search term spelled another way still fits
        ("Applied Cog-Ingenieure", "", "example_tertiary", ""),
        # the defaults ban list drops in every lane
        (
            "Distinguished Cog Engineer",
            "example_tertiary",
            "example_tertiary",
            "banned: distinguished",
        ),
        # a legacy strong-keep term still beats a lane's own exclude term
        ("Sprocket Risk Management Operations", "example_secondary", "example_secondary", ""),
    ],
)
def test_lane_for(cfg, title, hint, lane, reason):
    got = title_gate.lane_for(title, hint, "linkedin")
    assert (got.lane, got.reason) == (lane, reason)


@pytest.mark.parametrize(
    "term, title, hit",
    [
        (".net developer", "ASP.NET Developer", True),
        ("forward deployed engineer", "VegaのForward Deployed Engineer", True),
        ("ml engineer", "HTML Engineer", False),
        ("ingénieur", "Ingénieur IA", True),  # decomposed accent, NFC on both sides
    ],
)
def test_terms_match_loosely_but_never_inside_a_latin_word(term, title, hit):
    rx = title_terms.compile_terms((term,))
    assert bool(rx and rx.search(title_terms.normalize(title))) is hit


def test_a_single_word_needs_its_lanes_context_except_on_its_own_search():
    rule = verticals.CompoundRule(("agent", "agents"), ("ai", "llm", "agents"))
    assert rule.matches("AI Agent Developer")
    assert not rule.matches("Insurance Agents")  # "agents" is the match itself
    assert rule.matches("Insurance Agents", hinted=True)
    assert verticals.CompoundRule("ML Engineer", ("LLM",)).matches("ML Engineer, LLMOps")


def test_a_keep_rule_beats_a_ban_word(cfg):
    keep = verticals.TermsRule(("cog platform",), keep=True)
    kept = dataclasses.replace(
        cfg, classifier_rules=((cfg.default_vertical, keep), *cfg.classifier_rules)
    )
    title = "Distinguished Cog Platform Engineer"
    assert title_gate.lane_for(title, "", "greenhouse", cfg).reason == "banned: distinguished"
    assert title_gate.lane_for(title, "", "greenhouse", kept).passes
