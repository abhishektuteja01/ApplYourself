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
