"""discover-ids: the job_ids table and dedupe_key built from today's files."""

from __future__ import annotations

import json

import pandas as pd

from applyourself.discovery import id_table
from applyourself.discovery import store as S
from applyourself.shared.parquet_io import write_parquet

TODAY = pd.Timestamp("2026-09-27")


def _row(url, company, title, job_id):
    return {
        **S.make_posting(url=url, company=company, title=title, source="greenhouse"),
        "job_id": job_id,
        "first_seen": TODAY,
        "last_seen": TODAY,
    }


def _setup(tmp_path):
    jobs, pipe = tmp_path / "jobs", tmp_path / "pipeline"
    pipe.mkdir()
    rows = [
        _row("https://x.io/1", "Acme", "AI Engineer", "inherit1"),
        _row("https://x.io/2", "Acme Inc", "AI Engineer", "inherit1"),
        _row("https://x.io/3", "Acme AG", "AI Engineer", "bbbb2222"),
    ]
    # Today's store shape: no dedupe_key column yet.
    write_parquet(
        S.coerce_store(pd.DataFrame(rows)).drop(columns="dedupe_key"), jobs / "jobs.parquet"
    )
    created = {
        "v": 1,
        "job_id": "cccc3333",
        "kind": "created",
        "data": {"company": "Zeta", "title": "Chef"},
    }
    (pipe / "events.jsonl").write_text(json.dumps(created) + "\n", encoding="utf-8")
    return jobs, pipe


def test_dry_run_writes_nothing_and_apply_keeps_every_id(tmp_path, capsys):
    jobs, pipe = _setup(tmp_path)
    args = ["--jobs-dir", str(jobs), "--pipeline-dir", str(pipe)]
    assert id_table.main(["--dry-run", *args]) == 0
    out = capsys.readouterr().out
    assert "dedupe_key_stored        False" in out and "dedupe_maybe_pairs       1" in out
    assert "from_events              1" in out and not (jobs / "job_ids.parquet").exists()
    assert id_table.main(["--apply", *args]) == 0
    table = S.load_job_ids(jobs / "job_ids.parquet")
    assert set(table["job_id"]) == {"inherit1", "bbbb2222", "cccc3333"} and len(table) == 3
    assert set(S.load_store(jobs / "jobs.parquet")["job_id"]) == {"inherit1", "bbbb2222"}
    assert "dedupe_key" in pd.read_parquet(jobs / "jobs.parquet").columns


def test_a_blank_pair_in_the_table_is_a_conflict(tmp_path):
    jobs, pipe = _setup(tmp_path)
    bad = pd.DataFrame([{"job_id": "dddd4444", "company_normalized": "", "title_normalized": "x"}])
    S.save_job_ids(bad, jobs / "job_ids.parquet")
    assert id_table.main(["--dry-run", "--jobs-dir", str(jobs), "--pipeline-dir", str(pipe)]) == 1
