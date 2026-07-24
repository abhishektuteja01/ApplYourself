"""Tests for src/state_io.py — deterministic state.yaml plumbing."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import yaml

from src.state_io import (
    ACTIVE_STATES,
    CLOSED_STATES,
    TERMINAL_STATES,
    VALID_STATES,
    append_outreach_draft,
    append_tailored_dir,
    load_all_states,
    load_state,
    mark_outreach_sent,
    skip_count,
    state_path_for,
    transition,
)


def _initial(**overrides) -> dict:
    base = {
        "job_id": "aaaaaaaa", "company": "Acme", "title": "SAP SD",
        "source": "indeed", "url": "https://x", "location": "Remote",
        "vertical": "sap", "sponsorship_label": "opt_ok", "fit_score": 75,
    }
    base.update(overrides)
    return base


def _yaml_load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


# ---------- state-set constants ----------

def test_valid_states_count_and_terminal_subset():
    """11 states locked; 4 are terminal."""
    assert len(VALID_STATES) == 11
    assert TERMINAL_STATES <= VALID_STATES
    assert len(TERMINAL_STATES) == 4
    assert TERMINAL_STATES == {"offer", "rejected", "withdrawn", "ghosted"}
    assert ACTIVE_STATES.isdisjoint(TERMINAL_STATES)
    assert "skip" in CLOSED_STATES and "skip" not in TERMINAL_STATES


# ---------- transition: creation path ----------

def test_transition_creates_state_yaml_when_missing(tmp_path):
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    data = transition(p, "saved", initial_fields=_initial(),
                       now=datetime(2026, 6, 6, 10, 0))
    assert p.exists()
    assert data["state"] == "saved"
    assert data["state_history"] == [
        {"state": "saved", "at": "2026-06-06T10:00:00", "note": ""},
    ]
    assert data["tailored_dirs"] == []
    assert data["outreach"] == []
    assert data["applied_at"] is None
    assert data["company"] == "Acme"
    assert data["fit_score"] == 75
    assert data["vertical"] == "sap"


def test_transition_creation_defaults_vertical_when_absent(tmp_path):
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    fields = _initial()
    del fields["vertical"]
    data = transition(p, "saved", initial_fields=fields)
    assert data["vertical"] == ""


def test_transition_creation_requires_initial_fields(tmp_path):
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    with pytest.raises(ValueError, match="initial_fields"):
        transition(p, "saved")


def test_transition_creation_requires_core_initial_keys(tmp_path):
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    with pytest.raises(ValueError, match="missing required keys"):
        transition(p, "saved", initial_fields={"job_id": "aaaaaaaa"})


# ---------- transition: existing path ----------

def test_transition_appends_state_history(tmp_path):
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    transition(p, "saved", initial_fields=_initial(),
                now=datetime(2026, 6, 1, 10, 0))
    transition(p, "tailored", note="v1",
                now=datetime(2026, 6, 2, 10, 0))
    transition(p, "applied",
                now=datetime(2026, 6, 3, 10, 0))
    data = load_state(p)
    assert [h["state"] for h in data["state_history"]] == [
        "saved", "tailored", "applied",
    ]
    assert data["state"] == "applied"
    assert data["applied_at"] == "2026-06-03T10:00:00"
    assert data["last_touch"] == "2026-06-03T10:00:00"
    # Note carried on the tailored entry
    assert data["state_history"][1]["note"] == "v1"


def test_transition_rejects_unknown_state(tmp_path):
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    with pytest.raises(ValueError, match="unknown state"):
        transition(p, "lurking", initial_fields=_initial())


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES))
def test_transition_rejects_out_of_every_terminal_state(tmp_path, terminal):
    """R8 / §6.5: no transitions out of any of the 4 terminal states."""
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    transition(p, terminal, initial_fields=_initial())
    with pytest.raises(ValueError, match="cannot transition out of terminal"):
        transition(p, "saved")


def test_transition_applied_at_only_set_once(tmp_path):
    """applied_at should snapshot the FIRST time we hit applied, not be
    overwritten by later transitions through applied (shouldn't happen,
    but defensive)."""
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    transition(p, "applied", initial_fields=_initial(),
                now=datetime(2026, 6, 1, 10, 0))
    transition(p, "screen", now=datetime(2026, 6, 5, 10, 0))
    data = load_state(p)
    assert data["applied_at"] == "2026-06-01T10:00:00"


# ---------- tailored_dirs ----------

def test_append_tailored_dir(tmp_path):
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    transition(p, "saved", initial_fields=_initial())
    append_tailored_dir(p, "2026-06-06_acme_sap-sd_aaaaaaaa")
    data = load_state(p)
    assert data["tailored_dirs"] == ["2026-06-06_acme_sap-sd_aaaaaaaa"]


def test_append_tailored_dir_idempotent(tmp_path):
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    transition(p, "saved", initial_fields=_initial())
    append_tailored_dir(p, "d1")
    append_tailored_dir(p, "d1")   # same again — should NOT duplicate
    append_tailored_dir(p, "d2")
    data = load_state(p)
    assert data["tailored_dirs"] == ["d1", "d2"]


def test_append_tailored_dir_requires_existing_state(tmp_path):
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    with pytest.raises(ValueError, match="does not exist"):
        append_tailored_dir(p, "d1")


# ---------- outreach[] ----------

def test_append_outreach_draft(tmp_path):
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    transition(p, "saved", initial_fields=_initial())
    append_outreach_draft(p, channel="recruiter", to_name="Sarah K.",
                           draft_file="2026-06-06_recruiter_sarah-k.md",
                           now=datetime(2026, 6, 6, 11, 0))
    data = load_state(p)
    assert len(data["outreach"]) == 1
    e = data["outreach"][0]
    assert e["channel"] == "recruiter"
    assert e["to"] == "Sarah K."
    assert e["status"] == "draft"
    assert e["draft_file"] == "2026-06-06_recruiter_sarah-k.md"


def test_mark_outreach_sent_flips_status(tmp_path):
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    transition(p, "saved", initial_fields=_initial())
    append_outreach_draft(p, channel="alumni", to_name="John D.",
                           draft_file="2026-06-06_alumni_john-d.md")
    mark_outreach_sent(p, channel="alumni", to_name="John D.")
    data = load_state(p)
    assert data["outreach"][0]["status"] == "sent"


def test_mark_outreach_sent_no_match_raises(tmp_path):
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    transition(p, "saved", initial_fields=_initial())
    with pytest.raises(ValueError, match="no draft outreach found"):
        mark_outreach_sent(p, channel="recruiter", to_name="Nobody")


def test_mark_outreach_sent_picks_latest_matching_draft(tmp_path):
    """When two drafts match, sent-flip applies to the most recent.
    (Realistic: same recruiter, second outreach attempt.)"""
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    transition(p, "saved", initial_fields=_initial())
    append_outreach_draft(p, channel="recruiter", to_name="S",
                           draft_file="d1.md",
                           now=datetime(2026, 6, 1, 10, 0))
    append_outreach_draft(p, channel="recruiter", to_name="S",
                           draft_file="d2.md",
                           now=datetime(2026, 6, 2, 10, 0))
    mark_outreach_sent(p, channel="recruiter", to_name="S")
    data = load_state(p)
    # First draft stays draft, second flipped to sent
    assert data["outreach"][0]["status"] == "draft"
    assert data["outreach"][1]["status"] == "sent"


# ---------- read-only helpers ----------

def test_load_all_states_globs_pipeline_dir(tmp_path):
    pdir = tmp_path / "pipeline"
    for jid in ("aaaaaaaa", "bbbbbbbb", "cccccccc"):
        transition(state_path_for(pdir, jid), "saved",
                    initial_fields=_initial(job_id=jid, company=jid))
    states = load_all_states(pdir)
    assert {s["job_id"] for s in states} == {"aaaaaaaa", "bbbbbbbb", "cccccccc"}


def test_load_all_states_skips_malformed(tmp_path):
    pdir = tmp_path / "pipeline"
    transition(state_path_for(pdir, "aaaaaaaa"), "saved",
                initial_fields=_initial())
    bad = pdir / "bbbbbbbb"
    bad.mkdir()
    (bad / "state.yaml").write_text("this is: not\n  - valid: yaml: :\n")
    states = load_all_states(pdir)
    # Good one survives, bad is skipped (not raised)
    assert {s["job_id"] for s in states} == {"aaaaaaaa"}


def test_load_all_states_empty_when_no_pipeline_dir(tmp_path):
    assert load_all_states(tmp_path / "nope") == []


def test_skip_count(tmp_path):
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    transition(p, "saved", initial_fields=_initial(),
                now=datetime(2026, 6, 1))
    transition(p, "skip", now=datetime(2026, 6, 2))
    transition(p, "saved", now=datetime(2026, 6, 3))
    transition(p, "skip", now=datetime(2026, 6, 4))
    data = load_state(p)
    assert skip_count(data) == 2


def test_skip_count_handles_missing_history():
    assert skip_count({}) == 0
    assert skip_count({"state_history": None}) == 0


# ---------- atomicity ----------

def test_write_is_atomic_no_partial_corruption(tmp_path, monkeypatch):
    """If the rename step fails mid-write, the prior state.yaml must
    still be readable -- atomic write via tmp file + rename."""
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    transition(p, "saved", initial_fields=_initial())
    original = _yaml_load(p)
    # Simulate a write failure by replacing Path.replace with a raiser
    import pathlib
    real_replace = pathlib.Path.replace
    calls = {"n": 0}

    def fail_once(self, target):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated rename failure")
        return real_replace(self, target)

    monkeypatch.setattr(pathlib.Path, "replace", fail_once)
    with pytest.raises(OSError):
        transition(p, "tailored")
    # File should still hold the original content; no half-written YAML
    assert _yaml_load(p) == original
