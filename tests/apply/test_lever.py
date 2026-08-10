"""Lever's rendered form -> a merged field set, with no API to reconcile
against — the scan output is the merge, straight from the DOM (§12a).

No network: fetch_text is stubbed everywhere.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from src.apply.lever import (
    ApplyUrlError,
    LeverScanError,
    PostingExpired,
    fetch_form,
    has_captcha,
    load_board,
    parse_posting,
    scan_lever_form,
)
from src.apply.plan import build_plan, plan_for_board

from .conftest import load_html


class TestParsePosting:
    def test_the_posting_id_and_slug(self):
        posting = parse_posting(
            "https://jobs.lever.co/widgetco/00000001-0000-0000-0000-000000000001"
        )
        assert (posting.slug, posting.posting_id) == (
            "widgetco", "00000001-0000-0000-0000-000000000001",
        )

    def test_the_apply_suffix_is_optional_on_input_but_always_on_output(self):
        posting = parse_posting(
            "https://jobs.lever.co/widgetco/00000001-0000-0000-0000-000000000001/apply"
        )
        assert posting.form_url.endswith("/apply")

    def test_token_property_is_the_posting_id_for_plan_for_board(self):
        posting = parse_posting(
            "https://jobs.lever.co/widgetco/00000001-0000-0000-0000-000000000001"
        )
        assert posting.token == posting.posting_id

    def test_no_url(self):
        with pytest.raises(ApplyUrlError, match="no URL"):
            parse_posting("")

    def test_a_lever_url_with_no_posting_id(self):
        with pytest.raises(ApplyUrlError, match="no posting id"):
            parse_posting("https://jobs.lever.co/widgetco")

    def test_not_a_lever_url(self):
        with pytest.raises(ApplyUrlError, match="not a Lever posting URL"):
            parse_posting("https://boards.greenhouse.io/widgetco/jobs/123")


class TestScanLeverForm:
    def test_identity_fields_key_off_name_not_id(self):
        r = scan_lever_form(load_html("form_lever_minimal"))
        by_id = {f.id: f for f in r.fields}
        for expected in ("resume", "name", "email", "phone", "location", "org"):
            assert expected in by_id, by_id.keys()
            assert by_id[expected].id == by_id[expected].name

    def test_required_signal_is_read_per_field_not_assumed(self):
        # The same board asks for resume/location as required on one posting
        # (minimal fixture) and optional on another (full fixture) — required
        # has to come from what's actually rendered, never a fixed policy.
        minimal = {f.id: f.required for f in scan_lever_form(load_html("form_lever_minimal")).fields}
        full = {f.id: f.required for f in scan_lever_form(load_html("form_lever_full")).fields}
        assert minimal["resume"] is True
        assert full["resume"] is False
        assert minimal["location"] is True
        assert full["location"] is False

    def test_required_via_required_field_class_on_a_select_card(self):
        r = scan_lever_form(load_html("form_lever_full"))
        by_id = {f.id: f for f in r.fields}
        previous_employee = by_id["cards[0000000b-0000-0000-0000-00000000000b][field0]"]
        assert previous_employee.required is True
        assert previous_employee.kind == "select"
        assert {o.label for o in previous_employee.options} == {"Yes", "No"}

    def test_native_select_options_exclude_the_placeholder(self):
        r = scan_lever_form(load_html("form_lever_full"))
        by_id = {f.id: f for f in r.fields}
        age = by_id["cards[00000005-0000-0000-0000-000000000005][field0]"]
        assert "" not in [o.value for o in age.options]
        assert [o.label for o in age.options] == ["Yes", "No"]

    def test_textarea_card_is_scanned(self):
        r = scan_lever_form(load_html("form_lever_full"))
        by_id = {f.id: f for f in r.fields}
        comp = by_id["cards[00000007-0000-0000-0000-000000000007][field0]"]
        assert comp.kind == "textarea"
        assert "compensation" in comp.label.casefold()

    def test_eeoc_section_is_tagged_and_ids_are_raw_dom_names(self):
        r = scan_lever_form(load_html("form_lever_full"))
        eeoc = {f.id: f for f in r.fields if f.section == "eeoc"}
        assert set(eeoc) == {
            "eeo[gender]", "eeo[race]", "eeo[veteran]", "eeo[disability]",
            "eeo[disabilitySignature]", "eeo[disabilitySignatureDate]",
        }
        assert eeoc["eeo[gender]"].kind == "select"
        assert eeoc["eeo[race]"].kind == "radio_group"

    def test_race_radio_group_collects_every_option_once(self):
        r = scan_lever_form(load_html("form_lever_full"))
        race = next(f for f in r.fields if f.id == "eeo[race]")
        assert race.kind == "radio_group"
        labels = [o.label for o in race.options]
        assert "Decline to self-identify" in labels
        assert len(labels) == len(set(labels))

    def test_disability_signature_fields_are_not_required_and_not_lost(self):
        r = scan_lever_form(load_html("form_lever_full"))
        by_id = {f.id: f for f in r.fields}
        assert by_id["eeo[disabilitySignature]"].required is False
        assert by_id["eeo[disabilitySignatureDate]"].required is False

    def test_a_field_never_appears_twice(self):
        r = scan_lever_form(load_html("form_lever_full"))
        names = [f.name for f in r.fields]
        assert len(names) == len(set(names))

    def test_no_api_only_ever_since_there_is_no_api(self):
        r = scan_lever_form(load_html("form_lever_full"))
        assert r.api_only == ()

    def test_empty_document_raises(self):
        with pytest.raises(LeverScanError, match="empty"):
            scan_lever_form("")

    def test_no_application_form_raises(self):
        with pytest.raises(LeverScanError, match="no <form"):
            scan_lever_form("<html><body>not a form</body></html>")

    def test_unknown_input_type_raises(self):
        html = load_html("form_lever_minimal").replace(
            '<input type="text" data-qa="org-input" name="org">',
            '<input type="color" data-qa="org-input" name="org">',
        )
        with pytest.raises(LeverScanError, match="unknown input type"):
            scan_lever_form(html)


class TestHasCaptcha:
    def test_both_fixtures_render_it(self):
        assert has_captcha(load_html("form_lever_minimal")) is True
        assert has_captcha(load_html("form_lever_full")) is True

    def test_absent_is_false(self):
        assert has_captcha("<html><body><form id='application-form'></form></body></html>") is False


class TestFetchForm:
    def test_a_404_is_an_ordinary_expiry(self):
        from src.discovery.sources.ats.http import CareersError

        posting = parse_posting("https://jobs.lever.co/widgetco/00000001-0000-0000-0000-000000000001")
        with patch("src.apply.lever.fetch_text", side_effect=CareersError("gone", status=404)):
            with pytest.raises(PostingExpired):
                fetch_form(posting)


class TestLoadBoard:
    def test_submit_selector_and_captcha_flag(self):
        html = load_html("form_lever_full")
        with patch("src.apply.lever.fetch_text", return_value=html):
            board = load_board(
                "https://jobs.lever.co/widgetco/00000001-0000-0000-0000-000000000001"
            )
        assert board.scan.submit_selector == '#application-form [data-qa="btn-submit"]'
        assert board.scan.submit_disabled is False
        assert board.requires_captcha is True
        assert board.slug == "widgetco"

    def test_plan_for_board_carries_ats_and_captcha_through(self, answers, tailor_dir):
        html = load_html("form_lever_minimal")
        with patch("src.apply.lever.fetch_text", return_value=html):
            board = load_board(
                "https://jobs.lever.co/widgetco/00000001-0000-0000-0000-000000000001"
            )
        plan = plan_for_board(
            board, answers, tailor_dir, job_id="deadbeef",
            ats="lever", requires_captcha=board.requires_captcha,
        )
        assert plan.ats == "lever"
        assert plan.requires_captcha is True
        assert plan.job_id == "deadbeef"
        assert plan.board == "widgetco"


class TestEndToEndResolution:
    """The two fixtures resolved against the shared synthetic answers config —
    the same cross-check test_plan.py runs for every Greenhouse fixture."""

    def test_minimal_fully_resolves(self, answers, tailor_dir):
        reconciled = scan_lever_form(load_html("form_lever_minimal"))
        plan = build_plan(reconciled, answers, tailor_dir, ats="lever")
        assert plan.unmapped == ()
        assert plan.files and plan.files[0].id == "resume"

    def test_full_parks_only_on_genuinely_unanswerable_customs(self, answers, tailor_dir):
        reconciled = scan_lever_form(load_html("form_lever_full"))
        plan = build_plan(reconciled, answers, tailor_dir, ats="lever")
        # Every parked field is a bespoke employer card with no matching rule
        # -- not an identity or EEOC field, which must always resolve.
        assert plan.unmapped
        for u in plan.unmapped:
            assert u.id.startswith("cards[")

    def test_work_authorization_resolves_both_cards(self, answers, tailor_dir):
        reconciled = scan_lever_form(load_html("form_lever_full"))
        plan = build_plan(reconciled, answers, tailor_dir, ats="lever")
        auth_fields = [
            f for f in plan.fields
            if f.id.startswith("cards[00000020-0000-0000-0000-000000000020]")
        ]
        assert len(auth_fields) == 2
        assert all(f.tier == "B0" for f in auth_fields)

    def test_eeoc_block_resolves_to_opt_outs(self, answers, tailor_dir):
        reconciled = scan_lever_form(load_html("form_lever_full"))
        plan = build_plan(reconciled, answers, tailor_dir, ats="lever")
        eeoc_fields = {f.id: f.value for f in plan.fields if f.id.startswith("eeo[")}
        assert eeoc_fields["eeo[gender]"] == "Decline to self-identify"
        assert eeoc_fields["eeo[disability]"] == "I do not want to answer"
