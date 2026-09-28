"""The status sheet is derived from state, ledger and run reports — never typed."""
from __future__ import annotations

import openpyxl
import pandas as pd
import yaml

from src import paths
from src.apply import ledger, sheet

GH = "https://job-boards.greenhouse.io/acme/jobs/1"


def _world(tmp_path, monkeypatch):
    for name, value in (("REPO_ROOT", tmp_path), ("PROFILE", tmp_path / "profile"),
                        ("PIPELINE", tmp_path / "pipeline"),
                        ("SCORED", tmp_path / "scored.parquet"),
                        ("CLEAN", tmp_path / "clean.parquet")):
        monkeypatch.setattr(paths, name, value)
    from src import apply_cli
    monkeypatch.setattr(apply_cli, "PIPELINE", tmp_path / "pipeline")
    monkeypatch.setattr(apply_cli, "CLEAN", tmp_path / "clean.parquet")
    (tmp_path / "profile").mkdir()
    (tmp_path / "profile" / "automation.yaml").write_text(yaml.safe_dump(
        {"score_threshold": 80, "table_path": "sheet.xlsx"}), encoding="utf-8")
    pd.DataFrame([{"job_id": j, "fit_score": s} for j, s in
                  (("aaaa1111", 90), ("bbbb2222", 85), ("cccc3333", 70))]
                 ).to_parquet(tmp_path / "scored.parquet")
    pd.DataFrame([{"job_id": j, "company": "Acme", "title": "Eng", "source": "greenhouse",
                   "url": GH} for j in ("aaaa1111", "bbbb2222", "cccc3333")]
                 ).to_parquet(tmp_path / "clean.parquet")


def _state(tmp_path, job_id, state, folder=None):
    d = tmp_path / "pipeline" / job_id
    d.mkdir(parents=True)
    (d / "state.yaml").write_text(yaml.safe_dump({
        "job_id": job_id, "state": state, "url": GH,
        "tailored_dirs": [folder] if folder else []}), encoding="utf-8")


def _rows(tmp_path):
    ws = openpyxl.load_workbook(tmp_path / "sheet.xlsx").active
    header = [c.value for c in ws[1]]
    return {r[1]: dict(zip(header, r)) for r in ws.iter_rows(min_row=2, values_only=True)}


def test_add_appends_only_roles_above_the_threshold_and_not_sent(tmp_path, monkeypatch):
    _world(tmp_path, monkeypatch)
    ledger.append(paths.APPLICATIONS / "apply_runs" / ledger.LEDGER_NAME, "bbbb2222", "accepted")
    sheet.refresh(add=True)
    assert set(_rows(tmp_path)) == {"aaaa1111"}


def test_status_follows_the_records(tmp_path, monkeypatch):
    _world(tmp_path, monkeypatch)
    folder = "lane/2026-09-28_acme_eng_aaaa1111"
    (paths.APPLICATIONS / folder).mkdir(parents=True)
    _state(tmp_path, "aaaa1111", "tailored", folder)
    sheet.refresh(add=True)
    assert _rows(tmp_path)["aaaa1111"]["status"] == "Resume ready"

    runs = paths.APPLICATIONS / "apply_runs"
    (runs).mkdir(parents=True, exist_ok=True)
    (runs / "2026-09-28T10-00-00.md").write_text(
        "# apply run\n\n## Ready (not submitted) (1)\n\n- `aaaa1111` Acme — Eng\n"
        "  - every field resolved\n", encoding="utf-8")
    sheet.refresh()
    row = _rows(tmp_path)["aaaa1111"]
    assert row["status"] == "Submittable" and "--submit" in row["uv_run_command"]

    ledger.append(runs / ledger.LEDGER_NAME, "aaaa1111", "accepted", at="2026-09-28T11:00:00")
    sheet.refresh()
    assert _rows(tmp_path)["aaaa1111"]["status"] == "Applied"


def test_answers_waiting_for_the_user(tmp_path, monkeypatch):
    import json
    _world(tmp_path, monkeypatch)
    folder = "lane/2026-09-28_acme_eng_aaaa1111"
    (paths.APPLICATIONS / folder).mkdir(parents=True)
    (paths.APPLICATIONS / folder / "answers_override.json").write_text(
        json.dumps({"job_id": "aaaa1111", "q": {"value": "x"}}), encoding="utf-8")
    _state(tmp_path, "aaaa1111", "tailored", folder)
    sheet.refresh(add=True)
    assert _rows(tmp_path)["aaaa1111"]["status"] == "Needs your OK"


def test_confirmed_answers_are_not_waiting(tmp_path, monkeypatch):
    import json
    _world(tmp_path, monkeypatch)
    folder = "lane/2026-09-28_acme_eng_aaaa1111"
    (paths.APPLICATIONS / folder).mkdir(parents=True)
    (paths.APPLICATIONS / folder / "answers_override.json").write_text(
        json.dumps({"job_id": "aaaa1111", "confirmed": True, "q": {"value": "x"}}),
        encoding="utf-8")
    _state(tmp_path, "aaaa1111", "tailored", folder)
    sheet.refresh(add=True)
    assert _rows(tmp_path)["aaaa1111"]["status"] == "Resume ready"
