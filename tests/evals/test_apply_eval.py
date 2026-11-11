"""The apply eval grader: renders judge entries the way plan does, then compares."""

from __future__ import annotations

import json
from dataclasses import replace

from applyourself.apply.bank_schema import Question
from applyourself.apply.judge_packet import JudgeEntry
from applyourself.apply.plan import Override
from applyourself.evals import apply_eval as E
from applyourself.evals.cases import Case

PERSONA = "opt_student"
US = "United States"


def case(cid: str, field: dict, expect: dict, group: str = "rewording") -> Case:
    return Case.model_validate(
        {
            "id": cid,
            "group": group,
            "persona": PERSONA,
            "job_country": US,
            "field": field,
            "expect": expect,
        }
    )


def entry(cid: str, **kw) -> JudgeEntry:
    return JudgeEntry.model_validate({"field_id": cid, "why": "w", **kw})


def grade(c: Case, e: JudgeEntry, override: Override, answers=None) -> dict:
    answers = answers or E.persona_answers(PERSONA, US)
    return E.grade_case(c, e, override, answers, E.persona_known_ids(PERSONA), [])


TEXT = {"label": "LinkedIn", "kind": "text"}
YES_NO = {"label": "Will you relocate?", "kind": "radio_group", "options": ["Yes", "No"]}


def test_a_different_question_that_renders_the_same_value_passes():
    answers = E.persona_answers(PERSONA, US)
    linkedin = answers.question("linkedin")
    assert linkedin is not None
    answers = replace(
        answers, questions=answers.questions + (Question(id="li_dup", answers=linkedin.answers),)
    )
    c = case("c1", TEXT, {"action": "fill", "question": "linkedin"})
    e = entry("c1", decision="question", question="li_dup")
    row = grade(c, e, Override(value="", question="li_dup", decision="question"), answers)
    assert row["passed"], row


def test_ask_expected_but_judge_filled_fails():
    c = case("c2", YES_NO, {"action": "ask"}, group="must_ask")
    e = entry("c2", decision="option", value="Yes", basis=["F-RELOCATION"])
    ov = Override(value="Yes", source="F-RELOCATION", decision="option", basis=("F-RELOCATION",))
    row = grade(c, e, ov)
    assert not row["passed"] and row["got"]["action"] == "fill"


def test_prose_with_a_missing_basis_id_fails():
    c = case(
        "c3", {"label": "Describe a project", "kind": "textarea"}, {"action": "fill", "prose": True}
    )
    text = "Built a retrieval pipeline over support tickets with an offline eval set."

    def row(basis: str) -> dict:
        e = entry("c3", decision="text", value=text, basis=[basis])
        return grade(c, e, Override(value=text, source=basis, decision="text", basis=(basis,)))

    assert row("B-OPT-RAG-01")["passed"]  # a persona id, not the real profile's
    missed = row("B-NOT-THERE")
    assert not missed["passed"] and "B-NOT-THERE" in missed["got"]["reason"]


def test_check_entries_problems_fail_the_case(tmp_path):
    c = case("c4", YES_NO, {"action": "fill", "value": "Yes"})
    ok = case("c5", YES_NO, {"action": "fill", "value": "Yes"})
    meta = {
        "persona": PERSONA,
        "job_country": US,
        "cases": [x.model_dump(exclude_none=True) for x in (c, ok)],
    }
    out = [
        {"field_id": "c4", "decision": "option", "value": "Sure", "basis": ["option"], "why": "w"},
        {"field_id": "c5", "decision": "option", "value": "Yes", "basis": ["option"], "why": "w"},
    ]
    (tmp_path / "out_1.json").write_text(json.dumps(out), encoding="utf-8")
    rows = {r["id"]: r for r in E.grade_slice(tmp_path, 1, meta)}
    assert not rows["c4"]["passed"] and rows["c4"]["got"]["action"] == "invalid"
    assert rows["c5"]["passed"]
    (tmp_path / "meta_1.json").write_text(json.dumps(meta), encoding="utf-8")
    assert len(E.owed(tmp_path)) == 1  # one bad entry owes the whole slice


def test_gate_fails_at_99_percent_when_a_gate_group_misses_one():
    rows = [{"group": "rewording", "passed": True} for _ in range(99)]
    rows.append({"group": "flip_word", "passed": False})
    g = E.gate(rows)
    assert g["overall"] == 0.99 and not g["passed"]
    assert g["failed"] == ["flip_word 0.0% < 100%"]


def test_repeat_agreement_is_decision_plus_rendered_value():
    a = [{"id": "x", "key": ["fact", "yes"]}, {"id": "y", "key": ["option", "no"]}]
    b = [{"id": "x", "key": ["option", "yes"]}, {"id": "y", "key": ["option", "no"]}]
    rate, differ = E.agreement(a, b)
    assert rate == 0.5 and differ == ["x"]


def test_an_entry_plan_would_ignore_fails_as_ignored():
    c = case(
        "c9",
        {"label": "Are you 18 or older?", "kind": "radio_group", "options": ["Yes", "No"]},
        {"action": "fill", "value": "Yes"},
    )
    fact = {"fact": "authorized", "yes_when": True}
    e = entry("c9", decision="fact", fact=fact)
    row = grade(c, e, Override(value="", decision="fact", fact=fact))
    assert not row["passed"] and row["got"]["reason"].startswith("ignored: a fact judgment")


def test_a_persona_renders_a_computed_salary():
    c = case(
        "c10", {"label": "Desired pay", "kind": "text"}, {"action": "fill", "value": "120000 USD"}
    )
    e = entry("c10", decision="question", question="salary")
    row = grade(c, e, Override(value="", decision="question", question="salary"))
    assert row["passed"], row


def test_one_picked_option_equals_the_same_scalar():
    assert E.norm(("I am NOT authorized.",)) == E.norm("i am not authorized.")
    assert E.norm(("a", "b")) != E.norm("a")


def test_a_question_that_parks_without_a_role_passes_only_for_that_question():
    answers = E.persona_answers(PERSONA, US)
    answers = replace(
        answers,
        questions=answers.questions
        + (
            Question(id="why_co", role_file="why_company"),
            Question(id="why_role", role_file="why_role"),
        ),
    )
    c = case(
        "c11", {"label": "Why us?", "kind": "textarea"}, {"action": "fill", "question": "why_co"}
    )

    def row(qid: str) -> dict:
        e = entry("c11", decision="question", question=qid)
        return grade(c, e, Override(value="", decision="question", question=qid), answers)

    assert row("why_co")["passed"]
    assert not row("why_role")["passed"]  # both park, for a different question


def test_no_job_country_reads_as_unknown_never_home():
    from applyourself.apply.answers import UNKNOWN_COUNTRY

    assert E.persona_answers(PERSONA, "").country == UNKNOWN_COUNTRY


def test_a_checkbox_expecting_its_affirmative_option_passes_on_yes():
    f = {"label": "I acknowledge this.", "kind": "checkbox", "options": ["I acknowledge"]}
    c = case("c12", f, {"action": "fill", "value": "I acknowledge"})
    e = entry("c12", decision="option", value="I acknowledge", basis=["option"])
    o = Override(value="I acknowledge", decision="option", basis=("option",))
    assert grade(c, e, o)["passed"]


def test_a_refused_judged_entry_fails_even_when_an_ask_was_expected():
    c = case(
        "c9b",
        {"label": "Are you 18 or older?", "kind": "radio_group", "options": ["Yes", "No"]},
        {"action": "ask"},
        group="must_ask",
    )
    fact = {"fact": "authorized", "yes_when": True}
    e = entry("c9b", decision="fact", fact=fact)
    row = grade(c, e, Override(value="", decision="fact", fact=fact))
    assert not row["passed"] and row["got"]["action"] == "ignored"


def test_or_ask_accepts_a_stop_but_not_a_wrong_fill():
    c = case("c9", YES_NO, {"action": "fill", "value": "No", "or_ask": True})
    ask = entry("c9", decision="ask", ask="Will you relocate?")
    assert grade(c, ask, Override(value="", decision="ask", ask="Will you relocate?"))["passed"]
    wrong = entry("c9", decision="option", value="Yes", basis=["option"])
    ov = Override(value="Yes", source="option", decision="option", basis=("option",))
    assert not grade(c, wrong, ov)["passed"]
