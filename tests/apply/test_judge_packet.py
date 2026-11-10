"""Edge cases of the apply-judge's packet checks and override merge."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from applyourself.apply import judge_cli, report
from applyourself.apply.bank_schema import load_answers
from applyourself.apply.judge_packet import (
    JudgeEntry,
    build_packet,
    check_entries,
    fields_for_judge,
    to_override,
)
from applyourself.apply.plan import build_plan
from applyourself.apply.reconcile import MergedField, MergedOption, Reconciled

BANK = Path(__file__).parent / "fixtures" / "application_answers.yaml"

FIELDS = [
    {"id": "f1", "label": "LinkedIn", "kind": "text", "options": [], "description": "",
     "required": True, "multi": False},
    {"id": "f2", "label": "Pick a city", "kind": "select", "options": ["Austin", "Boston"],
     "description": "", "required": True, "multi": False},
    {"id": "f3", "label": "Languages", "kind": "checkbox_group",
     "options": ["Python", "Go", "Rust"], "description": "", "required": False, "multi": True},
]  # fmt: skip


@pytest.fixture
def answers():
    return replace(load_answers(BANK), facts={"F-REMOTE": "Remote only."})


def _ok():
    return [
        {"field_id": "f1", "decision": "question", "question": "linkedin", "why": "bank"},
        {"field_id": "f2", "decision": "option", "value": "Austin", "basis": ["F-REMOTE"],
         "why": "fact"},
        {"field_id": "f3", "decision": "option", "value": ["Python", "Go"], "basis": ["SKILL-PY"],
         "why": "skills"},
    ]  # fmt: skip


def test_good_file_passes(answers):
    assert check_entries(_ok(), FIELDS, answers) == []


def test_wrong_field_id_set(answers):
    entries = _ok()[:2] + [{**_ok()[0], "field_id": "zz"}]
    [err] = check_entries(entries, FIELDS, answers)
    assert "missing ['f3']" in err and "extra ['zz']" in err


def test_duplicate_field_id(answers):
    entries = _ok() + [_ok()[0]]
    assert any("answered twice" in e for e in check_entries(entries, FIELDS, answers))


def test_extra_key_and_key_of_another_decision(answers):
    entries = _ok()
    entries[0]["confidence"] = 0.9
    entries[1]["ask"] = "which city?"
    errs = check_entries(entries, FIELDS, answers)
    assert any("f1:" in e and "confidence" in e for e in errs)
    assert any("f2: option takes no ask" in e for e in errs)


def test_null_counts_as_present(answers):
    entries = _ok()
    entries[0]["value"] = None
    assert check_entries(entries, FIELDS, answers) == ["f1: question takes no value"]


def test_option_not_verbatim(answers):
    entries = _ok()
    entries[1]["value"] = "austin"
    assert any("not among" in e for e in check_entries(entries, FIELDS, answers))


def test_multi_select_partial_mismatch(answers):
    entries = _ok()
    entries[2]["value"] = ["Python", "Java"]
    [err] = check_entries(entries, FIELDS, answers)
    assert "['Java']" in err


def test_single_valued_option_takes_a_string(answers):
    entries = _ok()
    entries[1]["value"] = ["Austin"]
    assert any("one string" in e for e in check_entries(entries, FIELDS, answers))


def test_unknown_question_and_fact_ids(answers):
    entries = _ok()
    entries[0]["question"] = "no_such_question"
    entries[1]["basis"] = ["F-NOPE"]
    errs = check_entries(entries, FIELDS, answers)
    assert any("'no_such_question' is not in the bank" in e for e in errs)
    assert any("'F-NOPE' is not in the bank's facts" in e for e in errs)


def test_option_needs_basis(answers):
    entries = _ok()
    entries[1]["basis"] = []
    assert any("basis needs" in e for e in check_entries(entries, FIELDS, answers))


def test_text_on_a_select_refused(answers):
    entries = _ok()
    entries[1]["decision"] = "text"
    assert any("use option" in e for e in check_entries(entries, FIELDS, answers))


def test_fact_must_be_a_known_fact(answers):
    entries = _ok()
    entries[0] = {"field_id": "f1", "decision": "fact", "why": "x",
                  "fact": {"fact": "citizen", "yes_when": True}}  # fmt: skip
    assert any("f1: fact.fact" in e for e in check_entries(entries, FIELDS, answers))


def test_to_override_shapes():
    def e(**kw):
        return JudgeEntry.model_validate({"field_id": "f", "why": "w", **kw})

    assert to_override(e(decision="question", question="q"), "L") == {
        "decision": "question", "question": "q", "label": "L"}  # fmt: skip
    assert to_override(e(decision="fact", fact={"fact": "authorized", "yes_when": False}), "L") == {
        "decision": "fact", "fact": {"fact": "authorized", "yes_when": False}, "label": "L"}  # fmt: skip
    assert to_override(e(decision="text", value="v", basis=["B-1", "S-2"]), "L") == {
        "decision": "text", "value": "v", "basis": ["B-1", "S-2"], "source": "B-1", "label": "L"}  # fmt: skip
    assert to_override(e(decision="ask", ask="a?"), "L") == {
        "decision": "ask", "ask": "a?", "label": "L"}  # fmt: skip
    assert to_override(e(decision="skip"), "L") == {"decision": "skip", "label": "L"}


def test_fields_for_judge_drops_structural_and_dupes():
    plan = {
        "fields": [
            {"id": "name", "tier": "A", "section": "questions", "kind": "text", "label": "Name"},
            {"id": "g", "tier": "A2", "section": "eeoc", "kind": "select", "label": "Gender"},
            {"id": "q", "tier": "B", "section": "questions", "kind": "yesno", "label": "Q",
             "options": ["Yes", "No"], "required": True, "value": "Yes"},
        ],
        "unmapped": [
            {"id": "resume", "tier": "A", "section": "questions", "kind": "file", "label": "CV"},
            {"id": "cl", "tier": "C", "section": "questions", "kind": "file", "label": "CL"},
            {"id": "q", "tier": "C", "section": "questions", "kind": "text", "label": "dupe"},
        ],
        "skipped": [{"id": "s", "tier": "C", "label": "Optional", "reason": "x"}],
    }  # fmt: skip
    got = fields_for_judge(plan)
    assert [f["id"] for f in got] == ["q", "s"]
    assert got[0]["options"] == ["Yes", "No"] and "value" not in got[0]


def test_a_skipped_optional_work_auth_select_reaches_the_judge_with_its_options(
    answers, tailor_dir
):
    f = MergedField(
        id="auth", name="auth", label="Will you now or in the future require visa sponsorship?",
        required=False, kind="select", section="questions", multi=False,
        options=(MergedOption(label="Yes"), MergedOption(label="No")),
    )  # fmt: skip
    plan = build_plan(Reconciled(fields=(f,), api_only=()), answers, tailor_dir)
    assert [(s.id, s.tier) for s in plan.skipped] == [("auth", "B0")]
    [got] = fields_for_judge(json.loads(json.dumps(report.as_dict(plan))))
    assert (got["kind"], got["options"], got["required"]) == ("select", ["Yes", "No"], False)


def test_packet_wrong_country_shows_default(answers):
    text = build_packet(FIELDS, answers, "Germany")
    assert "Germany: no row of its own" in text
    assert "- F-REMOTE: Remote only." in text
    assert "- salary: {computed: salary" in text


def test_merge_keeps_unrelated_entries_and_top_keys(tmp_path, monkeypatch, capsys):
    out_dir = tmp_path / "role"
    (out_dir / "judge").mkdir(parents=True)
    plan = {"work_authorization": {"job_country": "United States"},
            "unmapped": [{**f, "tier": "C", "section": "questions"} for f in FIELDS]}  # fmt: skip
    (out_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (out_dir / "answers_override.json").write_text(json.dumps({
        "job_id": "abc", "country": "United States", "confirmed": "keep",
        "other": {"value": "x", "source": "user"},
        "f1": {"value": "https://me", "source": "user"},
        "f2": {"value": "Boston", "source": "user"},
        "f3": {"decision": "skip", "label": "Languages"},
    }), encoding="utf-8")  # fmt: skip
    entries = _ok()
    entries[0] = {"field_id": "f1", "decision": "ask", "ask": "which URL?", "why": "w"}
    (out_dir / "judge" / "out.json").write_text(json.dumps(entries), encoding="utf-8")
    monkeypatch.setattr("applyourself.apply_cli.resolve_out_dir", lambda job_id: out_dir)
    monkeypatch.setattr(judge_cli, "load_answers", lambda: replace(
        load_answers(BANK), facts={"F-REMOTE": "Remote only."}))  # fmt: skip

    assert judge_cli.main(["judge-check", "abc"]) == 0
    got = json.loads((out_dir / "answers_override.json").read_text(encoding="utf-8"))
    assert got["job_id"] == "abc" and got["country"] == "United States"
    assert got["confirmed"] == "keep"
    assert got["other"] == {"value": "x", "source": "user"}
    assert got["f1"] == {"value": "https://me", "source": "user"}
    assert got["f2"] == {"value": "Boston", "source": "user"}  # never overwritten
    assert got["f3"]["decision"] == "option"  # an earlier judgment is replaced
    out = capsys.readouterr().out
    assert "kept your entry for f1" in out and "kept your entry for f2" in out


def test_check_refuses_and_keeps_rejected(tmp_path, monkeypatch, capsys):
    out_dir = tmp_path / "role"
    (out_dir / "judge").mkdir(parents=True)
    plan = {"unmapped": [{**FIELDS[0], "tier": "C", "section": "questions"}]}
    (out_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (out_dir / "judge" / "out.json").write_text(json.dumps([{"field_id": "f1"}]), encoding="utf-8")
    monkeypatch.setattr("applyourself.apply_cli.resolve_out_dir", lambda job_id: out_dir)
    monkeypatch.setattr(judge_cli, "load_answers", lambda: load_answers(BANK))

    assert judge_cli.main(["judge-check", "abc"]) == 1
    assert (out_dir / "judge" / "rejected_out.json").exists()
    assert not (out_dir / "answers_override.json").exists()
    assert "f1:" in capsys.readouterr().out


SENTENCES = {"id": "s1", "label": "Your U.S. person status?", "kind": "select",
             "options": ["I am a U.S. person", "None of the above"], "description": "",
             "required": True, "multi": False}  # fmt: skip


def test_a_fact_on_sentence_options_is_refused(answers):
    entry = {"field_id": "s1", "decision": "fact", "why": "w",
             "fact": {"fact": "us_person", "yes_when": True}}  # fmt: skip
    [err] = check_entries([entry], [SENTENCES], answers)
    assert "sentence options: use decision option" in err


def test_a_bank_question_id_is_a_basis(answers):
    entries = _ok()
    entries[1]["basis"] = ["linkedin"]
    assert check_entries(entries, FIELDS, answers) == []
    entries[1]["basis"] = ["not_a_question"]
    assert any("not_a_question" in e for e in check_entries(entries, FIELDS, answers))


def test_a_work_authorization_key_is_a_basis(answers):
    entries = _ok()
    entries[1]["basis"] = ["us_person", "work_authorization.requires_sponsorship"]
    assert check_entries(entries, FIELDS, answers) == []
    entries[1]["basis"] = ["work_authorization.salary"]
    assert any("work_authorization.salary" in e for e in check_entries(entries, FIELDS, answers))


def test_packet_lists_every_country_row(answers):
    text = build_packet(FIELDS, answers, "Germany")
    assert "- United States: authorized=true" in text
    assert "- default: authorized=false" in text
