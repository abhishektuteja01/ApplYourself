import pytest

from src.apply import schema
from src.discovery.sources.ats.http import CareersError
from tests.apply.conftest import load_fixture


def parse(name):
    return schema.parse_schema(load_fixture(name))


def sources(bs):
    return {q.source for q in bs.questions}


def by_label(bs, label):
    return next(q for q in bs.questions if q.label == label)


def test_flat_sources_tagged():
    bs = parse("greenhouse_location_compliance")
    assert sources(bs) == {"questions", "location_questions", "compliance:eeoc"}


def test_compliance_group_is_unwrapped():
    bs = parse("greenhouse_location_compliance")
    eeoc = [q.label for q in bs.questions if q.source == "compliance:eeoc"]
    assert sorted(eeoc) == ["DisabilityStatus", "Gender", "Race", "VeteranStatus"]
    # The group wrapper itself must not survive as a question.
    assert all(q.fields for q in bs.questions)


def test_location_questions_picked_up():
    bs = parse("greenhouse_location_compliance")
    loc = {f.name for q in bs.questions if q.source == "location_questions" for f in q.fields}
    assert loc == {"location", "latitude", "longitude"}


def test_empty_location_questions_yields_none():
    bs = parse("greenhouse_minimal")
    assert "location_questions" not in sources(bs)


def test_null_compliance_and_demographic_are_fine():
    raw = load_fixture("greenhouse_minimal")
    assert raw["compliance"] is None and raw["demographic_questions"] is None
    bs = schema.parse_schema(raw)
    assert bs.demographic == ()
    assert not [q for q in bs.questions if q.source.startswith("compliance")]


def test_multi_field_question_is_or():
    bs = parse("greenhouse_minimal")
    resume = by_label(bs, "Resume/CV")
    assert [f.name for f in resume.fields] == ["resume", "resume_text"]
    assert [f.type for f in resume.fields] == ["input_file", "textarea"]
    assert resume.satisfy == "any"


def test_single_select_retains_options():
    bs = parse("greenhouse_location_compliance")
    gender = by_label(bs, "Gender")
    field = gender.fields[0]
    assert field.type == "multi_value_single_select"
    assert "Decline To Self Identify" in [o.label for o in field.options]
    assert all(o.value is not None for o in field.options)


def test_multiselect_strips_bracket_suffix():
    bs = parse("greenhouse_multiselect")
    multi = [f for q in bs.questions for f in q.fields if f.type == "multi_value_multi_select"]
    assert multi
    for f in multi:
        assert f.multi is True
        assert not f.name.endswith("[]")
        assert f.options


def test_demographic_questions_parsed_separately():
    bs = parse("greenhouse_demographic")
    assert len(bs.demographic) == 4
    assert not [q for q in bs.questions if q.label in {d.label for d in bs.demographic}]
    q = bs.demographic[0]
    assert q.id and q.type in schema.FIELD_TYPES


def test_demographic_required_flag_is_carried_both_ways():
    # A demographic question may be required; it is not always optional.
    assert all(q.required for q in parse("greenhouse_demographic").demographic)
    assert not any(q.required for q in parse("greenhouse_demographic_freeform").demographic)


def test_demographic_decline_flag_parsed():
    opts = [o for q in parse("greenhouse_demographic").demographic for o in q.options]
    assert any(o.decline_to_answer for o in opts)
    assert any(o.label == "I don't wish to answer" for o in opts)


def test_demographic_free_form_parsed_without_decline_flag():
    # The majority case: an opt-out exists by label only, decline flag unset.
    opts = [o for q in parse("greenhouse_demographic_freeform").demographic for o in q.options]
    assert any(o.free_form for o in opts)
    assert not any(o.decline_to_answer for o in opts)
    assert any(o.label == "I don't wish to answer" for o in opts)


def test_demographic_without_optout_still_parses():
    bs = parse("greenhouse_demographic_no_optout")
    q = bs.demographic[0]
    assert q.options
    assert not any(o.decline_to_answer for o in q.options)
    assert not any(o.label == "I don't wish to answer" for o in q.options)


@pytest.mark.parametrize("name,education,employment", [
    ("greenhouse_minimal", None, None),
    ("greenhouse_location_compliance", "education_optional", None),
    ("greenhouse_demographic", "education_optional", "employment_optional"),
    ("greenhouse_multiselect", "education_required", None),
])
def test_education_and_employment_blocks(name, education, employment):
    bs = parse(name)
    assert bs.education == education
    assert bs.employment == employment


def test_company_and_title_carried():
    bs = parse("greenhouse_minimal")
    assert bs.company_name == "Widget Corp"
    assert bs.title == "Widget Engineer"


def test_unknown_field_type_raises():
    raw = load_fixture("greenhouse_minimal")
    raw["questions"][0]["fields"][0]["type"] = "input_carrier_pigeon"
    with pytest.raises(schema.SchemaError, match="unknown field type"):
        schema.parse_schema(raw)


def test_unknown_demographic_type_raises():
    raw = load_fixture("greenhouse_demographic")
    raw["demographic_questions"]["questions"][0]["type"] = "vibes"
    with pytest.raises(schema.SchemaError, match="unknown demographic question type"):
        schema.parse_schema(raw)


def test_field_without_name_raises():
    raw = load_fixture("greenhouse_minimal")
    raw["questions"][0]["fields"][0]["name"] = ""
    with pytest.raises(schema.SchemaError, match="no name"):
        schema.parse_schema(raw)


def test_demographic_questions_as_list_raises():
    raw = load_fixture("greenhouse_demographic")
    raw["demographic_questions"] = raw["demographic_questions"]["questions"]
    with pytest.raises(schema.SchemaError, match="demographic_questions is not an object"):
        schema.parse_schema(raw)


def test_fetch_questions_builds_url_and_parses(monkeypatch):
    seen = {}

    def fake_fetch(url, timeout=30):
        seen["url"] = url
        return load_fixture("greenhouse_minimal")

    monkeypatch.setattr(schema, "fetch_json", fake_fetch)
    bs = schema.fetch_questions("widgetcorp", 4298509009)
    assert seen["url"] == (
        "https://boards-api.greenhouse.io/v1/boards/widgetcorp/jobs/4298509009?questions=true"
    )
    assert bs.company_name == "Widget Corp"


def test_fetch_questions_propagates_careers_error(monkeypatch):
    def fake_fetch(url, timeout=30):
        raise CareersError("board not found (404)", status=404, permanent=True)

    monkeypatch.setattr(schema, "fetch_json", fake_fetch)
    with pytest.raises(CareersError):
        schema.fetch_questions("nope", 1)
