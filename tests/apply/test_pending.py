"""apply prose: the judge's written answers, reviewed before they join the bank."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from applyourself import apply_cli
from applyourself.apply import pending
from applyourself.apply.bank_schema import load_answers
from applyourself.shared import paths

FIXTURES = Path(__file__).parent / "fixtures"
TWO = "I built a widget pipeline. It cut setup time in half."


def _role(name: str, entries: dict) -> Path:
    folder = paths.APPLICATIONS / "lane" / name
    folder.mkdir(parents=True)
    (folder / "answers_override.json").write_text(
        json.dumps({"job_id": name, **entries}), encoding="utf-8"
    )
    return folder


def _text(value=TWO, basis=("B-1",), label="Tell us about a project you are proud of"):
    return {"decision": "text", "value": value, "basis": list(basis), "label": label}


@pytest.fixture
def bank_path(tmp_path):
    path = tmp_path / "bank.yaml"
    shutil.copy(FIXTURES / "application_answers.yaml", path)
    return path


def test_only_multi_sentence_profile_prose_without_a_question_is_listed(bank_path):
    _role(
        "r1",
        {
            "a": _text(),
            "b": _text(value="One sentence only.", label="Short"),
            "c": _text(basis=("B-1", "jd"), label="Rests on the posting"),
            "d": _text(label="LinkedIn"),  # the bank already holds `linkedin`
            "e": {"value": TWO, "source": "B-1", "label": "Not judged"},
        },
    )
    _role("r2", {"x": _text()})
    rows = pending.collect(load_answers(bank_path))
    assert [(r["question"], r["forms"], r["source"]) for r in rows] == [
        ("tell_us_about_a_project_you_are_proud_of", 2, "B-1")
    ]


def test_a_decision_survives_a_new_review_and_saves_on_a_typed_yes(
    bank_path, tmp_path, monkeypatch, capsys
):
    _role("r1", {"a": _text()})
    out = tmp_path / "review.yaml"
    assert apply_cli.main(["prose", "--review", "--out", str(out), "--path", str(bank_path)]) == 0
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    data["prose"][0].update(decision="yes", question="proud_project")
    out.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert apply_cli.main(["prose", "--review", "--out", str(out), "--path", str(bank_path)]) == 0
    row = yaml.safe_load(out.read_text(encoding="utf-8"))["prose"][0]
    assert (row["decision"], row["question"]) == ("yes", "proud_project")

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    assert apply_cli.main(["prose", "--save", str(out), "--path", str(bank_path)]) == 0
    saved = yaml.safe_load(bank_path.read_text(encoding="utf-8"))["questions"]["proud_project"]
    assert saved == {"answer": TWO, "source": "B-1"}


@pytest.mark.parametrize("unattended", [True, False])
def test_save_is_refused_unattended_or_without_a_terminal(
    bank_path, tmp_path, monkeypatch, unattended
):
    out = tmp_path / "review.yaml"
    row = {"label": "L", "question": "q_new", "answer": TWO, "source": "B-1", "decision": "yes"}
    out.write_text(yaml.safe_dump({"prose": [row]}), encoding="utf-8")
    before = bank_path.read_text(encoding="utf-8")
    if unattended:
        monkeypatch.setenv("APPLY_UNATTENDED", "1")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    assert apply_cli.main(["prose", "--save", str(out), "--path", str(bank_path)]) == 1
    assert bank_path.read_text(encoding="utf-8") == before


def test_a_yes_row_with_a_non_profile_source_is_refused(tmp_path):
    out = tmp_path / "review.yaml"
    row = {"label": "L", "question": "q", "answer": TWO, "source": "jd", "decision": "yes"}
    out.write_text(yaml.safe_dump({"prose": [row]}), encoding="utf-8")
    with pytest.raises(pending.PendingError, match="B-/S-/SKILL-"):
        pending.to_save(out)
