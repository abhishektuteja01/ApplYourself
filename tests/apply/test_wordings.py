"""`apply wordings`: review the parked work-authorization wordings, save the approved ones."""

import json
import shutil

import pytest
import yaml

from src import apply_cli
from src.shared import paths

from .conftest import FIXTURES

NEW = "Are you authorized to work in the US for any employer?"
SAVED = "Are you legally authorized to work in the United States?"


@pytest.fixture
def bank_path(tmp_path, monkeypatch):
    path = tmp_path / "bank.yaml"
    shutil.copy(FIXTURES / "application_answers.yaml", path)
    monkeypatch.setitem(paths._overrides, "ANSWER_BANK", path)
    return path


def _plan(job_id: str, *labels: str) -> None:
    folder = paths.APPLICATIONS / "lane" / f"2026-01-01_x_{job_id}"
    folder.mkdir(parents=True)
    fields = [
        {"id": f"q{i}", "label": label, "kind": "select", "section": "questions",
         "required": True, "options": ["Yes", "No"]}
        for i, label in enumerate(labels)
    ]  # fmt: skip
    plan = {"job_id": job_id, "work_authorization": {"job_country": "United States"}}
    (folder / "plan.json").write_text(json.dumps({**plan, "unmapped": fields}), encoding="utf-8")


def _rows(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))["wordings"]


def test_review_lists_new_wordings_and_keeps_earlier_decisions(bank_path, tmp_path):
    _plan("aaaa1111", NEW, SAVED)
    _plan("bbbb2222", NEW)
    out = tmp_path / "review.yaml"
    assert apply_cli.main(["wordings", "--review", "--out", str(out)]) == 0
    assert _rows(out) == [
        {"label": NEW, "forms": 2, "fact": "authorized", "answer": "Yes", "decision": ""}
    ]
    text = out.read_text(encoding="utf-8").replace("decision: ''", "decision: no")
    out.write_text(text, encoding="utf-8")
    assert apply_cli.main(["wordings", "--review", "--out", str(out)]) == 0
    assert _rows(out)[0]["decision"] == "no"


def test_save_needs_a_tty_and_saves_only_yes_rows(bank_path, tmp_path, monkeypatch):
    _plan("aaaa1111", NEW, "Will you now or in the future require sponsorship to work in the US?")
    out = tmp_path / "review.yaml"
    assert apply_cli.main(["wordings", "--review", "--out", str(out)]) == 0
    rows = _rows(out)
    rows[0]["decision"], rows[1]["decision"] = True, "no"  # a bare yes reads as a bool
    out.write_text(yaml.safe_dump({"wordings": rows}), encoding="utf-8")
    before = bank_path.read_text(encoding="utf-8")

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("must not prompt"))
    assert apply_cli.main(["wordings", "--save", str(out)]) == 1
    assert bank_path.read_text(encoding="utf-8") == before

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    assert apply_cli.main(["wordings", "--save", str(out)]) == 0
    judged = yaml.safe_load(bank_path.read_text(encoding="utf-8"))["fact_wordings"]
    assert judged[rows[0]["label"]] == {"fact": rows[0]["fact"], "yes_when": True}
    assert rows[1]["label"] not in judged
    assert list((tmp_path / ".state" / "bank_history").glob("bank.*.yaml"))

    assert apply_cli.main(["wordings", "--review", "--out", str(out)]) == 0
    assert [r["label"] for r in _rows(out)] == [rows[1]["label"]]
