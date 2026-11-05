"""The status sheet is derived from state, ledger and run reports — never typed."""

from __future__ import annotations

import openpyxl
import yaml

from src.apply import ledger, sheet
from src.shared import paths
from tests.factories import scored_rows, write_clean, write_state

GH = "https://job-boards.greenhouse.io/acme/jobs/1"


def _world(tmp_path, monkeypatch):
    for name, value in (
        ("REPO_ROOT", tmp_path),
        ("PROFILE", tmp_path / "profile"),
        ("PIPELINE", tmp_path / "pipeline"),
        ("SCORED", tmp_path / "scored.parquet"),
        ("CLEAN", tmp_path / "clean.parquet"),
    ):
        monkeypatch.setattr(paths, name, value)
    from src import apply_cli

    monkeypatch.setattr(apply_cli, "CLEAN", tmp_path / "clean.parquet")
    (tmp_path / "profile").mkdir()
    (tmp_path / "profile" / "automation.yaml").write_text(
        yaml.safe_dump({"table_path": "sheet.xlsx"}), encoding="utf-8"
    )
    cutoffs = {"review": 50, "tailor": 60, "keep_long": 70, "sheet": 80}
    (tmp_path / "profile" / "scoring.yaml").write_text(
        yaml.safe_dump({"cutoffs": cutoffs}), encoding="utf-8"
    )
    scored_rows(
        *(
            {"job_id": j, "fit_score": s}
            for j, s in (("aaaa1111", 90), ("bbbb2222", 85), ("cccc3333", 70))
        )
    ).to_parquet(tmp_path / "scored.parquet")
    write_clean(
        tmp_path / "clean.parquet",
        *(
            {"job_id": j, "company": "Acme", "title": "Eng", "source": "greenhouse", "url": GH}
            for j in ("aaaa1111", "bbbb2222", "cccc3333")
        ),
    )


def _state(tmp_path, job_id, state, folder=None):
    write_state(
        tmp_path / "pipeline", job_id, state, url=GH, tailored_dirs=[folder] if folder else []
    )


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
        "  - every field resolved\n",
        encoding="utf-8",
    )
    sheet.refresh()
    row = _rows(tmp_path)["aaaa1111"]
    assert row["status"] == "Submittable" and "--submit" in row["uv_run_command"]

    ledger.append(runs / ledger.LEDGER_NAME, "aaaa1111", "accepted", at="2026-09-28T11:00:00")
    counts = sheet.refresh()
    assert "aaaa1111" not in _rows(tmp_path) and counts["Applied (removed)"] == 1
    sheet.refresh(add=True)
    assert "aaaa1111" not in _rows(tmp_path)


def test_answers_waiting_for_the_user(tmp_path, monkeypatch):
    import json

    _world(tmp_path, monkeypatch)
    folder = "lane/2026-09-28_acme_eng_aaaa1111"
    (paths.APPLICATIONS / folder).mkdir(parents=True)
    (paths.APPLICATIONS / folder / "answers_override.json").write_text(
        json.dumps({"job_id": "aaaa1111", "q": {"value": "x"}}), encoding="utf-8"
    )
    _state(tmp_path, "aaaa1111", "tailored", folder)
    sheet.refresh(add=True)
    assert _rows(tmp_path)["aaaa1111"]["status"] == "Needs your OK"


def test_confirmed_answers_are_not_waiting(tmp_path, monkeypatch):
    import json

    _world(tmp_path, monkeypatch)
    folder = "lane/2026-09-28_acme_eng_aaaa1111"
    (paths.APPLICATIONS / folder).mkdir(parents=True)
    from src.apply import confirmation

    data = {"job_id": "aaaa1111", "q": {"value": "x"}}
    answers = paths.APPLICATIONS / folder / "answers_override.json"
    answers.write_text(json.dumps(data), encoding="utf-8")
    confirmation.record(answers, data)
    _state(tmp_path, "aaaa1111", "tailored", folder)
    sheet.refresh(add=True)
    assert _rows(tmp_path)["aaaa1111"]["status"] == "Resume ready"


def test_job_ids_lists_one_status_in_sheet_order(tmp_path, monkeypatch, capsys):
    from src import apply_cli

    _world(tmp_path, monkeypatch)
    assert sheet.job_ids("New") == []
    sheet.refresh(add=True)
    folder = "lane/2026-09-28_acme_eng_aaaa1111"
    (paths.APPLICATIONS / folder).mkdir(parents=True)
    _state(tmp_path, "aaaa1111", "tailored", folder)
    sheet.refresh()
    assert sheet.job_ids("Resume ready") == ["aaaa1111"]
    assert sheet.job_ids("New") == ["bbbb2222"]
    assert apply_cli.main(["sheet", "--list", "New"]) == 0
    assert capsys.readouterr().out.split() == ["bbbb2222"]


def test_closed_roles_leave_the_sheet_and_stay_out(tmp_path, monkeypatch):
    _world(tmp_path, monkeypatch)
    sheet.refresh(add=True)
    _state(tmp_path, "aaaa1111", "skip")
    counts = sheet.refresh()
    assert set(_rows(tmp_path)) == {"bbbb2222"} and counts["Closed: skip (removed)"] == 1
    sheet.refresh(add=True)
    assert set(_rows(tmp_path)) == {"bbbb2222"}


def test_an_expired_role_stays_with_an_expired_note(tmp_path, monkeypatch):
    _world(tmp_path, monkeypatch)
    folder = "lane/2026-09-28_acme_eng_aaaa1111"
    (paths.APPLICATIONS / folder).mkdir(parents=True)
    _state(tmp_path, "aaaa1111", "tailored", folder)
    runs = paths.APPLICATIONS / "apply_runs"
    runs.mkdir(parents=True)
    (runs / "2026-09-28T10-00-00.md").write_text(
        "# apply run\n\n## Expired postings (1)\n\n- `aaaa1111` Acme — Eng\n"
        "  - the posting is gone\n",
        encoding="utf-8",
    )
    sheet.refresh(add=True)
    row = _rows(tmp_path)["aaaa1111"]
    assert row["status"] == "Expired" and row["note"].startswith("expired")


def test_check_reports_what_is_wrong_and_writes_nothing(tmp_path, monkeypatch):
    _world(tmp_path, monkeypatch)
    assert sheet.check() == {"finished": [], "duplicates": [], "not_live": [], "wrong_status": []}
    sheet.refresh(add=True)
    assert sheet.check() == {"finished": [], "duplicates": [], "not_live": [], "wrong_status": []}
    write_clean(tmp_path / "clean.parquet", {"job_id": "bbbb2222", "url": ""})
    wb = openpyxl.load_workbook(tmp_path / "sheet.xlsx")
    ws = wb.active
    header = [c.value for c in ws[1]]
    ws.append([70, "aaaa1111"] + [""] * (len(header) - 2))
    ws.cell(row=ws.max_row, column=header.index("status") + 1, value="Applied")
    wb.save(tmp_path / "sheet.xlsx")
    got = sheet.check()
    assert got["finished"] == ["aaaa1111"] and got["duplicates"] == ["aaaa1111"]
    assert got["not_live"] == ["aaaa1111", "aaaa1111"]
    assert got["wrong_status"] == ["aaaa1111"]


def test_unsubmittable_role_is_new_until_it_has_a_resume():
    assert sheet.status_for("aaaa1111", False, {}, None, None)[0] == "New"
    built = {"tailored_dirs": ["lane/x"]}
    assert sheet.status_for("aaaa1111", False, built, None, None)[0] == "Manual apply"


def test_a_ledger_line_under_another_job_id_for_the_same_form_counts_as_sent(tmp_path, monkeypatch):
    import json

    _world(tmp_path, monkeypatch)
    folder = "lane/2026-09-28_acme_eng_aaaa1111"
    (paths.APPLICATIONS / folder).mkdir(parents=True)
    (paths.APPLICATIONS / folder / "answers_override.json").write_text(
        json.dumps({"job_id": "aaaa1111", "q": {"value": "x"}}), encoding="utf-8"
    )
    (paths.APPLICATIONS / folder / "plan.json").write_text(
        json.dumps({"form_url": "https://jobs.lever.co/acme/1/apply"}), encoding="utf-8"
    )
    _state(tmp_path, "aaaa1111", "tailored", folder)
    sheet.refresh(add=True)
    assert _rows(tmp_path)["aaaa1111"]["status"] == "Needs your OK"
    ledger.append(
        paths.APPLICATIONS / "apply_runs" / ledger.LEDGER_NAME,
        "zzzz9999",
        "backfill",
        form_url="https://jobs.lever.co/acme/1/apply",
    )
    counts = sheet.refresh()
    assert "aaaa1111" not in _rows(tmp_path) and counts["Applied (removed)"] == 1
