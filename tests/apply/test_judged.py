"""Judged entries: rendered from the bank and the facts, behind plan's one gate."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import pytest

from applyourself.apply import answers as A
from applyourself.apply import judged
from applyourself.apply.bank_schema import Question
from applyourself.apply.plan import Override, build_plan
from applyourself.apply.reconcile import MergedField, MergedOption, Reconciled
from applyourself.apply_cli import ApplyCliError, load_overrides

YES_NO = (MergedOption(label="Yes"), MergedOption(label="No"))


def field(**kw) -> MergedField:
    base = dict(
        id="q",
        name="q",
        label="A question",
        required=True,
        kind="text",
        section="questions",
        multi=False,
        options=(),
    )
    base.update(kw)
    return MergedField(**base)


def plan_one(f, answers, out_dir, entry):
    return build_plan(
        Reconciled(fields=(f,), api_only=()), answers, out_dir, overrides={f.id: entry}
    )


def test_an_option_not_on_the_board_parks(answers):
    f = field(kind="select", options=YES_NO)
    entry = Override(value="Maybe", decision="option", basis=("option",))
    r = judged.render(f, entry, answers)
    assert r.action == "park" and "Maybe" in r.reason


def test_a_missing_basis_id_parks_naming_it(answers):
    f = field(kind="textarea", label="Tell us about a project.")
    entry = Override(
        value="I built a widget line.", decision="text", basis=("B-WID-01", "F-NOPE", "S-9")
    )
    r = judged.render(f, entry, answers)
    assert r.action == "park"
    assert "F-NOPE" in r.reason and "S-9" in r.reason and "B-WID-01" not in r.reason
    with_fact = replace(answers, facts={"F-NOPE": "A fact."})
    entry = replace(entry, basis=("B-WID-01", "F-NOPE"))
    assert judged.render(f, entry, with_fact).action == "fill"


def test_a_decision_on_an_identity_field_parks_it(answers, tailor_dir):
    f = field(id="first_name", name="first_name", label="First Name")
    entry = Override(value="", decision="question", question="linkedin")
    plan = plan_one(f, answers, tailor_dir, entry)
    assert not plan.fields and not plan.ignored_overrides
    assert plan.unmapped[0].reason == "identity and EEOC fields take no override"


def test_a_session_override_the_gate_refuses_is_ignored(answers, tailor_dir):
    f = field(id="first_name", name="first_name", label="First Name")
    plan = plan_one(f, answers, tailor_dir, Override(value="Sam"))
    assert plan.fields[0].value == "Alex"
    assert plan.ignored_overrides == ("first_name: identity and EEOC fields take no override",)


def test_export_control_takes_a_fact_not_a_typed_value(answers, tailor_dir):
    f = field(label="Are you a U.S. person?", kind="select", options=YES_NO)
    typed = Override(value="No", decision="option", basis=("option",))
    plan = plan_one(f, answers, tailor_dir, typed)
    assert plan.parked and not plan.ignored_overrides
    assert "export control" in plan.unmapped[0].reason
    fact = Override(value="", decision="fact", fact={"fact": "us_person", "yes_when": True})
    plan = plan_one(f, replace(answers, us_person="no"), tailor_dir, fact)
    assert not plan.ignored_overrides and plan.fields[0].value == "No"


def test_a_question_renders_a_computed_start_date(answers, monkeypatch):
    monkeypatch.setattr(A, "_today", lambda: date(2026, 9, 27))
    f = field(label="Earliest day you could join us", kind="date")
    entry = Override(value="", decision="question", question="when_can_you_start")
    r = judged.render(f, entry, answers)
    assert (r.action, r.value, r.source) == ("fill", "10/11/2026", "bank:when_can_you_start")


def test_a_question_renders_a_role_file_answer(answers):
    bank = replace(
        answers,
        questions=(*answers.questions, Question(id="why_us", role_file="why_company")),
        company_answers={"why_company": "Your widgets."},
    )
    entry = Override(value="", decision="question", question="why_us")
    r = judged.render(field(kind="textarea", label="What draws you here?"), entry, bank)
    assert (r.action, r.value) == ("fill", "Your widgets.")
    r = judged.render(field(kind="textarea"), entry, replace(bank, company_answers={}))
    assert r.action == "park" and "why_company" in r.reason


def test_ask_parks_with_its_reason(answers, tailor_dir):
    entry = Override(value="", decision="ask", ask="Do you hold a forklift licence?")
    plan = plan_one(field(), answers, tailor_dir, entry)
    assert plan.unmapped[0].reason == "ask: Do you hold a forklift licence?"


def test_skip_parks_a_required_field_and_skips_an_optional_one(answers, tailor_dir):
    entry = Override(value="", decision="skip")
    assert plan_one(field(), answers, tailor_dir, entry).parked
    plan = plan_one(field(required=False), answers, tailor_dir, entry)
    assert [s.id for s in plan.skipped] == ["q"] and plan.draftable == ()


def test_a_fact_decision_follows_the_persona(answers):
    f = field(label="Can you work here without a visa?", kind="select", options=YES_NO)
    entry = Override(value="", decision="fact", fact={"fact": "authorized", "yes_when": True})
    us = replace(answers, job_country="United States")
    row = replace(us.countries["United States"], authorized=False)
    other = replace(us, countries={**us.countries, "United States": row})
    assert judged.render(f, entry, us).value == "Yes"
    assert judged.render(f, entry, other).value == "No"


def test_field_from_dict_renders_options():
    f = judged.field_from_dict(
        {"id": "x[]", "label": "L", "kind": "select", "options": ["Yes", "No"], "multi": True}
    )
    assert f.name == "x" and [o.label for o in f.options] == ["Yes", "No"] and f.multi


@pytest.mark.parametrize(
    "entry, needle",
    [
        ({"decision": "guess"}, "decision is one of"),
        ({"decision": "text", "value": "Hi"}, "basis"),
        ({"decision": "ask"}, "what to ask"),
        ({"decision": "question"}, "names a bank question"),
        ({"decision": "skip", "claim": {"authorized": True}}, "unknown keys"),
    ],
)
def test_load_overrides_refuses_a_malformed_judged_entry(tmp_path, entry, needle):
    path = tmp_path / "answers_override.json"
    path.write_text(json.dumps({"q": entry}), encoding="utf-8")
    with pytest.raises(ApplyCliError, match=needle):
        load_overrides(path)


def test_load_overrides_reads_judged_entries_without_a_value(tmp_path):
    path = tmp_path / "answers_override.json"
    path.write_text(
        json.dumps(
            {
                "a": {"decision": "question", "question": "linkedin", "label": "L"},
                "b": {"decision": "fact", "fact": {"fact": "authorized", "yes_when": True}},
                "c": {"decision": "text", "value": "Hi", "basis": ["B-WID-01"]},
                "d": {"decision": "skip", "why": "junk"},
            }
        ),
        encoding="utf-8",
    )
    got = load_overrides(path)
    assert got["a"].question == "linkedin" and got["b"].fact["fact"] == "authorized"
    assert (got["c"].source, got["c"].basis) == ("B-WID-01", ("B-WID-01",))
    assert got["d"].decision == "skip"


def test_a_fact_decision_outside_work_authorization_parks(answers, tailor_dir):
    f = field(label="Are you 18 or older?", kind="select", options=YES_NO)
    entry = Override(value="", decision="fact", fact={"fact": "authorized", "yes_when": True})
    plan = plan_one(f, answers, tailor_dir, entry)
    assert plan.parked and not plan.fields and not plan.ignored_overrides
    assert plan.unmapped[0].reason == (
        "a fact judgment is only for a work-authorization or export-control question"
    )


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("Are you authorized to work in California?", "Yes"),  # a state is the US
        ("Are you authorized to work in San Jose, CA?", "Yes"),
        ("Are you authorized to work in Puerto Rico?", "Yes"),  # a territory too
        ("Are you authorized to work in Georgia?", "No"),  # a country too: the job's
    ],
)
def test_a_us_state_or_territory_reads_as_the_us(answers, label, value):
    germany = replace(answers, job_country="Germany")
    f = field(label=label, kind="select", options=YES_NO)
    r = A.resolve_fact_wording(f, germany, {"fact": "authorized", "yes_when": True})
    assert (r.action, r.value) == ("fill", value)


def test_a_fact_decision_on_a_permission_label_outside_b0_applies(answers, tailor_dir):
    f = field(
        label="Are you currently permitted to take up employment in the USA?",
        kind="select",
        options=YES_NO,
    )
    entry = Override(value="", decision="fact", fact={"fact": "authorized", "yes_when": True})
    plan = plan_one(f, answers, tailor_dir, entry)
    assert not plan.ignored_overrides


US_PERSON_SENTENCES = (
    MergedOption(label="I am a U.S. person"),
    MergedOption(label="None of the above; I am a citizen of a different country"),
)


@pytest.mark.parametrize(("us_person", "refused"), [("no", False), ("yes", False), ("park", True)])
def test_export_control_takes_a_judged_option_once_us_person_is_set(
    answers, tailor_dir, us_person, refused
):
    f = field(
        label="Which best describes your U.S. person status?",
        kind="select",
        options=US_PERSON_SENTENCES,
    )
    pick = Override(value="I am a U.S. person", decision="option", basis=("option",))
    plan = plan_one(f, replace(answers, us_person=us_person), tailor_dir, pick)
    assert not plan.ignored_overrides
    if refused:
        assert "us_person is set" in plan.unmapped[0].reason
    else:
        assert plan.fields[0].value == "I am a U.S. person"  # polarity is the judge's


def test_export_control_refuses_judged_text_even_with_us_person_set(answers, tailor_dir):
    f = field(label="Are you a U.S. person?")
    typed = Override(value="No", decision="text", basis=("option",))
    plan = plan_one(f, replace(answers, us_person="no"), tailor_dir, typed)
    assert not plan.ignored_overrides and "export control" in plan.unmapped[0].reason


def test_a_bank_question_id_is_a_basis(answers):
    f = field(kind="select", options=YES_NO)
    entry = Override(value="Yes", decision="option", basis=("linkedin",))
    assert judged.render(f, entry, answers).action == "fill"


@pytest.mark.parametrize(
    "basis", ["us_person", "nationalities", "work_authorization.authorized", "status_label"]
)
def test_a_work_authorization_key_is_a_basis(answers, basis):
    f = field(kind="select", options=YES_NO)
    entry = Override(value="Yes", decision="option", basis=(basis,))
    assert judged.render(f, entry, answers).action == "fill"
    entry = Override(value="Yes", decision="option", basis=("work_authorization.salary",))
    assert judged.render(f, entry, answers).action == "park"


@pytest.mark.parametrize(
    "value,action",
    [
        ("I acknowledge", "fill"),
        ("I agree.", "fill"),
        ("yes", "fill"),
        ("I have read the privacy notice", "fill"),  # the checkbox's own label
        ("No", "park"),
        ("I decline", "park"),
    ],
)
def test_a_single_checkbox_ticks_only_on_an_affirmative(answers, value, action):
    f = field(kind="checkbox", label="I have read the privacy notice")
    entry = Override(value=value, decision="text", basis=("option",))
    r = judged.render(f, entry, answers)
    assert r.action == action
    if action == "fill":
        assert r.value == "Yes"


@pytest.mark.parametrize("decision", ["fact", "question"])
def test_a_single_checkbox_answered_no_parks_never_ticks(answers, tailor_dir, decision):
    f = field(kind="checkbox", label="I am authorized to work in the United States")
    no_bank = replace(
        answers, questions=answers.questions + (Question(id="no_q", answers=("No",)),)
    )
    entry = Override(
        value="",
        decision=decision,
        fact={"fact": "authorized", "yes_when": False},
        question="no_q",
    )
    plan = plan_one(f, no_bank, tailor_dir, entry)
    assert not plan.fields and not plan.ignored_overrides
    assert plan.unmapped[0].reason == "a single checkbox takes only Yes; the answer here is No"


@pytest.mark.parametrize(
    "override",
    [None, Override(value="", decision="fact", fact={"fact": "us_person", "yes_when": True})],
)
def test_a_lone_itar_checkbox_with_us_person_no_parks(answers, tailor_dir, override):
    f = field(label="I certify that I am a U.S. person under ITAR", kind="checkbox")
    a = replace(answers, us_person="no")
    plan = build_plan(
        Reconciled(fields=(f,), api_only=()),
        a,
        tailor_dir,
        overrides={f.id: override} if override else None,
    )
    assert not plan.fields
    assert "a single checkbox takes only Yes" in plan.unmapped[0].reason
