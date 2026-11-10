"""The bank's one writer: a fact or an approved prose question, never an overwrite."""

from __future__ import annotations

import shutil

import pytest
import yaml

from applyourself.apply import bank
from applyourself.apply.bank import Lesson

from .conftest import FIXTURES


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "application_answers.yaml"
    shutil.copyfile(FIXTURES / "application_answers.yaml", p)
    text = p.read_text(encoding="utf-8")
    p.write_text(
        text + "facts:\n  # where I am\n  F-HOME: I live in Exampletown.\n", encoding="utf-8"
    )
    return p


def test_a_fact_and_a_prose_question_go_in_and_comments_stay(path):
    kept, refused = bank.learn(
        [
            Lesson("fact", "F-RELOCATE", "Willing to relocate? Yes", source="user"),
            Lesson("question", "why_widgets", "Widgets are my thing.", source="B-W-01, S-W-02"),
        ],
        path,
    )
    assert (len(kept), refused) == (2, [])
    text = path.read_text(encoding="utf-8")
    assert "# where I am" in text
    data = yaml.safe_load(text)
    assert data["facts"]["F-RELOCATE"] == "Willing to relocate? Yes"
    assert data["questions"]["why_widgets"] == {
        "answer": "Widgets are my thing.",
        "source": "B-W-01, S-W-02",
    }


def test_an_existing_id_is_refused_and_the_rest_stand(path):
    kept, refused = bank.learn(
        [
            Lesson("fact", "F-HOME", "I live elsewhere.", source="user"),
            Lesson("question", "pronouns", "He/Him", source="B-X"),
            Lesson("fact", "F-NEW", "A new fact.", source="user"),
        ],
        path,
    )
    assert [k.id for k in kept] == ["F-NEW"]
    assert len(refused) == 2 and all("undo it first" in r for r in refused)
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["facts"]["F-HOME"] == (
        "I live in Exampletown."
    )


@pytest.mark.parametrize(
    "lesson,needle",
    [
        (Lesson("fact", "F-bad", "x", source="user"), "fact id"),
        (Lesson("fact", "F-TWO", "one\ntwo", source="user"), "one line"),
        (Lesson("question", "why_x", "Because.", source="jd"), "profile ids"),
        (Lesson("question", "Why X", "Because.", source="B-X"), "question id"),
    ],
)
def test_a_malformed_lesson_is_refused(path, lesson, needle):
    before = path.read_text(encoding="utf-8")
    kept, refused = bank.learn([lesson], path)
    assert not kept and needle in refused[0]
    assert path.read_text(encoding="utf-8") == before


def test_an_unattended_run_writes_nothing(path, monkeypatch):
    monkeypatch.setenv(bank.UNATTENDED_ENV, "1")
    before = path.read_text(encoding="utf-8")
    with pytest.raises(bank.BankError, match="unattended"):
        bank.learn([Lesson("fact", "F-NEW", "x.", source="user")], path)
    assert path.read_text(encoding="utf-8") == before


def test_undo_removes_one_fact_or_question_and_keeps_history(path):
    assert bank.undo("F-HOME", path) == "removed fact F-HOME"
    assert bank.undo("pronouns", path) == "removed question pronouns"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "pronouns" not in data["questions"] and not data.get("facts")
    assert len(list((path.parent / ".state" / "bank_history").glob("*.yaml"))) == 2
    with pytest.raises(bank.BankError, match="no fact"):
        bank.undo("F-HOME", path)
