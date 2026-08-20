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

    def test_two_submit_buttons_raise_rather_than_picking_one(self):
        """`btn-submit` is a data-qa attribute, not a unique id — a board that
        renders a second one (a "save and finish later" twin) makes the
        selector ambiguous, and the guard refuses instead of clicking whichever
        Playwright resolves first on an already-filled form."""
        html = load_html("form_lever_full").replace(
            "</form>",
            '<button type="button" data-qa="btn-submit">Save for later</button></form>',
            1,
        )
        with patch("src.apply.lever.fetch_text", return_value=html):
            with pytest.raises(LeverScanError,
                               match="expected one submit button, found 2"):
                load_board(
                    "https://jobs.lever.co/widgetco/00000001-0000-0000-0000-000000000001"
                )

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

    def test_the_sponsorship_card_resolves_and_the_qualified_one_parks(
        self, answers, tailor_dir
    ):
        """This fixture is why the qualifier guard exists.

        Card 1 is the real-board label "Are you permanently authorized to work
        for **any employer** in the United States?" — for a `time_limited`
        status that is No, but it used to be answered Yes off
        `authorized_now`, which answers only "may you work here today". A
        scope-qualified authorization question is not answerable from the
        status alone, so it parks (§5's never-guess policy, previously applied
        to the sponsorship family only).

        Card 2 is the plain sponsorship question and still resolves.
        """
        reconciled = scan_lever_form(load_html("form_lever_full"))
        plan = build_plan(reconciled, answers, tailor_dir, ats="lever")
        card = "cards[00000020-0000-0000-0000-000000000020]"

        resolved = [f for f in plan.fields if f.id.startswith(card)]
        # Optional on this board, so it is left blank rather than parked —
        # §6's contract is unanswerable+required -> park, +optional -> skip.
        # Either way nothing false is submitted, which is the whole point.
        left_blank = [s for s in plan.skipped if s.id.startswith(card)]

        assert [f.tier for f in resolved] == ["B0"]
        assert "sponsorship" in resolved[0].label.casefold()
        assert resolved[0].value == "Yes"          # time_limited requires it

        assert len(left_blank) == 1
        assert "permanently authorized" in left_blank[0].label.casefold()
        assert "qualifies the scope" in left_blank[0].reason

    def test_the_same_question_parks_the_role_when_the_board_requires_it(
        self, answers, tailor_dir
    ):
        """The optional case above is only safe because the required case
        blocks. A board that makes the qualified question mandatory must stop
        the submission, not answer it."""
        from src.apply.answers import resolve
        from src.apply.domscan import DomOption
        from src.apply.reconcile import MergedField

        field = MergedField(
            id="q", name="q", required=True, kind="select", section="questions",
            multi=False, api_type="multi_value_single_select",
            label="Are you permanently authorized to work for any employer in "
                  "the United States?",
            options=(DomOption(value="1", label="Yes"), DomOption(value="0", label="No")),
        )
        r = resolve(field, answers)
        assert r.action == "park"
        assert r.tier == "B0"

    def test_eeoc_block_resolves_to_opt_outs(self, answers, tailor_dir):
        reconciled = scan_lever_form(load_html("form_lever_full"))
        plan = build_plan(reconciled, answers, tailor_dir, ats="lever")
        eeoc_fields = {f.id: f.value for f in plan.fields if f.id.startswith("eeo[")}
        assert eeoc_fields["eeo[gender]"] == "Decline to self-identify"
        assert eeoc_fields["eeo[disability]"] == "I do not want to answer"


class TestCheckboxes:
    """Lever renders checkboxes for pronouns, GDPR storage consent and custom
    card fields. `_scan_question` had no branch for them, so `scan_lever_form`
    raised `unknown input type 'checkbox'` and the whole board failed.

    Measured over 19 live Lever boards during the form harvest: **10 of them**
    render at least one checkbox, so more than half of Lever was unplannable.
    No committed fixture contained one, which is why the suite was green.
    """

    def _form(self, inner: str) -> str:
        html = load_html("form_lever_full")
        return html.replace(
            '<li class="application-question resume">',
            f'<li class="application-question">{inner}</li>'
            '<li class="application-question resume">',
            1,
        )

    def test_a_group_of_checkboxes_is_a_multi_valued_field(self):
        inner = (
            '<label><div class="application-label">Pronouns</div></label>'
            '<input type="checkbox" name="pronouns" value="she/her">'
            '<input type="checkbox" name="pronouns" value="he/him">'
            '<input type="checkbox" name="pronouns" value="they/them">'
        )
        reconciled = scan_lever_form(self._form(inner))
        f = next(f for f in reconciled.fields if f.id == "pronouns")
        assert f.kind == "checkbox_group"
        assert f.multi is True
        assert [o.value for o in f.options] == ["she/her", "he/him", "they/them"]

    def test_a_lone_valueless_checkbox_is_a_consent_tick_not_a_group(self):
        inner = (
            '<label><div class="application-label">Store my data</div></label>'
            '<input type="checkbox" name="consent[store]" required>'
        )
        reconciled = scan_lever_form(self._form(inner))
        f = next(f for f in reconciled.fields if f.id == "consent[store]")
        assert f.kind == "checkbox"
        assert f.multi is False
        assert f.required is True

    def test_a_lone_checkbox_with_a_truthy_value_is_still_a_consent_tick(self):
        # Real markup harvested from a live Lever board: a hidden
        # `value="0"` fallback sits next to the real checkbox, whose own
        # `value="1"` is just the checked-state idiom, not a menu of options —
        # and the label lives in a plain <span>, not `.application-label`.
        inner = (
            '<label><span class="consent-required">By applying for this '
            "position, your data will be processed as per the Privacy Policy."
            "</span>"
            '<input type="hidden" name="consent[store]" value="0">'
            '<input type="checkbox" name="consent[store]" value="1" required>'
            "</label>"
        )
        reconciled = scan_lever_form(self._form(inner))
        f = next(f for f in reconciled.fields if f.id == "consent[store]")
        assert f.kind == "checkbox"
        assert f.multi is False
        assert f.required is True
        assert f.options == ()
        assert "Privacy Policy" in f.label

    def test_the_rest_of_the_form_still_scans_around_it(self):
        inner = ('<label><div class="application-label">Pronouns</div></label>'
                 '<input type="checkbox" name="pronouns" value="they/them">')
        before = len(scan_lever_form(load_html("form_lever_full")).fields)
        after = len(scan_lever_form(self._form(inner)).fields)
        assert after == before + 1


class TestStructuredLocation:
    """Lever's Current Location renders as `input[type=text]` and is not a text
    field: it searches a place taxonomy, and the board posts the structured
    `selectedLocation` the suggestion click resolves. Scanned as plain text, the
    fill typed the string, never picked a suggestion, left the hidden partner
    empty, and the board answered "there was an error verifying your
    application".
    """

    def _form(self, inner: str) -> str:
        html = load_html("form_lever_full")
        return html.replace(
            '<li class="application-question resume">',
            f'<li class="application-question">{inner}</li>'
            '<li class="application-question resume">',
            1,
        )

    LOCATION = (
        '<label><div class="application-label">Current location '
        '<span class="required">\u271f</span></div>'
        '<div class="application-field">'
        '<input class="location-input" id="location-input" type="text" '
        'maxlength="100" name="location" required="">'
        '<input id="selected-location" type="hidden" name="selectedLocation">'
        '</div></label>'
    )

    def test_a_location_with_a_hidden_partner_is_type_and_pick(self):
        reconciled = scan_lever_form(self._form(self.LOCATION))
        f = next(f for f in reconciled.fields if f.id == "location")
        assert f.kind == "react_select", (
            "a location scanned as `text` gets typed but never resolved"
        )
        assert f.required is True

    def test_a_plain_text_field_stays_text(self):
        inner = (
            '<label><div class="application-label">Current company</div>'
            '<div class="application-field">'
            '<input type="text" name="org"></div></label>'
        )
        reconciled = scan_lever_form(self._form(inner))
        assert next(f for f in reconciled.fields if f.id == "org").kind == "text"

    def test_the_partner_decides_it_not_the_field_name(self):
        """Detected structurally, so a board that renames the visible input
        still resolves as type-and-pick."""
        renamed = self.LOCATION.replace('name="location"', 'name="whereabouts"')
        reconciled = scan_lever_form(self._form(renamed))
        f = next(f for f in reconciled.fields if f.id == "whereabouts")
        assert f.kind == "react_select"

    def test_the_hidden_partner_is_not_itself_a_fillable_field(self):
        reconciled = scan_lever_form(self._form(self.LOCATION))
        assert not [f for f in reconciled.fields if f.id == "selectedLocation"]
