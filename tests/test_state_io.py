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
    append_cover_letter,
    append_outreach_draft,
    append_tailored_dir,
    ensure_state,
    load_all_states,
    load_state,
    mark_outreach_sent,
    state_path_for,
    transition,
)


def _initial(**overrides) -> dict:
    base = {
        "job_id": "aaaaaaaa", "company": "Acme", "title": "Widget FN",
        "source": "indeed", "url": "https://x", "location": "Remote",
        "vertical": "example_primary", "sponsorship_label": "opt_ok", "fit_score": 75,
    }
    base.update(overrides)
    return base


def _yaml_load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------- state-set constants ----------

def test_valid_states_count_and_terminal_subset():
    """11 states locked; 4 are terminal."""
    assert len(VALID_STATES) == 11
    assert TERMINAL_STATES <= VALID_STATES
    assert len(TERMINAL_STATES) == 4
    assert TERMINAL_STATES == {"offer", "rejected", "withdrawn", "ghosted"}
    assert ACTIVE_STATES.isdisjoint(TERMINAL_STATES)
    assert "skip" in CLOSED_STATES and "skip" not in TERMINAL_STATES


# ---------- load_state: truncated / corrupt files ----------

def _empty_state_file(tmp_path, content: str = "") -> Path:
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    p.parent.mkdir(parents=True)
    p.write_text(content, encoding="utf-8")
    return p


@pytest.mark.parametrize("content", ["", "\n", "   \n", "# comment only\n", "{}\n"])
def test_load_state_returns_none_for_an_empty_file(tmp_path, content):
    """A file carrying no mapping is indistinguishable from an absent one.
    Returning {} sends transition() down its existing-role branch."""
    assert load_state(_empty_state_file(tmp_path, content)) is None


@pytest.mark.parametrize("content", ["- a\n- b\n", "just a string\n", "42\n"])
def test_load_state_still_raises_on_non_mapping(tmp_path, content):
    with pytest.raises(ValueError, match="not a YAML mapping"):
        load_state(_empty_state_file(tmp_path, content))


def test_load_state_raises_on_malformed_yaml(tmp_path):
    with pytest.raises(ValueError, match="failed to read"):
        load_state(_empty_state_file(tmp_path, "this is: not\n  - valid: yaml: :\n"))


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
    assert data["vertical"] == "example_primary"


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


def test_transition_over_an_empty_file_rebuilds_a_full_record(tmp_path):
    """A truncated state.yaml takes the creation path, not the update path."""
    p = _empty_state_file(tmp_path)
    data = transition(p, "offer", initial_fields=_initial(),
                       now=datetime(2026, 6, 6, 10, 0))
    assert data["job_id"] == "aaaaaaaa"
    assert data["company"] == "Acme"
    assert data["title"] == "Widget FN"
    assert data["state"] == "offer"
    assert data["state_history"] == [
        {"state": "offer", "at": "2026-06-06T10:00:00", "note": ""},
    ]
    assert _yaml_load(p)["job_id"] == "aaaaaaaa"


def test_transition_over_an_empty_file_requires_initial_fields(tmp_path):
    p = _empty_state_file(tmp_path)
    with pytest.raises(ValueError, match="initial_fields"):
        transition(p, "offer")


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
    """No transitions out of any of the 4 terminal states."""
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    transition(p, terminal, initial_fields=_initial())
    with pytest.raises(ValueError, match="cannot transition out of terminal"):
        transition(p, "saved")


@pytest.mark.parametrize("record", [
    {"state": "saved", "state_history": []},
    {"job_id": "", "state": "saved", "state_history": []},
    {"job_id": None, "state": "saved", "state_history": []},
])
def test_transition_refuses_an_existing_record_without_job_id(tmp_path, record):
    """A partially-written file is a dict, so it reaches the update branch —
    which would otherwise write a record no downstream reader can key on."""
    p = _empty_state_file(tmp_path, yaml.safe_dump(record, sort_keys=False))
    before = p.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="no job_id"):
        transition(p, "applied")
    assert p.read_text(encoding="utf-8") == before


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
    append_tailored_dir(p, "2026-06-06_acme_widget-fn_aaaaaaaa")
    data = load_state(p)
    assert data["tailored_dirs"] == ["2026-06-06_acme_widget-fn_aaaaaaaa"]


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


# ---------- side lists over a truncated file ----------

def test_side_list_helpers_refuse_an_empty_file(tmp_path):
    """Each would otherwise write a file holding only its own side list."""
    p = _empty_state_file(tmp_path)
    for call in (
        lambda: append_tailored_dir(p, "d1"),
        lambda: append_cover_letter(p, "c1"),
        lambda: append_outreach_draft(p, channel="recruiter", to_name="S",
                                      draft_file="d.md"),
        lambda: mark_outreach_sent(p, channel="recruiter", to_name="S"),
    ):
        with pytest.raises(ValueError, match="does not exist"):
            call()
    assert p.read_text(encoding="utf-8") == ""


def test_ensure_state_over_an_empty_file_creates(tmp_path):
    """/tailor's bootstrap must repair a truncated file, not adopt it."""
    p = _empty_state_file(tmp_path)
    data, created = ensure_state(p, initial_fields=_initial())
    assert created is True
    assert data["job_id"] == "aaaaaaaa"
    assert data["state"] == "saved"


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
    (bad / "state.yaml").write_text("this is: not\n  - valid: yaml: :\n", encoding="utf-8")
    states = load_all_states(pdir)
    # Good one survives, bad is skipped (not raised)
    assert {s["job_id"] for s in states} == {"aaaaaaaa"}


def test_load_all_states_skips_empty_files(tmp_path):
    pdir = tmp_path / "pipeline"
    transition(state_path_for(pdir, "aaaaaaaa"), "saved",
                initial_fields=_initial())
    (pdir / "bbbbbbbb").mkdir()
    (pdir / "bbbbbbbb" / "state.yaml").write_text("", encoding="utf-8")
    assert {s["job_id"] for s in load_all_states(pdir)} == {"aaaaaaaa"}


def test_load_all_states_empty_when_no_pipeline_dir(tmp_path):
    assert load_all_states(tmp_path / "nope") == []


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


# ---------- ensure_state: bootstrap only, never a transition (R10) ----------

def test_ensure_state_creates_at_saved_when_absent(tmp_path):
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    data, created = ensure_state(p, initial_fields=_initial())
    assert created is True
    assert data["state"] == "saved"
    assert len(data["state_history"]) == 1
    assert _yaml_load(p)["state"] == "saved"


@pytest.mark.parametrize("existing_state", sorted(VALID_STATES))
def test_ensure_state_never_mutates_an_existing_role(tmp_path, existing_state):
    """A re-tailor must not touch state, terminal states included."""
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    transition(p, existing_state, initial_fields=_initial())
    before = p.read_text(encoding="utf-8")

    data, created = ensure_state(p, initial_fields=_initial())

    assert created is False
    assert data["state"] == existing_state
    assert p.read_text(encoding="utf-8") == before, "ensure_state rewrote the file"


def test_ensure_state_preserves_applied_at_and_history(tmp_path):
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    transition(p, "saved", initial_fields=_initial())
    transition(p, "applied")
    applied_at = _yaml_load(p)["applied_at"]
    assert applied_at is not None

    ensure_state(p, initial_fields=_initial())

    after = _yaml_load(p)
    assert after["state"] == "applied"
    assert after["applied_at"] == applied_at
    assert len(after["state_history"]) == 2


# ---------- cover_letters[] ----------

def test_append_cover_letter(tmp_path):
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    transition(p, "saved", initial_fields=_initial())
    append_cover_letter(p, "example_primary/2026-06-06_acme_widget-fn_aaaaaaaa")
    assert load_state(p)["cover_letters"] == ["example_primary/2026-06-06_acme_widget-fn_aaaaaaaa"]


def test_append_cover_letter_idempotent(tmp_path):
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    transition(p, "saved", initial_fields=_initial())
    append_cover_letter(p, "d1")
    append_cover_letter(p, "d1")   # same again — should NOT duplicate
    append_cover_letter(p, "d2")
    assert load_state(p)["cover_letters"] == ["d1", "d2"]


def test_append_cover_letter_preserves_order(tmp_path):
    """cover-letter.md reads the LAST entry, so append order is load-bearing."""
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    transition(p, "saved", initial_fields=_initial())
    for d in ("d1", "d2", "d3"):
        append_cover_letter(p, d)
    assert load_state(p)["cover_letters"] == ["d1", "d2", "d3"]


def test_append_cover_letter_requires_existing_state(tmp_path):
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    with pytest.raises(ValueError, match="does not exist"):
        append_cover_letter(p, "d1")


def test_append_cover_letter_bumps_last_touch_only(tmp_path):
    """R10: /cover-letter may append to a side list but must never transition."""
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    transition(p, "applied", initial_fields=_initial(),
                now=datetime(2026, 6, 1, 10, 0))
    before = load_state(p)
    append_cover_letter(p, "d1", now=datetime(2026, 6, 6, 9, 30))
    after = load_state(p)
    assert after["state"] == "applied"
    assert after["applied_at"] == before["applied_at"]
    assert after["last_touch"] == "2026-06-06T09:30:00"
    assert after["state_history"] == before["state_history"]


def test_append_cover_letter_independent_of_tailored_dirs(tmp_path):
    """Two side lists, two keys — one must not clobber the other."""
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    transition(p, "saved", initial_fields=_initial())
    append_tailored_dir(p, "t1")
    append_cover_letter(p, "c1")
    data = load_state(p)
    assert data["tailored_dirs"] == ["t1"]
    assert data["cover_letters"] == ["c1"]


def test_append_cover_letter_over_a_preexisting_null(tmp_path):
    """A state.yaml written with `cover_letters:` (null) must not raise."""
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    transition(p, "saved", initial_fields=_initial())
    data = load_state(p)
    data["cover_letters"] = None
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    append_cover_letter(p, "c1")
    assert load_state(p)["cover_letters"] == ["c1"]


def test_append_cover_letter_returns_the_written_state(tmp_path):
    p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
    transition(p, "saved", initial_fields=_initial())
    returned = append_cover_letter(p, "c1")
    assert returned == load_state(p)


class TestEnsureState:
    """ensure_state is the R10 boundary: /tailor uses it to register a role
    without becoming a transition writer. Untested until now, so nothing
    stopped it from quietly appending history on an existing role."""

    def test_creates_at_saved_when_absent(self, tmp_path):
        p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
        data, created = ensure_state(p, _initial(), now=datetime(2026, 6, 1))
        assert created is True
        assert data["state"] == "saved"
        assert len(data["state_history"]) == 1

    def test_is_a_no_op_that_appends_no_history_on_an_existing_role(self, tmp_path):
        p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
        transition(p, "saved", initial_fields=_initial(), now=datetime(2026, 6, 1))
        transition(p, "applied", now=datetime(2026, 6, 2))
        before = _yaml_load(p)

        data, created = ensure_state(p, _initial(), now=datetime(2026, 6, 3))
        assert created is False
        assert data["state"] == "applied"
        assert _yaml_load(p) == before, "ensure_state must not write anything"

    def test_leaves_a_terminal_state_untouched(self, tmp_path):
        """A re-tailor of a rejected role must not resurrect it, and must not
        trip the terminal-state guard either."""
        p = state_path_for(tmp_path / "pipeline", "aaaaaaaa")
        transition(p, "saved", initial_fields=_initial(), now=datetime(2026, 6, 1))
        transition(p, "rejected", now=datetime(2026, 6, 2))
        data, created = ensure_state(p, _initial(), now=datetime(2026, 6, 3))
        assert created is False
        assert data["state"] == "rejected"
