"""Ashby's rendered form -> a merged field set, with no API to reconcile
against — the scan output is the merge, straight from the DOM (§12a).

Scan-only: this module has no fill driver (see ashby.py's module docstring),
so there is no submit/captcha/driver coverage here, unlike test_lever.py.

No network: fetch_text is stubbed everywhere.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from src.apply.ashby import (
    AshbyScanError,
    ApplyUrlError,
    PostingExpired,
    fetch_form,
    load_board,
    parse_posting,
    scan_ashby_form,
)
from src.apply.plan import build_plan, plan_for_board

from .conftest import load_html


class TestParsePosting:
    def test_the_slug_and_job_id(self):
        posting = parse_posting(
            "https://jobs.ashbyhq.com/widgetco/00000001-0000-0000-0000-000000000001"
        )
        assert (posting.slug, posting.job_id) == (
            "widgetco", "00000001-0000-0000-0000-000000000001",
        )

    def test_token_property_is_the_job_id_for_plan_for_board(self):
        posting = parse_posting(
            "https://jobs.ashbyhq.com/widgetco/00000001-0000-0000-0000-000000000001"
        )
        assert posting.token == posting.job_id

    def test_no_url(self):
        with pytest.raises(ApplyUrlError, match="no URL"):
            parse_posting("")

    def test_an_ashby_url_with_no_job_id(self):
        with pytest.raises(ApplyUrlError, match="no job id"):
            parse_posting("https://jobs.ashbyhq.com/widgetco")

    def test_not_an_ashby_url(self):
        with pytest.raises(ApplyUrlError, match="not an Ashby posting URL"):
            parse_posting("https://boards.greenhouse.io/widgetco/jobs/123")


class TestScanAshbyForm:
    def test_identity_fields_key_off_data_field_path(self):
        r = scan_ashby_form(load_html("form_ashby_minimal"))
        by_id = {f.id: f for f in r.fields}
        assert "_systemfield_name" in by_id
        assert "_systemfield_email" in by_id
        assert "_systemfield_location" in by_id
        assert by_id["_systemfield_location"].kind == "combobox"

    def test_resume_field_is_aliased_to_the_greenhouse_artifact_id(self):
        r = scan_ashby_form(load_html("form_ashby_minimal"))
        by_id = {f.id: f for f in r.fields}
        assert "resume" in by_id
        assert by_id["resume"].kind == "file"
        assert by_id["resume"].required is True

    def test_required_signal_is_a_class_prefix_not_a_fixed_hash(self):
        # LinkedIn is required on the minimal fixture, optional on the
        # diversity one — read per field, never assumed (§12a).
        minimal = {f.label: f.required for f in scan_ashby_form(load_html("form_ashby_minimal")).fields}
        diversity = {f.label: f.required for f in scan_ashby_form(load_html("form_ashby_diversity")).fields}
        assert minimal["LinkedIn Profile"] is True
        assert diversity["LinkedIn Profile"] is False

    def test_yesno_widget_is_scanned_with_a_fixed_yes_no_option_pair(self):
        r = scan_ashby_form(load_html("form_ashby_minimal"))
        toggle = next(f for f in r.fields if "NY or SF office" in f.label)
        assert toggle.kind == "yesno"
        assert [o.label for o in toggle.options] == ["Yes", "No"]

    def test_combobox_location_has_no_options_but_a_stable_id(self):
        r = scan_ashby_form(load_html("form_ashby_diversity"))
        location = next(f for f in r.fields if f.id == "_systemfield_location")
        assert location.kind == "combobox"
        assert location.options == ()
        assert location.required is True

    def test_diversity_radio_group_is_single_select_demographic(self):
        r = scan_ashby_form(load_html("form_ashby_diversity"))
        age = next(f for f in r.fields if f.label == "What is your current age?")
        assert age.kind == "radio_group"
        assert age.multi is False
        assert age.section == "demographic"
        assert "I prefer not to answer" in [o.label for o in age.options]

    def test_diversity_checkbox_group_options_are_the_boxes_own_names(self):
        r = scan_ashby_form(load_html("form_ashby_diversity"))
        ethnicity = next(
            f for f in r.fields
            if f.label.startswith("Which ethnicity")
        )
        assert ethnicity.kind == "checkbox_group"
        assert ethnicity.multi is True
        assert ethnicity.section == "demographic"
        values = {o.value for o in ethnicity.options}
        assert "Asian or Asian American" in values
        assert "White" in values
        assert "I prefer not to answer" in values

    def test_diversity_survey_fields_are_never_required(self):
        r = scan_ashby_form(load_html("form_ashby_diversity"))
        for f in r.fields:
            if f.section == "demographic":
                assert f.required is False

    def test_a_field_never_appears_twice(self):
        r = scan_ashby_form(load_html("form_ashby_diversity"))
        ids = [f.id for f in r.fields]
        assert len(ids) == len(set(ids))

    def test_no_api_only_ever_since_there_is_no_api(self):
        r = scan_ashby_form(load_html("form_ashby_minimal"))
        assert r.api_only == ()

    def test_empty_document_raises(self):
        with pytest.raises(AshbyScanError, match="empty"):
            scan_ashby_form("")

    def test_no_form_root_raises(self):
        with pytest.raises(AshbyScanError, match='id="form"'):
            scan_ashby_form("<html><body>not a form</body></html>")

    def test_unknown_input_type_raises(self):
        html = load_html("form_ashby_minimal").replace(
            'name="00000004-0000-0000-0000-000000000004" required="" '
            'id="00000004-0000-0000-0000-000000000004" type="text"',
            'name="00000004-0000-0000-0000-000000000004" required="" '
            'id="00000004-0000-0000-0000-000000000004" type="color"',
        )
        with pytest.raises(AshbyScanError, match="unknown input type"):
            scan_ashby_form(html)


class TestFetchForm:
    def test_a_404_is_an_ordinary_expiry(self):
        from src.discovery.sources.ats.http import CareersError

        posting = parse_posting("https://jobs.ashbyhq.com/widgetco/00000001-0000-0000-0000-000000000001")
        with patch("src.apply.ashby.fetch_text", side_effect=CareersError("gone", status=404)):
            with pytest.raises(PostingExpired):
                fetch_form(posting)


class TestLoadBoard:
    def test_submit_selector_and_no_captcha(self):
        html = load_html("form_ashby_minimal")
        with patch("src.apply.ashby.fetch_text", return_value=html):
            board = load_board(
                "https://jobs.ashbyhq.com/widgetco/00000001-0000-0000-0000-000000000001"
            )
        assert board.scan.submit_selector == '#form .ashby-application-form-submit-button'
        assert board.scan.submit_disabled is False
        assert board.requires_captcha is False
        assert board.slug == "widgetco"

    def test_plan_for_board_carries_ats_through(self, answers, tailor_dir):
        html = load_html("form_ashby_minimal")
        with patch("src.apply.ashby.fetch_text", return_value=html):
            board = load_board(
                "https://jobs.ashbyhq.com/widgetco/00000001-0000-0000-0000-000000000001"
            )
        plan = plan_for_board(board, answers, tailor_dir, job_id="deadbeef", ats="ashby")
        assert plan.ats == "ashby"
        assert plan.requires_captcha is False
        assert plan.job_id == "deadbeef"
        assert plan.board == "widgetco"


class TestEndToEndResolution:
    """The two fixtures resolved against the shared synthetic answers config —
    the same cross-check test_plan.py runs for every Greenhouse fixture."""

    def test_minimal_resolves_identity_location_resume_and_the_linkedin_rule(self, answers, tailor_dir):
        reconciled = scan_ashby_form(load_html("form_ashby_minimal"))
        plan = build_plan(reconciled, answers, tailor_dir, ats="ashby")
        assert plan.files and plan.files[0].id == "resume"
        by_id = {f.id: f.value for f in plan.fields}
        assert by_id["_systemfield_name"] == "Alex Example"
        assert by_id["_systemfield_location"] == answers.identity["location"]

    def test_minimal_parks_only_on_genuinely_unanswerable_customs(self, answers, tailor_dir):
        reconciled = scan_ashby_form(load_html("form_ashby_minimal"))
        plan = build_plan(reconciled, answers, tailor_dir, ats="ashby")
        # Phone (no rule) and two custom yes/no + one textarea question with no
        # matching rule are the only fields nothing here can answer.
        parked_labels = {u.label for u in plan.unmapped}
        assert "Phone" in parked_labels
        assert any("NY or SF office" in label for label in parked_labels)

    def test_diversity_survey_resolves_to_decline_answers(self, answers, tailor_dir):
        reconciled = scan_ashby_form(load_html("form_ashby_diversity"))
        plan = build_plan(reconciled, answers, tailor_dir, ats="ashby")
        demographic_values = {
            f.label: f.value for f in plan.fields
            if f.id in {o.id for o in reconciled.fields if o.section == "demographic"}
        }
        assert demographic_values["What is your current age?"] == "I prefer not to answer"
        assert demographic_values["Which ethnicity(ies) do you identify with? Please select all that apply."] == (
            "I prefer not to answer",
        )

    def test_diversity_sponsorship_question_resolves_via_work_authorization(self, answers, tailor_dir):
        reconciled = scan_ashby_form(load_html("form_ashby_diversity"))
        plan = build_plan(reconciled, answers, tailor_dir, ats="ashby")
        sponsorship = next(
            f for f in plan.fields if "require sponsorship" in f.label
        )
        assert sponsorship.tier == "B0"
