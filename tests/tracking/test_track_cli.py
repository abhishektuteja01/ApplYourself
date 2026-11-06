"""Tests for src/applyourself/track_cli.py — the terminal entry point for /track.
state_io transition/mark_outreach_sent logic is covered by test_state_io.py;
these tests cover only the CLI's own plumbing: dispatch, parquet-backed
initial_fields lookup, and error exit codes."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

from applyourself import track_cli
from applyourself.shared import paths
from tests.factories import write_clean, write_state


@pytest.fixture(autouse=True)
def _paths(tmp_path, monkeypatch):
    monkeypatch.setitem(paths._overrides, "CLEAN", tmp_path / "clean.parquet")
    monkeypatch.setitem(paths._overrides, "SCORED", tmp_path / "scored.parquet")
    monkeypatch.setitem(paths._overrides, "STORE", tmp_path / "jobs.parquet")
    return tmp_path


def _write_clean(tmp_path, rows):
    write_clean(tmp_path / "clean.parquet", *rows)


def test_first_transition_creates_state_from_clean_parquet(tmp_path, capsys):
    _write_clean(
        tmp_path,
        [
            {
                "job_id": "aaaaaaaa",
                "company": "Acme",
                "title": "Widget FN",
                "source": "indeed",
                "url": "https://x",
                "location": "Remote",
                "vertical": "example_primary",
            }
        ],
    )
    rc = track_cli.main(["aaaaaaaa", "saved"])
    assert rc == 0
    state = yaml.safe_load(
        (tmp_path / "pipeline" / "aaaaaaaa" / "state.yaml").read_text(encoding="utf-8")
    )
    assert state["state"] == "saved"
    assert state["company"] == "Acme"
    out = capsys.readouterr().out
    assert "OK: aaaaaaaa" in out
    assert "next: ready for `/tailor aaaaaaaa`" in out


def test_missing_job_id_in_clean_parquet_errors(tmp_path):
    _write_clean(tmp_path, [{"job_id": "bbbbbbbb", "company": "X", "title": "Y"}])
    with pytest.raises(SystemExit):
        track_cli.main(["aaaaaaaa", "saved"])


def test_transition_out_of_terminal_state_errors(tmp_path, capsys):
    _write_clean(
        tmp_path,
        [
            {
                "job_id": "aaaaaaaa",
                "company": "Acme",
                "title": "Widget FN",
                "source": "",
                "url": "",
                "location": "",
                "vertical": "example_primary",
            }
        ],
    )
    assert track_cli.main(["aaaaaaaa", "rejected"]) == 0
    rc = track_cli.main(["aaaaaaaa", "applied"])
    assert rc == 1
    assert "ERROR:" in capsys.readouterr().err


def test_unknown_state_rejected_by_argparse(tmp_path):
    with pytest.raises(SystemExit):
        track_cli.main(["aaaaaaaa", "bogus_state"])


def test_outreach_sent_flips_latest_draft(tmp_path, capsys):
    state_dir = write_state(
        tmp_path / "pipeline",
        "aaaaaaaa",
        title="Widget FN",
        outreach=[
            {"channel": "recruiter", "to": "Jane", "status": "draft"},
        ],
    ).parent
    rc = track_cli.main(["outreach-sent", "aaaaaaaa", "--channel", "recruiter", "--to", "Jane"])
    assert rc == 0
    state = yaml.safe_load((state_dir / "state.yaml").read_text(encoding="utf-8"))
    assert state["outreach"][0]["status"] == "sent"
    assert "OK: outreach[] entry flipped to sent" in capsys.readouterr().out


def test_outreach_sent_missing_state_file_errors(tmp_path, capsys):
    rc = track_cli.main(["outreach-sent", "aaaaaaaa", "--channel", "recruiter", "--to", "Jane"])
    assert rc == 1
    assert "state.yaml missing" in capsys.readouterr().err


def test_ensure_registers_then_is_a_noop(tmp_path, capsys):
    """/tailor calls this: create the role once, then never touch it."""
    _write_clean(
        tmp_path,
        [
            {
                "job_id": "aaaaaaaa",
                "company": "Acme",
                "title": "Widget FN",
                "source": "indeed",
                "url": "https://x",
                "location": "Remote",
                "vertical": "example_primary",
            }
        ],
    )

    assert track_cli.main(["ensure", "aaaaaaaa"]) == 0
    p = tmp_path / "pipeline" / "aaaaaaaa" / "state.yaml"
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["state"] == "saved"
    assert "registered" in capsys.readouterr().out

    assert track_cli.main(["aaaaaaaa", "applied"]) == 0
    before = p.read_text(encoding="utf-8")

    assert track_cli.main(["ensure", "aaaaaaaa"]) == 0
    out = capsys.readouterr().out
    assert "already registered" in out and "applied" in out
    assert p.read_text(encoding="utf-8") == before


def test_ensure_registers_a_role_only_in_the_store(tmp_path, capsys):
    """A role retention dropped from clean.parquet can still be registered."""
    _write_clean(
        tmp_path,
        [
            {
                "job_id": "bbbbbbbb",
                "company": "Other",
                "title": "T",
                "source": "ashby",
                "url": "",
                "location": "",
                "vertical": "v",
            }
        ],
    )
    pd.DataFrame(
        [
            {
                "job_id": "aaaaaaaa",
                "company": "Acme",
                "title": "Widget FN",
                "source": "ashby",
                "url": "https://old",
                "location_raw": "Remote",
                "vertical": "example_primary",
                "last_seen": pd.Timestamp("2026-01-01"),
            },
            {
                "job_id": "aaaaaaaa",
                "company": "Acme",
                "title": "Widget FN",
                "source": "ashby",
                "url": "https://new",
                "location_raw": "Boston, MA",
                "vertical": "example_primary",
                "last_seen": pd.Timestamp("2026-02-01"),
            },
        ]
    ).to_parquet(tmp_path / "jobs.parquet")

    assert track_cli.main(["ensure", "aaaaaaaa"]) == 0
    state = yaml.safe_load(
        (tmp_path / "pipeline" / "aaaaaaaa" / "state.yaml").read_text(encoding="utf-8")
    )
    assert state["state"] == "saved"
    assert state["url"] == "https://new" and state["location"] == "Boston, MA"

    with pytest.raises(SystemExit, match=r"not in clean.parquet or the jobs store"):
        track_cli.main(["ensure", "cccccccc"])


def _truncated_state(tmp_path) -> Path:
    p = tmp_path / "pipeline" / "aaaaaaaa" / "state.yaml"
    p.parent.mkdir(parents=True)
    p.write_text("", encoding="utf-8")
    return p


def test_transition_over_a_truncated_state_file_rebuilds_it(tmp_path, capsys):
    """The CLI gates on content, not p.exists(), so an empty state.yaml is
    rebuilt from the parquets instead of silently losing its job_id."""
    _write_clean(
        tmp_path,
        [
            {
                "job_id": "aaaaaaaa",
                "company": "Acme",
                "title": "Widget FN",
                "source": "indeed",
                "url": "https://x",
                "location": "Remote",
                "vertical": "example_primary",
            }
        ],
    )
    p = _truncated_state(tmp_path)
    assert track_cli.main(["aaaaaaaa", "applied"]) == 0
    state = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert state["job_id"] == "aaaaaaaa"
    assert state["company"] == "Acme"
    assert state["vertical"] == "example_primary"
    assert state["state"] == "applied"
    assert "OK: aaaaaaaa" in capsys.readouterr().out


def test_ensure_over_a_truncated_state_file_rebuilds_it(tmp_path, capsys):
    _write_clean(
        tmp_path,
        [
            {
                "job_id": "aaaaaaaa",
                "company": "Acme",
                "title": "Widget FN",
                "source": "",
                "url": "",
                "location": "",
                "vertical": "example_primary",
            }
        ],
    )
    p = _truncated_state(tmp_path)
    assert track_cli.main(["ensure", "aaaaaaaa"]) == 0
    state = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert state["job_id"] == "aaaaaaaa"
    assert state["state"] == "saved"
    assert "registered" in capsys.readouterr().out


def test_transition_over_a_corrupt_state_file_errors(tmp_path, capsys):
    """Content but no job_id: refuse rather than write an unkeyable record."""
    p = tmp_path / "pipeline" / "aaaaaaaa" / "state.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(yaml.safe_dump({"state": "saved", "state_history": []}), encoding="utf-8")
    before = p.read_text(encoding="utf-8")
    assert track_cli.main(["aaaaaaaa", "applied"]) == 1
    assert "no job_id" in capsys.readouterr().err
    assert p.read_text(encoding="utf-8") == before


def test_transition_over_malformed_yaml_errors(tmp_path, capsys):
    p = tmp_path / "pipeline" / "aaaaaaaa" / "state.yaml"
    p.parent.mkdir(parents=True)
    p.write_text("this is: not\n  - valid: yaml: :\n", encoding="utf-8")
    assert track_cli.main(["aaaaaaaa", "applied"]) == 1
    assert "failed to read" in capsys.readouterr().err


def test_ensure_does_not_raise_on_terminal_state(tmp_path):
    """A re-tailor of a rejected role must not hit the terminal-state check."""
    _write_clean(
        tmp_path,
        [
            {
                "job_id": "aaaaaaaa",
                "company": "Acme",
                "title": "Widget FN",
                "source": "",
                "url": "",
                "location": "",
                "vertical": "example_primary",
            }
        ],
    )
    track_cli.main(["aaaaaaaa", "rejected"])
    p = tmp_path / "pipeline" / "aaaaaaaa" / "state.yaml"
    before = p.read_text(encoding="utf-8")

    assert track_cli.main(["ensure", "aaaaaaaa"]) == 0
    assert p.read_text(encoding="utf-8") == before


def test_if_guard_fires_only_from_the_named_state(tmp_path, capsys):
    """`--if saved` lets /cover-letter and /apply promote a role without
    reading its state in a separate shell call first."""
    _write_clean(
        tmp_path,
        [
            {
                "job_id": "aaaaaaaa",
                "company": "Acme",
                "title": "Widget FN",
                "source": "",
                "url": "",
                "location": "",
                "vertical": "example_primary",
            }
        ],
    )
    state_file = tmp_path / "pipeline" / "aaaaaaaa" / "state.yaml"
    assert track_cli.main(["aaaaaaaa", "tailored", "--if", "saved"]) == 0
    assert not state_file.parent.exists()  # untracked: no-op, no empty folder
    assert track_cli.main(["aaaaaaaa", "saved"]) == 0
    assert track_cli.main(["aaaaaaaa", "tailored", "--if", "saved"]) == 0
    assert yaml.safe_load(state_file.read_text(encoding="utf-8"))["state"] == "tailored"
    assert track_cli.main(["aaaaaaaa", "applied"]) == 0
    capsys.readouterr()
    assert track_cli.main(["aaaaaaaa", "tailored", "--if", "saved"]) == 0
    assert yaml.safe_load(state_file.read_text(encoding="utf-8"))["state"] == "applied"
    assert "left unchanged" in capsys.readouterr().out


def test_if_guard_checks_the_state_under_the_lock(tmp_path, monkeypatch, capsys):
    """A writer that moves the role after the CLI's first read still wins."""
    _write_clean(
        tmp_path,
        [
            {
                "job_id": "aaaaaaaa",
                "company": "Acme",
                "title": "Widget FN",
                "source": "",
                "url": "",
                "location": "",
                "vertical": "example_primary",
            }
        ],
    )
    track_cli.main(["aaaaaaaa", "saved"])
    track_cli.main(["aaaaaaaa", "applied"])
    state_file = tmp_path / "pipeline" / "aaaaaaaa" / "state.yaml"
    stale = {**yaml.safe_load(state_file.read_text(encoding="utf-8")), "state": "saved"}
    monkeypatch.setattr(track_cli, "load_state", lambda _p: stale)
    capsys.readouterr()
    assert track_cli.main(["aaaaaaaa", "tailored", "--if", "saved"]) == 0
    assert yaml.safe_load(state_file.read_text(encoding="utf-8"))["state"] == "applied"
    assert "left unchanged" in capsys.readouterr().out


# --- migrate-events -----------------------------------------------------------


def _real_shaped_pipeline(tmp_path) -> Path:
    """A few state.yaml files shaped like the real ones, plus a submit ledger
    with a sent, a rejected and a clicking role that have no state.yaml."""
    import json

    from applyourself import state_io

    pipe = tmp_path / "pipeline"
    p = state_io.state_path_for(pipe, "aaaaaaaa")
    initial = {"job_id": "aaaaaaaa", "company": "A", "title": "T"}
    state_io.transition(p, "saved", initial_fields=initial)
    state_io.append_tailored_dir(p, "example_primary/a_1")
    state_io.append_outreach_draft(p, channel="recruiter", to_name="S", draft_file="d.md")
    state_io.mark_outreach_sent(p, channel="recruiter", to_name="S")
    state_io.transition(p, "applied")
    state_io.transition(p, "applied", note="re-sent", force=True)
    state_io.mark_cleaned(p, {"date": "2026-10-01", "archived": None})
    write_state(pipe, "bbbbbbbb", "skip")
    led = tmp_path / "applications" / "apply_runs" / "submitted.jsonl"
    led.parent.mkdir(parents=True)
    lines = [
        {"at": "2026-08-12T19:05:03", "job_id": "cccccccc", "status": "backfill", "company": "C"},
        {"at": "2026-08-13T10:00:00", "job_id": "dddddddd", "status": "rejected", "company": "D"},
        {"at": "2026-08-14T10:00:00", "job_id": "eeeeeeee", "status": "clicking", "company": "E"},
    ]
    led.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return pipe


def test_migrate_events_dry_run_checks_the_fold_and_writes_nothing(tmp_path, capsys):
    from applyourself import state_io

    pipe = _real_shaped_pipeline(tmp_path)
    before = sorted(str(f) for f in pipe.rglob("*"))
    assert track_cli.main(["migrate-events", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "2 state.yaml files, 2 sent roles" in out
    assert "eeeeeeee" in out  # the clicking role is named, not restored
    assert sorted(str(f) for f in pipe.rglob("*")) == before

    assert track_cli.main(["migrate-events", "--apply"]) == 0
    idx = state_io.load_state_index(pipe)
    assert idx["cccccccc"]["state"] == "applied"
    assert idx["dddddddd"]["state"] == "rejected"
    assert "eeeeeeee" not in idx
    # After the switch each write appends an event and the cache stays the fold.
    assert track_cli.main(["bbbbbbbb", "saved"]) == 0
    events = state_io.read_events(pipe / "events.jsonl")
    assert events[-1]["kind"] == "transition" and events[-1]["from"] == "skip"
    assert state_io.rebuild_states(pipe, apply=False) == []
    # A lost state.yaml comes back from the log instead of re-registering.
    p = state_io.state_path_for(pipe, "bbbbbbbb")
    p.unlink()
    assert track_cli.main(["ensure", "bbbbbbbb"]) == 0
    restored = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert restored["state"] == "saved"
    assert len(restored["state_history"]) == 2


def test_migrate_events_refuses_on_a_mismatch(tmp_path, capsys):
    pipe = _real_shaped_pipeline(tmp_path)
    p = pipe / "bbbbbbbb" / "state.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    p.write_text(yaml.safe_dump({**data, "state": "applied"}), encoding="utf-8")
    assert track_cli.main(["migrate-events", "--apply"]) == 1
    assert "MISMATCH: bbbbbbbb  state" in capsys.readouterr().err
    assert not (pipe / "events.jsonl").exists()


def test_score_copies_are_joined_then_dropped_through_the_log(tmp_path, capsys):
    """An old state.yaml's fit_score copy is shown from scored.parquet, folds
    the same after the migration, and drop-score-copies removes it by event."""
    import json

    from applyourself import state_io
    from tests.factories import scored_rows, state_record

    pipe = tmp_path / "pipeline"
    old = {**state_record("saved"), "sponsorship_label": "opt_ok", "fit_score": 60}
    p = state_io.state_path_for(pipe, "aaaaaaaa")
    p.parent.mkdir(parents=True)
    p.write_text(yaml.safe_dump(old, sort_keys=False), encoding="utf-8")
    scored_rows({"job_id": "aaaaaaaa", "fit_score": 88.0}).to_parquet(tmp_path / "scored.parquet")
    assert track_cli.main(["migrate-events", "--apply"]) == 0
    assert state_io.rebuild_states(pipe, apply=False) == []
    capsys.readouterr()

    assert track_cli.main(["states"]) == 0
    (row,) = json.loads(capsys.readouterr().out)["states"]
    assert row["fit_score"] == 88

    assert track_cli.main(["drop-score-copies", "--dry-run"]) == 0
    assert "would drop the score copies from 1 of 1" in capsys.readouterr().out
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["fit_score"] == 60
    assert track_cli.main(["drop-score-copies", "--apply"]) == 0
    left = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert "fit_score" not in left and "sponsorship_label" not in left
    assert state_io.read_events(pipe / "events.jsonl")[-1]["kind"] == "fields_dropped"
    assert state_io.rebuild_states(pipe, apply=False) == []


def test_standup_renders_the_tables_without_touching_state(tmp_path, monkeypatch):
    """A stale `applied` lands in Closed as ghosted, the fit comes from
    scored.parquet, a `|` in a cell is escaped, and no state.yaml changes."""
    from tests.factories import scored_rows

    monkeypatch.setattr(track_cli, "ghosted_after_days", lambda: 30)
    pipe = tmp_path / "pipeline"
    saved = write_state(pipe, "aaaaaaaa", "saved", title="Data | Analyst", tailored_dirs=["a", "b"])
    write_state(pipe, "bbbbbbbb", "applied")
    before = saved.read_text(encoding="utf-8")
    scored_rows({"job_id": "aaaaaaaa", "fit_score": 88.0}).to_parquet(tmp_path / "scored.parquet")
    note = tmp_path / "note.md"
    note.write_text("Two roles.\n", encoding="utf-8")

    assert track_cli.main(["standup", "--note-file", str(note)]) == 0
    md = (tmp_path / "pipeline.md").read_text(encoding="utf-8").splitlines()
    assert md[0].startswith("# Pipeline — ") and md[2] == "Two roles."
    active, closed = md.index("## Active (1)"), md.index("## Closed (1)")
    assert r"| saved | Acme | Data \| Analyst | ? | 88 |" in md[active + 4]
    assert "`b`" in md[active + 4]
    assert md[closed + 4].startswith("| ghosted | Acme |")
    assert saved.read_text(encoding="utf-8") == before
