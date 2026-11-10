"""Ashby's application form -> a merged field set.

`load_board` reads the GraphQL API, because a live Ashby posting serves no
form in its HTML at all.

No network: fetch_json_post is stubbed everywhere, and every
`load_board` call also stubs `fetch_dom_enrichment` — otherwise it would open a
real headless browser against a fake URL. `TestDomEnrichment` below is the one
place that exercises `fetch_dom_enrichment` itself, against a fake Playwright.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from applyourself.apply import ashby
from applyourself.apply.ashby import (
    ApplyUrlError,
    AshbyScanError,
    PostingExpired,
    fields_from_application_form,
    load_board,
    parse_posting,
)
from applyourself.apply.plan import build_plan, plan_for_board

from .conftest import load_api


class TestParsePosting:
    def test_the_slug_and_job_id(self):
        posting = parse_posting(
            "https://jobs.ashbyhq.com/widgetco/00000001-0000-0000-0000-000000000001"
        )
        assert (posting.slug, posting.job_id) == (
            "widgetco",
            "00000001-0000-0000-0000-000000000001",
        )

    def test_the_form_url_is_the_application_page_not_the_ad(self):
        """`fill_plan` navigates to `plan.form_url`. The posting URL renders
        the job ad, which draws no fields at all, so a driver pointed there
        times out waiting for a form that page never shows. Greenhouse points
        at its embed form and Lever at `/apply` for the same reason."""
        posting = parse_posting(
            "https://jobs.ashbyhq.com/widgetco/00000001-0000-0000-0000-000000000001"
        )
        assert posting.form_url.endswith("/application")
        assert posting.form_url == posting.posting_url + "/application"

    def test_a_url_that_already_names_the_form_does_not_double_up(self):
        """The Posting is canonical and the URLs derive from it, so pasting
        either spelling in resolves to the same one."""
        posting = parse_posting(
            "https://jobs.ashbyhq.com/widgetco/00000001-0000-0000-0000-000000000001/application"
        )
        assert posting.form_url.count("/application") == 1

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


ASHBY_URL = "https://jobs.ashbyhq.com/widgetco/00000001-0000-0000-0000-000000000001"


class TestLoadBoard:
    """`load_board` reads the GraphQL API, not the page. A static GET of a live
    Ashby posting returns a ~32 KB shell with zero `<form>` elements — measured
    over 6 orgs — so the DOM path it used to take could never work outside the
    fixtures."""

    def _board(self, payload=None):
        with (
            patch(
                "applyourself.apply.ashby.fetch_json_post",
                return_value=payload or load_api("api_ashby_form"),
            ),
            patch("applyourself.apply.ashby.fetch_dom_enrichment", return_value={}),
        ):
            return load_board(ASHBY_URL)

    def test_the_submit_selector_is_the_shared_one(self):
        board = self._board()
        assert board.scan.submit_selector == ashby.SUBMIT_SELECTOR
        assert ashby._SUBMIT_CLASS in ashby.SUBMIT_SELECTOR

    def test_no_captcha_wait_because_ashbys_recaptcha_is_invisible(self):
        """Ashby loads reCAPTCHA v3 — score-based, with no challenge for a
        person to solve. Lever's hCaptcha does block on a human; treating the
        two the same would hang every unattended Ashby submit."""
        board = self._board()
        assert board.requires_captcha is False
        assert board.slug == "widgetco"

    def test_the_title_comes_from_the_api(self):
        board = self._board()
        assert board.schema.title == "Widget Engineer"
        assert board.schema.company_name == "widgetco"

    def test_plan_for_board_carries_ats_through(self, answers, tailor_dir):
        board = self._board()
        plan = plan_for_board(board, answers, tailor_dir, job_id="deadbeef", ats="ashby")
        assert plan.ats == "ashby"
        assert plan.requires_captcha is False
        assert plan.job_id == "deadbeef"
        assert plan.board == "widgetco"

    def test_a_percent_encoded_org_is_decoded_for_the_api(self):
        """Orgs whose page name has a space in it appear as `Hippocratic%20AI`
        in the URL; the API wants the decoded name."""
        seen = {}

        def capture(url, body, **kw):
            seen.update(body["variables"])
            return load_api("api_ashby_form")

        with (
            patch("applyourself.apply.ashby.fetch_json_post", side_effect=capture),
            patch("applyourself.apply.ashby.fetch_dom_enrichment", return_value={}),
        ):
            load_board(
                "https://jobs.ashbyhq.com/Gasket%20Works/00000001-0000-0000-0000-000000000001"
            )
        assert seen["organizationHostedJobsPageName"] == "Gasket Works"

    def test_a_null_job_posting_is_an_expired_posting(self):
        """The API answers 200 with a null node rather than 404. 5 of the first
        40 live URLs came back this way — ordinary, not breakage."""
        with pytest.raises(PostingExpired):
            self._board({"data": {"jobPosting": None}})

    def test_graphql_errors_are_reported_not_swallowed(self):
        with pytest.raises(AshbyScanError, match="Cannot query field"):
            self._board({"errors": [{"message": "Cannot query field 'nope'"}], "data": None})


class TestDomEnrichment:
    """`fetch_dom_enrichment` against a fake Playwright — no real browser, no
    real network. It matches by class (`data-field-path` / the description
    sibling), never by a known field id or label, so a board with instructional
    text under any field surfaces it — not just the two known cases (LiveFlow,
    Adaptyv) that motivated it."""

    @staticmethod
    def _fake_playwright(descriptions):
        class Page:
            def goto(self, *a, **kw):
                pass

            def wait_for_selector(self, *a, **kw):
                pass

            def eval_on_selector_all(self, *a, **kw):
                return descriptions

        class Ctx:
            pages: list = []

            def new_page(self):
                return Page()

            def close(self):
                pass

        class Chromium:
            @staticmethod
            def launch_persistent_context(**kw):
                return Ctx()

        class P:
            chromium = Chromium()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return lambda: P()

    def test_returns_whatever_the_page_carries(self, monkeypatch):
        monkeypatch.setattr(
            ashby,
            "require_playwright",
            lambda: self._fake_playwright({"q1": "Please explain briefly."}),
        )
        result = ashby.fetch_dom_enrichment(ASHBY_URL)
        assert result == {"q1": "Please explain briefly."}

    def test_load_board_merges_descriptions_into_matching_fields(self, monkeypatch):
        monkeypatch.setattr(
            ashby,
            "fetch_dom_enrichment",
            lambda url, timeout=30: {"_systemfield_name": "Legal name as on ID."},
        )
        with patch(
            "applyourself.apply.ashby.fetch_json_post", return_value=load_api("api_ashby_form")
        ):
            board = load_board(ASHBY_URL)
        by_id = {f.id: f for f in board.reconciled.fields}
        assert by_id["_systemfield_name"].description == "Legal name as on ID."
        assert by_id["_systemfield_email"].description == ""

    def test_a_file_fields_description_matches_by_its_real_dom_path(self, monkeypatch):
        """`resume`'s `MergedField.id` is aliased away from
        `_systemfield_resume`, the DOM's actual `data-field-path` — the
        description dict is keyed by that real path, so the merge must match
        on `.name`, not `.id`."""
        monkeypatch.setattr(
            ashby,
            "fetch_dom_enrichment",
            lambda url, timeout=30: {"_systemfield_resume": "PDF only, 5MB max."},
        )
        with patch(
            "applyourself.apply.ashby.fetch_json_post", return_value=load_api("api_ashby_form")
        ):
            board = load_board(ASHBY_URL)
        by_id = {f.id: f for f in board.reconciled.fields}
        assert by_id["resume"].name == "_systemfield_resume"
        assert by_id["resume"].description == "PDF only, 5MB max."

    def test_a_failed_enrichment_degrades_to_no_descriptions_not_a_broken_plan(self, monkeypatch):
        def boom(url, timeout=30):
            raise RuntimeError("chrome not installed")

        monkeypatch.setattr(ashby, "fetch_dom_enrichment", boom)
        with patch(
            "applyourself.apply.ashby.fetch_json_post", return_value=load_api("api_ashby_form")
        ):
            board = load_board(ASHBY_URL)
        assert all(f.description == "" for f in board.reconciled.fields)

    def test_missing_playwright_also_degrades_rather_than_raising(self, monkeypatch):
        # `require_playwright()` raises SystemExit, not Exception, when the
        # driver is not installed — the one case `_with_dom_descriptions`
        # must catch on top of the ordinary Exception path.
        def boom(url, timeout=30):
            raise SystemExit("ERROR: playwright not installed...")

        monkeypatch.setattr(ashby, "fetch_dom_enrichment", boom)
        with patch(
            "applyourself.apply.ashby.fetch_json_post", return_value=load_api("api_ashby_form")
        ):
            board = load_board(ASHBY_URL)
        assert all(f.description == "" for f in board.reconciled.fields)


class TestApiFieldMapping:
    """`field.type` -> `MergedField.kind`, over the fixture that carries every
    type observed across 150 live boards."""

    def _fields(self):
        board_fields = fields_from_application_form(
            load_api("api_ashby_form")["data"]["jobPosting"]
        )
        return {f.id: f for f in board_fields.fields}

    def test_every_scalar_type_maps_to_a_kind_the_planner_speaks(self):
        by_id = self._fields()
        kinds = {f.id: f.kind for f in by_id.values()}
        assert kinds["_systemfield_name"] == "text"  # String
        assert kinds["_systemfield_email"] == "text"  # Email
        assert kinds["_systemfield_location"] == "combobox"  # Location
        assert kinds["resume"] == "file"  # File
        assert kinds["20000000-0000-0000-0000-000000000003"] == "text"  # Phone
        assert kinds["20000000-0000-0000-0000-000000000004"] == "text"  # Url
        assert kinds["20000000-0000-0000-0000-000000000005"] == "yesno"  # Boolean
        assert kinds["20000000-0000-0000-0000-000000000007"] == "textarea"  # LongText
        assert kinds["20000000-0000-0000-0000-000000000008"] == "number"  # Number
        assert kinds["20000000-0000-0000-0000-000000000009"] == "date"  # Date
        assert kinds["20000000-0000-0000-0000-00000000000a"] == "select"  # ValueSelect
        assert kinds["20000000-0000-0000-0000-00000000000b"] == "select"  # MultiValueSelect

    def test_a_boolean_gets_the_yes_no_pair_the_resolvers_read(self):
        """`answers.py`'s work-authorization and opt-out logic reads
        `field.options`; the API sends a bare true/false type with none."""
        field = self._fields()["20000000-0000-0000-0000-000000000005"]
        assert [o.label for o in field.options] == ["Yes", "No"]

    def test_selectable_values_become_options(self):
        field = self._fields()["20000000-0000-0000-0000-00000000000a"]
        assert [o.label for o in field.options] == ["Job board", "Referral", "Company website"]
        assert [o.value for o in field.options] == ["1001", "1002", "1003"]

    def test_multi_comes_from_is_many_not_from_the_type_name(self):
        """`MultiValueSelect` means "choose among several values", not "choose
        several" — `isMany` was false on all 39 occurrences across 150 boards,
        and no board in the sample had `isMany: true` at all."""
        assert self._fields()["20000000-0000-0000-0000-00000000000b"].multi is False

    def test_the_resume_aliases_to_the_id_tailor_defers_to(self):
        assert "resume" in self._fields()
        assert "_systemfield_resume" not in self._fields()

    def test_the_cover_letter_is_recognized_by_title_not_by_path(self):
        """Ashby has no cover-letter systemfield. Across 112 boards it arrives
        as an employer-authored UUID titled some spelling of "Cover Letter"."""
        assert "cover_letter" in self._fields()

    def test_another_file_field_keeps_its_own_id(self):
        """ "Additional Attachments" is a real optional upload with no /tailor
        artifact behind it — aliasing it to a known id would attach the wrong
        document."""
        other = self._fields()["20000000-0000-0000-0000-000000000002"]
        assert other.kind == "file"
        assert other.required is False

    def test_a_second_upload_titled_cv_keeps_its_own_id(self):
        """Only the resume systemfield is `resume`. A required upload whose
        title also says CV used to take the same id and be dropped by the
        dedupe, so it never reached the plan or the user."""
        form = _form_with_files(
            ("_systemfield_resume", "Resume", True),
            ("30000000-0000-0000-0000-000000000001", "Writing sample (not your CV)", True),
        )
        by_id = {f.id: f for f in fields_from_application_form(form).fields}
        assert by_id["resume"].name == "_systemfield_resume"
        sample = by_id["30000000-0000-0000-0000-000000000001"]
        assert (sample.kind, sample.required) == ("file", True)

    def test_the_systemfield_wins_even_when_a_titled_upload_comes_first(self):
        form = _form_with_files(
            ("30000000-0000-0000-0000-000000000001", "CV / portfolio link", False),
            ("_systemfield_resume", "Resume", True),
        )
        by_id = {f.id: f for f in fields_from_application_form(form).fields}
        assert by_id["resume"].name == "_systemfield_resume"
        assert "30000000-0000-0000-0000-000000000001" in by_id

    def test_with_no_systemfield_the_first_titled_upload_is_the_resume(self):
        """Some boards render the resume under a UUID path."""
        form = _form_with_files(
            ("30000000-0000-0000-0000-000000000001", "Resume/CV", True),
            ("30000000-0000-0000-0000-000000000002", "Another CV", False),
        )
        by_id = {f.id: f for f in fields_from_application_form(form).fields}
        assert by_id["resume"].name == "30000000-0000-0000-0000-000000000001"
        assert "30000000-0000-0000-0000-000000000002" in by_id

    def test_two_different_fields_on_one_id_raise(self):
        form = _form_with_files(
            ("30000000-0000-0000-0000-000000000001", "Portfolio", True),
            ("30000000-0000-0000-0000-000000000001", "Portfolio", True),
        )
        # The same path twice is one field, kept once.
        assert len(fields_from_application_form(form).fields) == 1
        entries = form["applicationForm"]["sections"][0]["fieldEntries"]
        entries.append(
            {
                "id": "e-x",
                "isRequired": False,
                "field": {"type": "String", "path": "cover_letter", "title": "Cover letter note"},
            }
        )
        entries.append(
            {
                "id": "e-y",
                "isRequired": False,
                "field": {
                    "type": "File",
                    "path": "_systemfield_coverletter",
                    "title": "Cover Letter",
                },
            }
        )
        with pytest.raises(AshbyScanError, match="two fields map to id 'cover_letter'"):
            fields_from_application_form(form)

    def test_required_comes_from_the_entry_not_the_field(self):
        by_id = self._fields()
        assert by_id["_systemfield_name"].required is True
        assert by_id["20000000-0000-0000-0000-000000000004"].required is False

    def test_an_unknown_type_raises_rather_than_guessing(self):
        """Loud, matching the DOM scanner's unknown-input behaviour — a guessed
        mapping on a legal or compensation question is worse than a failure."""
        payload = load_api("api_ashby_form")
        entry = payload["data"]["jobPosting"]["applicationForm"]["sections"][0]["fieldEntries"][0]
        entry["field"]["type"] = "SomeTypeAshbyAddedLater"
        with pytest.raises(AshbyScanError, match="unknown Ashby field type"):
            fields_from_application_form(payload["data"]["jobPosting"])


class TestEducationHistoryIsExpandedNotRejected:
    """`EducationHistory` is one field entry carrying a whole repeating
    sub-form, with each sub-field's requirement declared inline. Greenhouse
    sends the same block as separate controls that `answers.py` already
    resolves, so the entry is expanded into those ids rather than raising."""

    FIELD = {
        "id": "9420a915-0000-0000-0000-000000000001",
        "path": "_systemfield_education_history",
        "title": "Education History",
        "type": "EducationHistory",
        "schoolName": "required",
        "degree": "optional",
        "major": "optional",
        "startDate": "optional",
        "endDate": "optional",
        "isRepeatable": True,
        "minRepeat": 1,
    }

    def _payload(self, field=None):
        payload = load_api("api_ashby_form")
        payload["data"]["jobPosting"]["applicationForm"]["sections"][0]["fieldEntries"].append(
            {"id": "e1", "isRequired": True, "field": field or dict(self.FIELD)}
        )
        return payload["data"]["jobPosting"]

    def _fields(self, field=None):
        return {f.id: f for f in fields_from_application_form(self._payload(field)).fields}

    def test_the_three_answerable_subfields_get_greenhouse_ids(self):
        by_id = self._fields()
        assert "school--0" in by_id
        assert "degree--0" in by_id
        assert "discipline--0" in by_id

    def test_requirement_comes_from_the_inline_declaration(self):
        by_id = self._fields()
        assert by_id["school--0"].required is True
        assert by_id["degree--0"].required is False

    def test_optional_dates_are_dropped_rather_than_emitted_unmapped(self):
        """`education.start_year` is a year and these want a date, so mapping
        them would write a wrong value. They cannot simply be emitted unmapped
        either: `_resolve_repeating` parks an unrecognized id even when it is
        optional, so that would park every board rendering them."""
        by_id = self._fields()
        assert "start-year--0" not in by_id
        assert not [i for i in by_id if i.endswith("startDate")]
        assert not [i for i in by_id if i.endswith("endDate")]

    def test_a_required_date_is_emitted_and_parks_loudly(self, answers, tailor_dir):
        field = dict(self.FIELD, startDate="required")
        plan = build_plan(
            fields_from_application_form(self._payload(field)), answers, tailor_dir, ats="ashby"
        )
        parked = [u.id for u in plan.unmapped]
        assert any(i.endswith("startDate") for i in parked)

    def test_the_observed_board_yields_exactly_the_three_mapped_subfields(self):
        """All five sub-fields declared, dates optional — the shape actually
        captured live. It must plan cleanly, not park."""
        ids = {i for i in self._fields() if "--0" in i}
        assert ids == {"school--0", "degree--0", "discipline--0"}

    def test_a_subfield_the_board_does_not_collect_is_absent(self):
        field = {k: v for k, v in self.FIELD.items() if k != "degree"}
        assert "degree--0" not in self._fields(field)

    def test_an_excluded_subfield_is_absent(self):
        """Quora's shape: `"degree": "excluded"` renders no degree control, and
        emitting one timed the fill out waiting for it."""
        field = dict(self.FIELD, degree="excluded", major="excluded")
        ids = {i for i in self._fields(field) if "--0" in i}
        assert ids == {"school--0"}

    def test_the_required_school_resolves_from_config(self, answers, tailor_dir):
        plan = build_plan(
            fields_from_application_form(self._payload()), answers, tailor_dir, ats="ashby"
        )
        by_id = {f.id: f.value for f in plan.fields}
        assert by_id["school--0"] == answers.education["school"]

    def test_a_block_declaring_no_subfields_raises(self):
        field = {
            "path": "_systemfield_education_history",
            "title": "Education",
            "type": "EducationHistory",
        }
        with pytest.raises(AshbyScanError, match="declaring no sub-fields"):
            fields_from_application_form(self._payload(field))

    def test_a_form_with_no_fields_raises(self):
        payload = load_api("api_ashby_form")
        payload["data"]["jobPosting"]["applicationForm"]["sections"] = []
        with pytest.raises(AshbyScanError, match="declares no fields"):
            fields_from_application_form(payload["data"]["jobPosting"])


class TestApiEndToEndResolution:
    def test_the_real_shaped_form_resolves_identity_and_defers_the_resume(
        self, answers, tailor_dir
    ):
        board_fields = fields_from_application_form(
            load_api("api_ashby_form")["data"]["jobPosting"]
        )
        plan = build_plan(board_fields, answers, tailor_dir, ats="ashby")
        assert [f.id for f in plan.files] == ["resume", "cover_letter"]
        by_id = {f.id: f.value for f in plan.fields}
        # The combined name field every Ashby board asks for, composed from
        # identity.first_name/last_name.
        assert by_id["_systemfield_name"] == "Alex Example"
        assert by_id["_systemfield_email"] == answers.identity["email"]

    def test_the_work_authorization_pair_parks_for_the_judge(self, answers, tailor_dir):
        board_fields = fields_from_application_form(
            load_api("api_ashby_form")["data"]["jobPosting"]
        )
        plan = build_plan(board_fields, answers, tailor_dir, ats="ashby")
        tiers = {u.id: u.tier for u in plan.unmapped}
        assert tiers["20000000-0000-0000-0000-000000000005"] == "B0"
        assert tiers["20000000-0000-0000-0000-000000000006"] == "B0"


def _form_with_files(*files):
    """A `jobPosting` node holding only upload fields: (path, title, required)."""
    return {
        "applicationForm": {
            "sections": [
                {
                    "fieldEntries": [
                        {
                            "id": f"entry-{i}",
                            "isRequired": required,
                            "field": {"type": "File", "path": path, "title": title},
                        }
                        for i, (path, title, required) in enumerate(files)
                    ]
                }
            ]
        }
    }
