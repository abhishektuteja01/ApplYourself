"""title_gate.py: which lane a title goes to, and what the gate drops."""

from __future__ import annotations

import pytest

from applyourself.shared import title_gate


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
        # a legacy strong-keep term still beats a lane's own exclude term
        ("Sprocket Risk Management Operations", "example_secondary", "example_secondary", ""),
    ],
)
def test_lane_for(cfg, title, hint, lane, reason):
    got = title_gate.lane_for(title, hint, "linkedin")
    assert (got.lane, got.reason) == (lane, reason)
