"""scripts/dev/backfill_future_refusal.py: the one-time fill of judge_facts."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from applyourself import score_store
from applyourself.score_filter import load_scoring
from applyourself.shared import paths

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev" / "backfill_future_refusal.py"


def _load():
    spec = importlib.util.spec_from_file_location("backfill_future_refusal", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_judged_posting_is_the_one_whose_jd_gives_the_stored_hash():
    """One job_id can hold postings that differ: one refusing sponsorship,
    the judged one offering it. The refusal of the other posting is not read."""
    mod = _load()
    cfg = load_scoring(paths.example("scoring.example.yaml"))
    refusing = ("Analyst", "Unable to sponsor now or in the future.")
    judged = ("Analyst", "Limited sponsorship may be available.")
    packet = "packet"
    stored = score_store.input_hash(packet, judged[1])
    assert mod.judged_posting([refusing, judged], stored, [packet], cfg) == judged
    assert mod.judged_posting([refusing, judged], "", [packet], cfg) == refusing
