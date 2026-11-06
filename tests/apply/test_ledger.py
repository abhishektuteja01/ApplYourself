"""The submit ledger: what went out, independent of state.yaml."""

from src.apply import ledger


def test_prior_matches_on_job_id_or_form_url(tmp_path):
    path = tmp_path / "submitted.jsonl"
    ledger.append(path, "aaaa1111", "accepted", form_url="https://jobs.ashbyhq.com/acme/123/")
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
    from src import track_cli
    from src.shared import paths

    ledger.append(
        paths.APPLICATIONS / "apply_runs" / ledger.LEDGER_NAME,
        "aaaa1111",
        "accepted",
        at="2026-08-12T20:38:54",
    )
    assert track_cli.main(["ensure", "aaaa1111"]) == 1
    assert "line from 2026-08-12T20:38:54" in capsys.readouterr().err
    assert not (tmp_path / "pipeline" / "aaaa1111").exists()


def test_a_rejected_submit_blocks_registration_as_prior_does(tmp_path, capsys):
    """The board has a rejected application: registering it fresh at `saved`
    would hide that, so ensure follows ledger.prior."""
    from src import track_cli
    from src.shared import paths

    path = paths.APPLICATIONS / "apply_runs" / ledger.LEDGER_NAME
    ledger.append(path, "bbbb2222", "rejected", detail="duplicate")
    assert ledger.prior(path, "bbbb2222") is not None
    assert track_cli.main(["ensure", "bbbb2222"]) == 1
    assert "uv run track bbbb2222 rejected" in capsys.readouterr().err
    assert not (tmp_path / "pipeline" / "bbbb2222").exists()


def test_a_refused_click_lets_the_role_be_tried_again(tmp_path):
    path = tmp_path / "submitted.jsonl"
    ledger.append(path, "aaaa1111", "clicking")
    ledger.append(path, "aaaa1111", "refused", detail="captcha never solved")
    assert ledger.prior(path, "aaaa1111") is None
    assert ledger.sent_job_ids(path) == set()


def test_every_reader_takes_one_verdict_for_a_rejected_or_unfinished_click(tmp_path):
    from src.apply import sheet

    path = tmp_path / "submitted.jsonl"
    ledger.append(path, "aaaa1111", "clicking")
    ledger.append(path, "aaaa1111", "rejected", detail="the board rejected the application: spam")
    ledger.append(path, "bbbb2222", "clicking")
    assert ledger.verdict(path, "aaaa1111")[0] is ledger.Verdict.REJECTED
    assert ledger.verdict(path, "bbbb2222")[0] is ledger.Verdict.UNKNOWN
    assert ledger.verdict(path, "cccc3333") == (ledger.Verdict.NOT_SENT, None)
    # The sheet shows a rejection as one, and a click with no outcome as unknown.
    assert (
        sheet.status_for("aaaa1111", True, {}, ledger.prior(path, "aaaa1111"), None)[0]
        == "Rejected by board"
    )
    status, note, _ = sheet.status_for("bbbb2222", True, {}, ledger.prior(path, "bbbb2222"), None)
    assert status == "Unsent" and "no outcome was recorded" in note


def test_lever_eu_and_global_hosts_are_one_posting(tmp_path):
    path = tmp_path / "submitted.jsonl"
    ledger.append(path, "aaaa1111", "accepted", form_url="https://jobs.eu.lever.co/acme/123")
    assert ledger.prior(path, "bbbb2222", "https://jobs.lever.co/acme/123")


def test_claim_rechecks_under_the_lock_and_appends_only_when_clear(tmp_path):
    path = tmp_path / "submitted.jsonl"
    assert ledger.claim(path, "aaaa1111", "clicking", company="Acme") == ""
    # A second run that passed its early check finds the first run's line.
    assert "already sent" in ledger.claim(path, "aaaa1111", "clicking")
    assert ledger.claim(path, "bbbb2222", "clicking", held=lambda recs: "cooldown") == "cooldown"
    assert [r["job_id"] for r in ledger.records(path)] == ["aaaa1111"]


def test_an_unstamped_old_line_still_counts_and_a_newer_line_is_refused(tmp_path):
    import json

    import pytest

    from src.shared import schemas

    path = tmp_path / "submitted.jsonl"
    old = json.dumps({"job_id": "aaaa1111", "status": "accepted"}) + "\n"
    path.write_text(old, encoding="utf-8")
    ledger.append(path, "bbbb2222", "clicking")
    assert ledger.sent_job_ids(path) == {"aaaa1111", "bbbb2222"}
    newer = {"schema_version": schemas.LEDGER_SCHEMA_VERSION + 1, "job_id": "cccc3333"}
    text = path.read_text(encoding="utf-8") + json.dumps(newer) + "\n"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="schema version"):
        ledger.records(path)
