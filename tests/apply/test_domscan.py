import pytest

from src.apply.domscan import DomScanError, scan_form
from src.apply.schema import parse_schema

from .conftest import FORM_FIXTURES, load_fixture, load_html


def ids(scan):
    return [f.id for f in scan.fields]


def field(scan, field_id):
    found = scan.by_id(field_id)
    assert found is not None, f"{field_id} not scanned; got {ids(scan)}"
    return found


# --- structure ---------------------------------------------------------------

@pytest.mark.parametrize("name", FORM_FIXTURES)
def test_every_board_yields_identity_files_and_one_submit(scan, name):
    s = scan(name)
    for required_id in ("first_name", "last_name", "email", "phone"):
        assert field(s, required_id).required
    # Resume is always rendered but NOT always required — optional on 9 of 25
    # live boards sampled, and on form_employment. Do not assert it required.
    assert field(s, "resume").kind == "file"
    # Cover letter is not universal (26/30 boards); when rendered it is a file.
    cover = s.by_id("cover_letter")
    assert cover is None or cover.kind == "file"
    assert s.submit_selector == '#application-form button[type="submit"]'
    assert s.submit_disabled is False


@pytest.mark.parametrize("name", FORM_FIXTURES)
def test_kinds_are_all_known(scan, name):
    from src.apply.domscan import KINDS
    assert {f.kind for f in scan(name).fields} <= KINDS


@pytest.mark.parametrize("name", FORM_FIXTURES)
def test_fields_come_back_in_document_order(scan, name):
    order = ids(scan(name))
    assert order[:2] == ["first_name", "last_name"]
    assert order.index("email") < order.index("resume")


def test_no_form_raises():
    with pytest.raises(DomScanError):
        scan_form("<html><body><p>redirected to a careers site</p></body></html>")


def test_empty_document_raises():
    with pytest.raises(DomScanError):
        scan_form("   ")


def test_a_document_lxml_cannot_parse_is_a_domscan_error():
    """A malformed board must fail as one role, not as the queue walk. lxml
    raises a bare ValueError on a string carrying an XML encoding declaration
    — not a DomScanError, so it was not in `apply_cli.BUILD_ERRORS` and the
    whole walk died on it."""
    with pytest.raises(DomScanError, match="could not parse"):
        scan_form('<?xml version="1.0" encoding="UTF-8"?>'
                  '<html><body><form id="application-form"></form></body></html>')


def test_a_required_field_with_no_id_raises():
    """No id means no selector, so nothing can fill it — and an unfilled
    required field is a refused submit at best."""
    with pytest.raises(DomScanError, match="required text field has no id"):
        scan_form('<form id="application-form">'
                  '<input type="text" aria-required="true"/>'
                  "</form>")


def test_an_optional_field_with_no_id_is_dropped_quietly():
    """The same shape, not required: an unlabelled decoy is not worth failing
    a whole board on."""
    s = scan_form('<form id="application-form">'
                  '<input type="text"/>'
                  '<input id="email" type="email"/>'
                  "</form>")
    assert ids(s) == ["email"]


def test_a_file_input_with_no_group_wrapper_raises():
    """The role=group wrapper carries the file field's real label and required
    flag; without it the scan would report an unlabelled, optional resume."""
    with pytest.raises(DomScanError, match="no role=group wrapper"):
        scan_form('<form id="application-form">'
                  '<input id="resume" type="file"/>'
                  "</form>")


def test_two_submit_buttons_raise_rather_than_picking_one():
    """Which one posts the application is not guessable from the DOM, and
    guessing wrong clicks something else on a filled form."""
    with pytest.raises(DomScanError, match="expected one submit button, found 2"):
        scan_form('<form id="application-form">'
                  '<button type="submit">Save draft</button>'
                  '<button type="submit">Submit application</button>'
                  "</form>")


def test_unknown_input_type_raises():
    with pytest.raises(DomScanError, match="unknown input type"):
        scan_form(
            '<form id="application-form">'
            '<input id="color_pick" type="color"/>'
            "</form>"
        )


# --- the decoys --------------------------------------------------------------

def test_aria_hidden_required_decoys_are_skipped(page, scan):
    """Greenhouse renders `remix-css-*-requiredInput` spans that carry a bare
    `required` attribute but are not fields. Counting them would report phantom
    required fields on every board."""
    html = page("form_minimal")
    assert 'class="remix-css-1a0ro4n-requiredInput"' in html
    assert 'aria-hidden="true"' in html
    for f in scan("form_minimal").fields:
        assert f.id, "a decoy with no id was emitted"


def test_visually_hidden_file_input_is_not_skipped(page, scan):
    """visually-hidden is not aria-hidden: the file inputs are the real ones."""
    assert 'id="resume" class="visually-hidden" type="file"' in page("form_minimal")
    assert field(scan("form_minimal"), "resume").kind == "file"


def test_toggle_gated_text_twins_are_absent(scan):
    """resume_text / cover_letter_text have a <label for> but no input until the
    "Enter manually" toggle is pressed."""
    s = scan("form_minimal")
    assert s.by_id("resume_text") is None
    assert s.by_id("cover_letter_text") is None


# --- labels ------------------------------------------------------------------

def test_required_asterisk_is_stripped_from_the_label(scan):
    assert field(scan("form_minimal"), "first_name").label == "First Name"


def test_file_label_comes_from_the_group_wrapper_not_the_local_label(page, scan):
    """The file input's own <label> reads "Attach"; the real one is on the
    wrapping role=group."""
    assert '<label class="visually-hidden" for="resume">Attach</label>' in page("form_minimal")
    resume = field(scan("form_minimal"), "resume")
    assert resume.label == "Resume/CV"
    assert resume.required is True
    assert field(scan("form_minimal"), "cover_letter").required is False


def test_label_falls_back_to_aria_labelledby(scan):
    """react-select inputs have no <label for>; they point at one by id."""
    assert field(scan("form_minimal"), "country").label == "Country"


# --- widget kinds ------------------------------------------------------------

def test_react_select_is_detected_by_role_not_tag(scan):
    """Not a native <select>; a tag-name scanner misses all of these."""
    s = scan("form_minimal")
    for field_id in ("country", "candidate-location", "gender", "question_68166152"):
        assert field(s, field_id).kind == "react_select"
    assert not any(f.kind == "react_select" and f.multi for f in s.fields)


def test_multi_react_select_flagged_from_the_value_container(scan):
    s = scan("form_demographic")
    assert field(s, "4005807007").multi is True   # "mark all that apply"
    assert field(s, "4005810007").multi is False  # "select one"


def test_checkbox_group_collapses_to_one_field_with_its_options(scan):
    s = scan("form_multiselect")
    group = field(s, "question_36638875002[]")
    assert group.kind == "checkbox_group"
    assert group.required is True
    assert group.multi is True
    assert group.label == "Diversity and Inclusion at Ratchet Co"
    assert [o.label for o in group.options][:3] == ["Woman", "Transgender Woman", "Man"]
    assert all(o.value for o in group.options)
    # The member checkboxes must not also appear as standalone fields.
    assert not any(f.id.startswith("question_36638875002[]_") for f in s.fields)


def test_checkbox_group_name_drops_the_bracket_suffix(scan):
    """`id` stays verbatim for the selector; `name` matches the API field."""
    group = field(scan("form_multiselect"), "question_36638875002[]")
    assert group.id.endswith("[]")
    assert group.name == "question_36638875002"


def test_free_text_kinds(scan):
    s = scan("form_multiselect")
    assert field(s, "phone").kind == "tel"
    assert field(s, "question_36638881002").kind == "textarea"
    assert field(s, "question_36638884002").kind == "text"


# --- sections ----------------------------------------------------------------

def test_eeoc_block_is_tagged(scan):
    s = scan("form_minimal")
    for field_id in ("gender", "hispanic_ethnicity", "veteran_status", "disability_status"):
        assert field(s, field_id).section == "eeoc"
        assert field(s, field_id).required is False


def test_demographic_block_is_tagged_and_separate_from_eeoc(scan):
    s = scan("form_demographic")
    assert field(s, "4005807007").section == "demographic"
    assert field(s, "gender").section == "eeoc"


def test_employer_authored_diversity_questions_stay_in_questions(scan):
    """A question that reads like EEOC but was authored by the employer sits in
    the ordinary questions block and is answered by rule, not structurally."""
    s = scan("form_multiselect")
    assert field(s, "question_36638878002").label == "I identify my race as"
    assert field(s, "question_36638878002").section == "questions"


def test_education_block_is_tagged_and_years_are_numeric(scan):
    s = scan("form_education")
    assert field(s, "school--0").section == "education"
    assert field(s, "school--0").kind == "react_select"
    assert field(s, "start-month--0").kind == "react_select"
    assert field(s, "start-year--0").kind == "number"
    assert field(s, "end-year--0").kind == "number"


def test_education_required_varies_by_board(scan):
    assert field(scan("form_education"), "school--0").required is False
    assert field(scan("form_demographic"), "school--0").required is True


def test_education_shape_varies_by_board(scan):
    """Months are present on some boards and absent on others."""
    assert scan("form_education").by_id("start-month--0") is not None
    assert scan("form_demographic").by_id("start-month--0") is None
    assert scan("form_demographic").by_id("discipline--0") is None


def test_employment_block_is_tagged(scan):
    """The employment block renders like education but under its own container,
    and every field in it is DOM-only. Untagged, these fall into `questions` and
    get keyword-matched as if the employer had authored them."""
    s = scan("form_employment")
    expected = {
        "company-name-0", "title-0", "start-date-month-0", "start-date-year-0",
        "end-date-month-0", "end-date-year-0", "current-role-0",
    }
    got = {f.name for f in s.fields if f.section == "employment"}
    assert got == expected
    assert field(s, "company-name-0").required is True
    assert field(s, "start-date-month-0").kind == "react_select"


def test_employment_checkbox_id_differs_from_its_name(scan):
    """`current-role` renders one checkbox whose id carries an option suffix the
    name does not. The id is the selector; the name is the API join key."""
    f = field(scan("form_employment"), "current-role-0_1")
    assert f.name == "current-role-0"
    assert f.kind == "checkbox"
    assert f.section == "employment"


def test_a_renamed_block_container_raises_instead_of_falling_into_questions():
    """The employment block was missed because an unrecognized container is
    silently `questions`. The id shape is a second, independent signal: if the
    two disagree the scan fails loud rather than handing employer-authored
    answer rules a field Greenhouse owns."""
    html = """
    <main><form id="application-form">
      <div class="renamed--container">
        <label for="company-name-0">Company name</label>
        <input id="company-name-0" name="company-name-0" type="text" aria-required="true"/>
      </div>
      <button type="submit">Submit</button>
    </form></main>
    """
    with pytest.raises(DomScanError, match="employment"):
        scan_form(html)


@pytest.mark.parametrize("name", FORM_FIXTURES)
def test_block_id_shapes_agree_with_their_containers(scan, name):
    """The cross-check in the *other* direction, which `scan_form` does not
    make itself.

    `scan_form` raises when an id has a block's shape but the container class
    put it somewhere else. It never checks the reverse: a container class that
    matches too broadly pulls an employer-authored question into a
    Greenhouse-owned block, where it is answered structurally instead of by
    rule, and nothing raises. So: every field the scan filed under one of the
    four blocks must also carry that block's id shape.
    """
    from src.apply.domscan import _BLOCK_ID_SHAPES

    shapes = dict(_BLOCK_ID_SHAPES)
    misfiled = [
        (f.id, f.section) for f in scan(name).fields
        if f.section in shapes and not shapes[f.section].match(f.id)
    ]
    assert misfiled == []


def test_the_captured_boards_between_them_exercise_every_block(scan):
    """The per-board check above is vacuous on a board that renders no
    Greenhouse-owned block at all (form_multiselect renders none). Across the
    five captures, all four blocks have to be hit, or the check is asserting
    nothing anywhere."""
    from src.apply.domscan import _BLOCK_ID_SHAPES

    seen = {f.section for name in FORM_FIXTURES for f in scan(name).fields}
    assert {block for block, _ in _BLOCK_ID_SHAPES} <= seen


def test_employment_years_are_text_not_number(scan):
    """Education renders its years as input[type=number]; employment renders
    them as plain text. The kind comes from what is rendered, not the block."""
    assert field(scan("form_education"), "start-year--0").kind == "number"
    assert field(scan("form_employment"), "start-date-year-0").kind == "text"


# --- the reason this module exists -------------------------------------------

@pytest.mark.parametrize("name", FORM_FIXTURES)
def test_dom_only_fields_survive_that_the_api_never_declares(scan, name):
    """The regression this module prevents: building the plan from the question
    API alone silently drops these."""
    s = scan(name)
    api_names = {
        f.name
        for q in parse_schema(load_fixture(name)).questions
        for f in q.fields
    }
    assert "country" not in api_names
    assert field(s, "country").required is True


def test_location_and_latlong_are_declared_but_not_rendered(scan):
    """`location` renders under a different id; lat/long render nowhere at all,
    so nothing can assert them (§2b)."""
    s = scan("form_minimal")
    api_names = {
        f.name for q in parse_schema(load_fixture("form_minimal")).questions for f in q.fields
    }
    assert {"location", "latitude", "longitude"} <= api_names
    assert s.by_id("location") is None
    assert s.by_id("latitude") is None
    assert s.by_id("longitude") is None
    assert field(s, "candidate-location").kind == "react_select"


def test_demographic_dom_ids_pair_with_the_api_question_ids(scan):
    """Demographic questions render with bare-numeric ids — the API's
    demographic_questions[].id — which is how reconcile pairs them."""
    s = scan("form_demographic")
    api_ids = {str(d.id) for d in parse_schema(load_fixture("form_demographic")).demographic}
    dom_ids = {f.id for f in s.fields if f.section == "demographic"}
    assert api_ids
    assert dom_ids == api_ids


def test_every_required_field_has_a_usable_selector(scan):
    for name in FORM_FIXTURES:
        for f in scan(name).fields:
            if f.required:
                assert f.id and f.kind, f"{name}: {f}"
