"""title_gate.py: which lanes the title gate applies to."""

from __future__ import annotations

from src.shared import title_gate

# ---------- title gate ----------


def test_gate_exempts_manual_and_unknown_lanes(cfg):
    assert title_gate.title_passes("Director of Anything", "no_such_lane")
    assert title_gate.title_passes("Senior Director", cfg.default_vertical, source="manual")
