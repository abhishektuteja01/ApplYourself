"""The eval case schema and the public set's integrity."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from applyourself.evals.cases import (
    CASES_DIR,
    Case,
    load_cases,
    load_persona,
    persona_path,
)

FIELD = {
    "label": "Are you authorized to work in the US?",
    "kind": "select",
    "options": ["Yes", "No"],
}


def test_a_fill_with_two_expectations_is_refused():
    with pytest.raises(ValidationError, match="exactly one"):
        Case.model_validate(
            {
                "id": "x",
                "group": "flip_word",
                "persona": "opt_student",
                "job_country": "United States",
                "field": FIELD,
                "expect": {
                    "action": "fill",
                    "value": "Yes",
                    "fact": {"fact": "authorized", "yes_when": True},
                },
            }
        )


def test_every_public_case_names_a_real_persona_and_question():
    files = sorted(CASES_DIR.glob("*.jsonl"))
    assert files
    personas: dict = {}
    for path in files:
        for case in load_cases(path):
            if case.persona not in personas:
                assert persona_path(case.persona).is_file(), case.id
                personas[case.persona] = load_persona(persona_path(case.persona))
            qid = case.expect.question
            if qid is not None:
                assert personas[case.persona].question(qid) is not None, case.id


def test_a_relabel_value_the_field_does_not_offer_is_refused(tmp_path):
    from applyourself.evals.extract_apply import RelabelError, relabel

    case = Case.model_validate(
        {
            "id": "x",
            "group": "private_bank",
            "persona": "real",
            "job_country": "United States",
            "field": FIELD,
            "expect": {"action": "fill", "question": "q"},
        }
    )
    path = tmp_path / "relabel.yaml"
    path.write_text(
        "relabel:\n  - label: 'are you authorized to work in the us'\n"
        "    expect: {action: fill, value: 'Maybe'}\n",
        encoding="utf-8",
    )
    with pytest.raises(RelabelError, match="not among its options"):
        relabel([case], path)
