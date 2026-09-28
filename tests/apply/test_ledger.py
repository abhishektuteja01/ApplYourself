"""The submit ledger: what went out, independent of state.yaml."""
from src.apply import ledger


def test_prior_matches_on_job_id_or_form_url(tmp_path):
    path = tmp_path / "submitted.jsonl"
    ledger.append(path, "aaaa1111", "accepted",
                  form_url="https://jobs.ashbyhq.com/acme/123/")
    assert ledger.prior(path, "aaaa1111")["status"] == "accepted"
    # Same posting under another id (a re-created or re-keyed role).
    assert ledger.prior(path, "bbbb2222", "http://www.jobs.ashbyhq.com/ACME/123")
    assert ledger.prior(path, "bbbb2222", "https://jobs.ashbyhq.com/acme/999") is None


def test_the_latest_line_wins_and_bad_lines_are_skipped(tmp_path):
    path = tmp_path / "submitted.jsonl"
    ledger.append(path, "aaaa1111", "clicking")
    path.write_text(path.read_text(encoding="utf-8") + "not json\n", encoding="utf-8")
    ledger.append(path, "aaaa1111", "rejected", detail="spam")
    assert ledger.prior(path, "aaaa1111")["status"] == "rejected"
    assert ledger.sent_job_ids(path) == {"aaaa1111"}


def test_a_missing_ledger_is_empty(tmp_path):
    assert ledger.records(tmp_path / "none.jsonl") == []


def test_registration_refuses_a_role_the_ledger_says_was_sent(tmp_path, monkeypatch, capsys):
    from src import paths, track_cli
    monkeypatch.setattr(track_cli, "PIPELINE", tmp_path / "pipeline")
    ledger.append(paths.APPLICATIONS / "apply_runs" / ledger.LEDGER_NAME, "aaaa1111",
                  "accepted", at="2026-08-12T20:38:54")
    assert track_cli.main(["ensure", "aaaa1111"]) == 1
    assert "sent on 2026-08-12T20:38:54" in capsys.readouterr().err
    assert not (tmp_path / "pipeline" / "aaaa1111").exists()


def test_a_rejected_submit_does_not_block_registration(tmp_path, monkeypatch):
    from src import paths, track_cli
    monkeypatch.setattr(track_cli, "PIPELINE", tmp_path / "pipeline")
    monkeypatch.setattr(track_cli, "_gather_initial_fields", lambda job_id: {"job_id": job_id, "company": "Acme", "title": "Engineer"})
    ledger.append(paths.APPLICATIONS / "apply_runs" / ledger.LEDGER_NAME, "bbbb2222",
                  "rejected")
    assert track_cli.main(["ensure", "bbbb2222"]) == 0
