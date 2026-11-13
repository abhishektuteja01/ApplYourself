"""The status sheet is derived from state, ledger and run reports — never typed."""

from __future__ import annotations

import openpyxl
import pandas as pd
import yaml

from applyourself.apply import ledger, sheet
from applyourself.shared import paths
from tests.factories import scored_rows, write_clean, write_state

GH = "https://job-boards.greenhouse.io/acme/jobs/1"


def _world(tmp_path, monkeypatch):
    monkeypatch.setitem(paths._overrides, "SCORED", tmp_path / "scored.parquet")
    monkeypatch.setitem(paths._overrides, "CLEAN", tmp_path / "clean.parquet")
    (tmp_path / "profile").mkdir()
    (tmp_path / "profile" / "settings.yaml").write_text(
        yaml.safe_dump({"table_path": "sheet.xlsx"}), encoding="utf-8"
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


def test_a_derived_skip_keeps_a_new_role_out_but_not_a_legacy_one_or_one_with_a_resume(
    tmp_path, monkeypatch
):
    _world(tmp_path, monkeypatch)
    scores = pd.DataFrame(
        {
            "job_id": ["aaaa1111", "bbbb2222", "cccc3333"],
            "vertical": ["example_primary"] * 3,
            "fit_score": [90.0, 90.0, 90.0],
            "suggested_action": ["skip"] * 3,
            "basis": ["judged", "judged", "legacy"],
        }
    )
    monkeypatch.setattr(sheet, "view", lambda _path: scores)
    folder = "lane/2026-09-28_acme_eng_bbbb2222"
    (paths.APPLICATIONS / folder).mkdir(parents=True)
    _state(tmp_path, "aaaa1111", "saved")
    _state(tmp_path, "bbbb2222", "saved", folder)
    assert [r["name"] for r in sheet.queue()] == ["bbbb2222", "cccc3333"]


def test_each_lane_has_its_own_line_and_an_unknown_lane_the_lowest(tmp_path, monkeypatch):
    _world(tmp_path, monkeypatch)  # fixture lines: primary 80, secondary 78
    scores = pd.DataFrame(
        {
            "job_id": ["aaaa1111", "bbbb2222", "cccc3333"],
            "vertical": ["example_primary", "example_secondary", "retired_lane"],
            "fit_score": [79.0, 79.0, 78.0],
            "suggested_action": ["tailor"] * 3,
            "basis": ["judged"] * 3,
        }
    )
    monkeypatch.setattr(sheet, "view", lambda _path: scores)
    assert [r["name"] for r in sheet.queue()] == ["bbbb2222", "cccc3333"]


def test_below_the_lane_line_only_a_submittable_role_joins_from_the_lower_line(
    tmp_path, monkeypatch
):
    _world(tmp_path, monkeypatch)  # fixture line: primary 80
    (tmp_path / "profile" / "settings.yaml").write_text(
        yaml.safe_dump({"table_path": "sheet.xlsx", "sheet_submittable_cutoff": 72}),
        encoding="utf-8",
    )
    write_clean(
        tmp_path / "clean.parquet",
        *(
            {"job_id": j, "company": "Acme", "title": "Eng", "source": "manual", "url": u}
            for j, u in (
                ("aaaa1111", GH),
                ("bbbb2222", "https://acme.example/careers/1"),
                ("cccc3333", GH),
            )
        ),
    )
    scores = pd.DataFrame(
        {
            "job_id": ["aaaa1111", "bbbb2222", "cccc3333"],
            "vertical": ["example_primary"] * 3,
            "fit_score": [75.0, 75.0, 71.0],
            "suggested_action": ["tailor"] * 3,
            "basis": ["judged"] * 3,
        }
    )
    monkeypatch.setattr(sheet, "view", lambda _path: scores)
    assert [r["name"] for r in sheet.queue()] == ["aaaa1111"]


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
    from applyourself.apply import confirmation

    data = {"job_id": "aaaa1111", "q": {"value": "x"}}
    answers = paths.APPLICATIONS / folder / "answers_override.json"
    answers.write_text(json.dumps(data), encoding="utf-8")
    confirmation.record(answers, data)
    _state(tmp_path, "aaaa1111", "tailored", folder)
    sheet.refresh(add=True)
    assert _rows(tmp_path)["aaaa1111"]["status"] == "Resume ready"


def test_job_ids_are_computed_without_the_sheet_file(tmp_path, monkeypatch, capsys):
    from applyourself import apply_cli

    _world(tmp_path, monkeypatch)
    assert sheet.job_ids("New") == ["aaaa1111", "bbbb2222"]
    assert not (tmp_path / "sheet.xlsx").exists()
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


def test_no_employer_parks_only_a_role_with_no_resume_yet():
    parked = sheet.status_for("aaaa1111", True, {}, None, None, no_employer=True)
    assert parked[0] == sheet.NO_EMPLOYER and "--company" in parked[1]
    built = {"tailored_dirs": ["lane/x"]}
    assert sheet.status_for("aaaa1111", True, built, None, None, no_employer=True)[0] != (
        sheet.NO_EMPLOYER
    )


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


def test_a_run_record_drives_the_sheet_and_renders_the_report(tmp_path, monkeypatch):
    import json
    from datetime import datetime

    from applyourself.apply_cli import RunOutcome, write_report

    _world(tmp_path, monkeypatch)
    folder = "lane/2026-09-28_acme_eng_aaaa1111"
    (paths.APPLICATIONS / folder).mkdir(parents=True)
    _state(tmp_path, "aaaa1111", "tailored", folder)
    row = {"id": "q1", "label": "Why us?", "tier": "C", "reason": "new", "source": ""}
    md = write_report(
        [RunOutcome("aaaa1111", "Acme", "Eng", "parked", unmapped=(row,), reason="unresolved")],
        datetime(2026, 9, 28, 10),
        out_dir=paths.APPLICATIONS / "apply_runs",
    )
    record = json.loads(md.with_suffix(".json").read_text(encoding="utf-8"))
    (role,) = record["roles"]
    assert role["reason"] == "unresolved" and role["fields_parked"][0]["kind"] == "new question"
    assert "## Parked (1)" in md.read_text(encoding="utf-8")
    md.unlink()  # the sheet reads the JSON, not the markdown
    sheet.refresh(add=True)
    assert _rows(tmp_path)["aaaa1111"]["note"] == "`q1` [new question] Why us?"


def test_run_backfill_dry_run_writes_nothing_and_apply_keeps_the_sheet(tmp_path, monkeypatch):
    from applyourself.apply import run_backfill

    _world(tmp_path, monkeypatch)
    for job in ("aaaa1111", "bbbb2222"):
        folder = f"lane/2026-09-28_acme_eng_{job}"
        (paths.APPLICATIONS / folder).mkdir(parents=True)
        _state(tmp_path, job, "tailored", folder)
    sheet.refresh(add=True)
    runs = paths.APPLICATIONS / "apply_runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "2026-09-28T10-00-00.md").write_text(
        "# apply run — 2026-09-28T10:00:00\n\n## Failed (1)\n\n- `aaaa1111` Acme — Eng\n"
        "  - TimeoutError: boom\nCall log:\n  - waiting for locator\n\n"
        "## Parked (1)\n\n- `bbbb2222` Acme — Eng\n  - `q1` [needs a fact] Salary?\n"
        "    - salary: no figure\n",
        encoding="utf-8",
    )
    (runs / "notes.md").write_text("not a report\n", encoding="utf-8")
    before = sheet.statuses(sheet.latest_outcomes(runs))
    assert run_backfill.main(["--dry-run", "--runs", str(runs)]) == 0
    assert not list(runs.glob("*.json"))
    assert run_backfill.main(["--apply", "--runs", str(runs)]) == 0
    (runs / "2026-09-28T10-00-00.md").unlink()
    assert sheet.statuses(sheet.latest_outcomes(runs)) == before
    assert before["aaaa1111"][1] == "failed: TimeoutError: boom"


def test_compare_names_rows_the_queue_does_not_hold_and_refresh_drops_them(
    tmp_path, monkeypatch, capsys
):
    from applyourself import apply_cli

    _world(tmp_path, monkeypatch)
    sheet.refresh()
    wb = openpyxl.load_workbook(tmp_path / "sheet.xlsx")
    wb.active.append([70, "cccc3333", "Acme", "Eng", "greenhouse", GH, "New", "", ""])
    wb.save(tmp_path / "sheet.xlsx")
    assert apply_cli.main(["sheet", "--compare"]) == 1
    assert "extra 1: cccc3333" in capsys.readouterr().out
    assert sheet.refresh() == {"New": 2, "New (removed)": 1}
    assert list(_rows(tmp_path)) == ["aaaa1111", "bbbb2222"]
    assert apply_cli.main(["sheet", "--compare"]) == 0
    assert sorted(p.name for p in tmp_path.glob("*sheet.xlsx*")) == [
        ".sheet.xlsx.lock",
        "sheet.xlsx",
    ]


def test_a_refresh_reads_settings_and_the_ledger_once(tmp_path, monkeypatch):
    from applyourself.shared import config_schema

    _world(tmp_path, monkeypatch)
    ledger.append(paths.APPLICATIONS / "apply_runs" / ledger.LEDGER_NAME, "cccc3333", "accepted")
    calls = {"settings": 0, "ledger": 0}

    def counted(name, real):
        def wrapper(*a, **k):
            calls[name] += 1
            return real(*a, **k)

        return wrapper

    monkeypatch.setattr(sheet, "load_settings", counted("settings", sheet.load_settings))
    monkeypatch.setattr(
        config_schema, "load_settings", counted("settings", config_schema.load_settings)
    )
    monkeypatch.setattr(ledger, "records", counted("ledger", ledger.records))
    sheet.refresh()
    assert len(_rows(tmp_path)) == 2
    assert calls == {"settings": 1, "ledger": 1}


def test_trend_keeps_only_runs_sharing_a_role(tmp_path):
    from applyourself.apply import run_record

    totals = {"parked": 0, "roles": 1, "bank": 0, "learned": 0}
    for stem, job_id in (("2026-11-01_0900", "aaaa1111"), ("2026-11-02_0900", "bbbb2222")):
        record = {"totals": totals, "roles": [{"job_id": job_id}]}
        run_record.write(record, tmp_path / f"{stem}.json")
    rows = run_record.trend(tmp_path, ["aaaa1111"])
    assert len(rows) == 1 and "2026-11-01_0900" in rows[0]
