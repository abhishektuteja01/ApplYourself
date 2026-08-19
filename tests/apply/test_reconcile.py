import dataclasses

import pytest

from src.apply.domscan import DomField, DomOption, FormScan, scan_form
from src.apply.reconcile import (
    DEMOGRAPHIC_SOURCE,
    MergedOption,
    ReconcileError,
    reconcile,
)
from src.apply.schema import (
    BoardSchema,
    DemographicOption,
    DemographicQuestion,
    Field,
    Option,
    Question,
    parse_schema,
)

from .conftest import FORM_FIXTURES, load_fixture, load_html


@pytest.fixture
def merged():
    def _merged(name):
        return reconcile(scan_form(load_html(name)), parse_schema(load_fixture(name)))
    return _merged


def field(rec, field_id):
    found = rec.by_id(field_id)
    assert found is not None, f"{field_id} missing; got {[f.id for f in rec.fields]}"
    return found


def dom_scan(name):
    return scan_form(load_html(name))


# --- the regression this module exists to prevent ----------------------------

@pytest.mark.parametrize("name", FORM_FIXTURES)
def test_every_rendered_field_survives_the_merge(merged, name):
    """Nothing the browser will show the applicant may be lost to the join."""
    assert [f.id for f in merged(name).fields] == [f.id for f in dom_scan(name).fields]


@pytest.mark.parametrize("name", FORM_FIXTURES)
def test_dom_only_fields_are_planned_and_flagged(merged, name):
    """A field the API never declares is still merged in — marked dom_only so a
    caller knows it has no label, no required flag and no option list to lean on."""
    rec = merged(name)
    country = field(rec, "country")
    assert country.dom_only and country.api_source is None
    assert country.kind == "react_select"


@pytest.mark.parametrize("name", FORM_FIXTURES)
def test_the_2b_dom_only_census_survives_with_its_required_flags(merged, name):
    """§2b's census of ids that appear in NO question array. Building the plan
    from the schema alone drops every one of them; several are required, so the
    submit would fail on a field the planner never knew existed."""
    census = {
        "country", "hispanic_ethnicity",
        "school--0", "degree--0", "discipline--0",
        "start-year--0", "end-year--0", "start-month--0", "end-month--0",
        "company-name-0", "title-0", "current-role-0_1",
        "start-date-month-0", "start-date-year-0",
        "end-date-month-0", "end-date-year-0",
    }
    rec, scan = merged(name), dom_scan(name)
    rendered = {f.id for f in scan.fields} & census
    assert rendered, f"{name} renders none of the census"
    for field_id in rendered:
        assert field(rec, field_id).dom_only
        assert field(rec, field_id).required is scan.by_id(field_id).required


@pytest.mark.parametrize("name", FORM_FIXTURES)
def test_required_comes_from_the_dom_verbatim(merged, name):
    """The API's required flag never overrides, weakens or supplies one. Over 60
    live boards the two never disagreed on a matched field — the rule earns its
    keep on the DOM-only fields, which have no API flag at all."""
    rec = merged(name)
    for dom in dom_scan(name).fields:
        assert field(rec, dom.id).required is dom.required


def test_education_block_survives_with_its_required_flags(merged):
    rec = merged("form_education")
    for field_id in ("school--0", "degree--0", "discipline--0",
                     "start-month--0", "start-year--0", "end-month--0", "end-year--0"):
        assert field(rec, field_id).dom_only
        assert field(rec, field_id).section == "education"
    # Required varies per board; form_demographic renders the same block required.
    assert field(merged("form_demographic"), "school--0").required is True


def test_employment_block_survives_with_its_required_flags(merged):
    rec = merged("form_employment")
    employment = rec.by_section("employment")
    assert {f.name for f in employment} == {
        "company-name-0", "title-0", "start-date-month-0", "start-date-year-0",
        "end-date-month-0", "end-date-year-0", "current-role-0",
    }
    assert all(f.dom_only for f in employment)
    assert sum(1 for f in employment if f.required) == 6


# --- the joins ---------------------------------------------------------------

def test_location_alias_joins_candidate_location(merged):
    """The API calls it `location`; the form renders `candidate-location`."""
    loc = field(merged("form_minimal"), "candidate-location")
    assert loc.api_source == "location_questions"
    assert loc.label == "Location"
    assert loc.kind == "react_select"      # DOM wins: the API declares input_text
    assert loc.api_type == "input_text"


@pytest.mark.parametrize("name", FORM_FIXTURES)
def test_undeclared_lat_long_never_become_fields(merged, name):
    """Declared required by the API, rendered on no board — nothing can fill or
    assert them, so they must not reach a plan."""
    rec = merged(name)
    assert rec.by_id("latitude") is None
    assert rec.by_id("longitude") is None


@pytest.mark.parametrize("name", FORM_FIXTURES)
def test_file_twins_are_not_reported_as_unrendered(merged, name):
    """Resume/CV declares `resume` and `resume_text`; satisfying either satisfies
    the question, so the unrendered twin is not a gap."""
    assert not [n for n in merged(name).api_only if n.endswith("_text")]


def test_api_only_is_exactly_what_renders_nowhere(merged):
    assert set(merged("form_minimal").api_only) == {"latitude", "longitude", "race"}


def test_race_and_hispanic_ethnicity_are_not_the_same_question(merged):
    """`race` is API-only, `hispanic_ethnicity` is DOM-only. Aliasing them would
    answer an EEOC question the applicant was never asked."""
    rec = merged("form_minimal")
    assert "race" in rec.api_only
    assert rec.by_id("race") is None
    assert field(rec, "hispanic_ethnicity").dom_only


def test_demographic_questions_join_on_id(merged):
    rec = merged("form_demographic")
    demographic = rec.by_section("demographic")
    assert len(demographic) == 6
    assert all(f.api_source == DEMOGRAPHIC_SOURCE for f in demographic)
    assert not [n for n in rec.api_only if n.startswith(DEMOGRAPHIC_SOURCE)]


def test_demographic_multi_flag_agrees_with_the_api_type(merged):
    """The DOM tell is a class on an ancestor div; the API says it outright.
    They agreed on every demographic question across 7 live boards."""
    for f in merged("form_demographic").by_section("demographic"):
        assert f.multi is (f.api_type == "multi_value_multi_select")


def test_demographic_options_carry_the_opt_out_flags(merged):
    """Tier A2 resolves by `decline_to_answer` first, then an exact label match,
    so both must survive the merge."""
    gender = field(merged("form_demographic"), "4005807007")
    labels = [o.label for o in gender.options]
    assert "I don't wish to answer" in labels
    assert any(o.free_form for o in gender.options)
    assert gender.options[0].value is not None


def test_an_unrendered_demographic_question_is_reported(merged):
    rec = reconcile(
        dom_scan("form_minimal"),
        dataclasses.replace(
            parse_schema(load_fixture("form_minimal")),
            demographic=(DemographicQuestion(
                id=99, label="Ghost", required=False,
                type="multi_value_single_select", options=(),
            ),),
        ),
    )
    assert f"{DEMOGRAPHIC_SOURCE}:99" in rec.api_only


# --- enrichment --------------------------------------------------------------

def test_select_options_come_from_the_api(merged):
    """The DOM expresses a react-select's choices only as ARIA plumbing, so the
    option labels have to come from the schema."""
    sponsorship = field(merged("form_minimal"), "question_68166152")
    assert [o.label for o in sponsorship.options] == ["Yes", "No"]
    assert len(field(merged("form_minimal"), "question_68166154").options) == 10


def test_checkbox_group_options_come_from_the_dom(merged):
    """Each option renders its own visible label, so no API lookup is needed —
    and this is the one widget that still has options when the join misses."""
    group = field(merged("form_multiselect"), "question_36638875002[]")
    assert group.kind == "checkbox_group"
    assert group.name == "question_36638875002"      # "[]" stripped for the join
    assert len(group.options) == 6
    assert all(o.label and o.value for o in group.options)


def test_dom_only_selects_have_no_options_to_offer(merged):
    """`country` and the education selects draw on a remote taxonomy the API
    never exposes. Inventing options here would be a guess; fill must type the
    configured string and assert the widget actually selected something."""
    rec = merged("form_education")
    for field_id in ("country", "school--0", "degree--0"):
        assert field(rec, field_id).options == ()


def test_api_label_wins_because_tier_a2_matches_it_exactly(merged):
    """Compliance answers are keyed to the API's CamelCase labels; the DOM
    renders the same four questions with spaced, friendlier text."""
    rec = merged("form_minimal")
    assert field(rec, "veteran_status").label == "VeteranStatus"
    assert field(rec, "disability_status").label == "DisabilityStatus"
    assert field(rec, "veteran_status").api_source == "compliance:eeoc"


def test_dom_label_is_the_fallback_when_the_api_declares_nothing(merged):
    assert field(merged("form_minimal"), "hispanic_ethnicity").label == "Are you Hispanic/Latino?"


def test_labels_are_whitespace_collapsed(merged):
    """Several API labels arrive with a trailing newline; Tier B matches on the
    normalized label and a stray newline is not a difference."""
    assert field(merged("form_multiselect"), "question_36638877002").label == "Citizenship"


def test_employer_authored_questions_keep_their_section(merged):
    """A question that reads like EEOC but sits in the questions block is
    rule-matched, not answered structurally."""
    race_alike = field(merged("form_multiselect"), "question_36638878002")
    assert race_alike.section == "questions"
    assert race_alike.api_source == "questions"


# --- ambiguity is an error ---------------------------------------------------

def _schema(questions=(), demographic=()):
    return BoardSchema(
        questions=tuple(questions), demographic=tuple(demographic),
        education=None, employment=None, company_name="Widget Co", title="Fitter",
    )


def _question(name, label="Q", source="questions", options=()):
    return Question(
        label=label, required=False, source=source,
        fields=(Field(name=name, type="multi_value_single_select", options=tuple(options)),),
    )


def _scan(*fields):
    return FormScan(fields=tuple(fields), submit_selector="#s", submit_disabled=False)


def _dom(field_id, **kw):
    return DomField(**{
        "id": field_id, "name": field_id, "label": "L", "required": False,
        "kind": "text", "section": "questions", **kw,
    })


def test_duplicate_api_field_name_raises():
    """Last-wins would enrich a select with a different question's options."""
    schema = _schema(questions=[_question("question_1", "A"), _question("question_1", "B")])
    with pytest.raises(ReconcileError, match="duplicate API field name"):
        reconcile(_scan(_dom("question_1")), schema)


def test_duplicate_dom_id_raises():
    """Two controls sharing an id make every selector built from it ambiguous."""
    with pytest.raises(ReconcileError, match="duplicate DOM field id"):
        reconcile(_scan(_dom("first_name"), _dom("first_name")), _schema())


def test_duplicate_demographic_id_raises():
    ghost = DemographicQuestion(
        id=7, label="G", required=False, type="multi_value_single_select",
        options=(DemographicOption(id=1, label="x"),),
    )
    with pytest.raises(ReconcileError, match="duplicate demographic question id"):
        reconcile(_scan(_dom("7", section="demographic")), _schema(demographic=[ghost, ghost]))


def test_a_dom_field_with_no_match_anywhere_is_still_merged():
    rec = reconcile(_scan(_dom("surprise_field", required=True)), _schema())
    assert rec.by_id("surprise_field").dom_only
    assert rec.required_ids == ("surprise_field",)


def test_checkbox_group_keeps_dom_options_with_no_api_match():
    group = _dom(
        "question_9[]", name="question_9", kind="checkbox_group", multi=True,
        options=(DomOption(value="1", label="Alpha"), DomOption(value="2", label="Beta")),
    )
    merged_field = reconcile(_scan(group), _schema()).by_id("question_9[]")
    assert merged_field.options == (
        MergedOption(label="Alpha", value="1"),
        MergedOption(label="Beta", value="2"),
    )


def test_api_option_values_survive_for_selects():
    schema = _schema(questions=[_question("question_1", options=[Option(label="Yes", value=1)])])
    merged_field = reconcile(_scan(_dom("question_1", kind="react_select")), schema).by_id("question_1")
    assert merged_field.options == (MergedOption(label="Yes", value=1),)


def _multi_question(name):
    return Question(
        label="Mark all that apply", required=True, source="questions",
        fields=(Field(name=name, type="multi_value_multi_select", options=(), multi=True),),
    )


def test_api_multi_but_dom_single_raises():
    """Filling this as a single select answers a "mark all that apply" with one
    option and moves on. Silent, and wrong under the applicant's name."""
    with pytest.raises(ReconcileError, match="multi"):
        reconcile(
            _scan(_dom("question_1", kind="react_select", multi=False)),
            _schema(questions=[_multi_question("question_1")]),
        )


def test_dom_multi_but_api_single_raises():
    """The mirror case: a second click on a single select clears the first."""
    with pytest.raises(ReconcileError, match="multi"):
        reconcile(
            _scan(_dom("question_1", kind="react_select", multi=True)),
            _schema(questions=[_question("question_1")]),
        )


def test_cardinality_is_unchecked_when_the_api_declared_nothing(merged):
    """A DOM-only field has no API type to contradict, so the guard must not
    fire on it — `country` would fail every board otherwise."""
    assert field(merged("form_minimal"), "country").api_type is None


# --- unhappy paths -----------------------------------------------------------

def test_a_form_with_no_fields_merges_to_nothing():
    rec = reconcile(_scan(), _schema())
    assert rec.fields == () and rec.api_only == ()
    assert rec.required_ids == () and rec.dom_only_ids == ()


def test_a_schema_that_renders_nothing_is_all_api_only():
    rec = reconcile(_scan(), _schema(questions=[_question("question_1"), _question("question_2")]))
    assert rec.fields == ()
    assert set(rec.api_only) == {"question_1", "question_2"}


def test_a_demographic_field_that_joins_nothing_is_still_merged():
    """Bare-numeric id, no matching demographic question. It is rendered, so it
    is planned — with no label and no options, which parks the role rather than
    guessing an answer to a demographic question."""
    rec = reconcile(_scan(_dom("4005807007", section="demographic", label="")), _schema())
    merged_field = rec.by_id("4005807007")
    assert merged_field.dom_only and merged_field.options == ()


def test_a_demographic_question_with_no_id_cannot_join_and_is_reported():
    """It has nothing to pair a rendered field with, so it is reported rather
    than silently indexed under a `None` key."""
    ghost = DemographicQuestion(
        id=None, label="G", required=False, type="multi_value_single_select", options=(),
    )
    rec = reconcile(_scan(), _schema(demographic=[ghost]))
    assert rec.api_only == (f"{DEMOGRAPHIC_SOURCE}:None",)


def test_an_empty_label_on_both_sides_stays_empty_rather_than_guessing():
    schema = _schema(questions=[_question("question_1", label="")])
    assert reconcile(_scan(_dom("question_1", label="")), schema).by_id("question_1").label == ""


def test_the_bracket_suffix_never_reaches_the_api_join_key():
    """The id is the selector and keeps "[]"; the name is the join key and does
    not. Joining on the id would miss every multi-select."""
    group = _dom("question_9[]", name="question_9", kind="checkbox_group", multi=True,
                 options=(DomOption(value="1", label="Alpha"),))
    schema = _schema(questions=[Question(
        label="Pick", required=True, source="questions",
        fields=(Field(name="question_9", type="multi_value_multi_select", multi=True),),
    )])
    merged_field = reconcile(_scan(group), schema).by_id("question_9[]")
    assert merged_field.api_source == "questions"
    assert merged_field.options == (MergedOption(label="Alpha", value="1"),)
