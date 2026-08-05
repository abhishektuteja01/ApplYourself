"""Tests for src/track_cli.py — the terminal entry point for /track.
state_io transition/mark_outreach_sent logic is covered by test_state_io.py;
these tests cover only the CLI's own plumbing: dispatch, parquet-backed
initial_fields lookup, and error exit codes."""
from __future__ import annotations

import pandas as pd
import pytest
import yaml

from src import track_cli


@pytest.fixture(autouse=True)
def _paths(tmp_path, monkeypatch):
    monkeypatch.setattr(track_cli, "PIPELINE", tmp_path / "pipeline")
    monkeypatch.setattr(track_cli, "CLEAN", tmp_path / "clean.parquet")
    monkeypatch.setattr(track_cli, "SCORED", tmp_path / "scored.parquet")
    return tmp_path


def _write_clean(tmp_path, rows):
    pd.DataFrame(rows).to_parquet(tmp_path / "clean.parquet")


def test_first_transition_creates_state_from_clean_parquet(tmp_path, capsys):
    _write_clean(tmp_path, [{
        "job_id": "aaaaaaaa", "company": "Acme", "title": "Widget FN",
        "source": "indeed", "url": "https://x", "location": "Remote",
        "vertical": "example_primary",
    }])
    rc = track_cli.main(["aaaaaaaa", "saved"])
    assert rc == 0
    state = yaml.safe_load((tmp_path / "pipeline" / "aaaaaaaa" / "state.yaml").read_text())
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
    _write_clean(tmp_path, [{
        "job_id": "aaaaaaaa", "company": "Acme", "title": "Widget FN",
        "source": "", "url": "", "location": "", "vertical": "example_primary",
    }])
    assert track_cli.main(["aaaaaaaa", "rejected"]) == 0
    rc = track_cli.main(["aaaaaaaa", "applied"])
    assert rc == 1
    assert "ERROR:" in capsys.readouterr().err


def test_unknown_state_rejected_by_argparse(tmp_path):
    with pytest.raises(SystemExit):
        track_cli.main(["aaaaaaaa", "bogus_state"])


def test_outreach_sent_flips_latest_draft(tmp_path, capsys):
    state_dir = tmp_path / "pipeline" / "aaaaaaaa"
    state_dir.mkdir(parents=True)
    (state_dir / "state.yaml").write_text(yaml.safe_dump({
        "job_id": "aaaaaaaa", "company": "Acme", "title": "Widget FN",
        "state": "saved", "state_history": [], "outreach": [
            {"channel": "recruiter", "to": "Jane", "status": "draft"},
        ],
    }))
    rc = track_cli.main(["outreach-sent", "aaaaaaaa", "--channel", "recruiter", "--to", "Jane"])
    assert rc == 0
    state = yaml.safe_load((state_dir / "state.yaml").read_text())
    assert state["outreach"][0]["status"] == "sent"
    assert "OK: outreach[] entry flipped to sent" in capsys.readouterr().out


def test_outreach_sent_missing_state_file_errors(tmp_path, capsys):
    rc = track_cli.main(["outreach-sent", "aaaaaaaa", "--channel", "recruiter", "--to", "Jane"])
    assert rc == 1
    assert "state.yaml missing" in capsys.readouterr().err


def test_ensure_registers_then_is_a_noop(tmp_path, capsys):
    """/tailor calls this: create the role once, then never touch it."""
    _write_clean(tmp_path, [{
        "job_id": "aaaaaaaa", "company": "Acme", "title": "Widget FN",
        "source": "indeed", "url": "https://x", "location": "Remote",
        "vertical": "example_primary",
    }])

    assert track_cli.main(["ensure", "aaaaaaaa"]) == 0
    p = tmp_path / "pipeline" / "aaaaaaaa" / "state.yaml"
    assert yaml.safe_load(p.read_text())["state"] == "saved"
    assert "registered" in capsys.readouterr().out

    assert track_cli.main(["aaaaaaaa", "applied"]) == 0
    before = p.read_text()

    assert track_cli.main(["ensure", "aaaaaaaa"]) == 0
    out = capsys.readouterr().out
    assert "already registered" in out and "applied" in out
    assert p.read_text() == before


def test_ensure_does_not_raise_on_terminal_state(tmp_path):
    """A re-tailor of a rejected role must not hit the terminal-state check."""
    _write_clean(tmp_path, [{
        "job_id": "aaaaaaaa", "company": "Acme", "title": "Widget FN",
        "source": "", "url": "", "location": "", "vertical": "example_primary",
    }])
    track_cli.main(["aaaaaaaa", "rejected"])
    p = tmp_path / "pipeline" / "aaaaaaaa" / "state.yaml"
    before = p.read_text()

    assert track_cli.main(["ensure", "aaaaaaaa"]) == 0
    assert p.read_text() == before
