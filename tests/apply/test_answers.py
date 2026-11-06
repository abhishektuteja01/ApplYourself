"""What goes into a field, and what refuses to.

The resolution cases run over the five reconciled DOM/API fixture pairs rather
than hand-built fields, because the questions that break this module are the
ones no one would think to invent: a sponsorship question next to an
authorization question, a work-authorization dropdown whose options are three
sentences, "How did you hear about this job?" with a different option list on
every board.
"""

from __future__ import annotations

from dataclasses import replace as dc_replace

import pytest
import yaml

from applyourself.apply import answers as A
from applyourself.apply import bank_schema as BS
from applyourself.apply.answers import Resolution
from applyourself.apply.bank_schema import (
    AnswersError,
    load_answers,
)
from applyourself.apply.reconcile import MergedField, MergedOption
from applyourself.shared import paths

from .conftest import FIXTURES
from .conftest import resolve_confirmed as resolve

CONFIG = FIXTURES / "application_answers.yaml"


def field(**kw) -> MergedField:
    """A merged field with everything but the interesting bits defaulted."""
    base = dict(
        id="question_1",
        name="question_1",
        label="",
        required=False,
        kind="text",
        section="questions",
        multi=False,
        options=(),
    )
    options = kw.pop("options", None)
    if options is not None:
        kw["options"] = tuple(
            o if isinstance(o, MergedOption) else MergedOption(label=o) for o in options
        )
    base.update(kw)
    return MergedField(**base)


def by_label(reconciled, needle: str) -> MergedField:
    hits = [f for f in reconciled.fields if needle.casefold() in f.label.casefold()]
    assert len(hits) == 1, f"{needle!r} matched {[f.label for f in hits]}"
    return hits[0]


def write_config(tmp_path, **overrides):
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    for key, value in overrides.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    path = tmp_path / "application_answers.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


class TestFindingAQuestion:
    """An exact wording beats every keyword; a keyword is a guess, named as one;
    a `not:` wording is never guessed again."""

    def _answers(self, tmp_path, qs):
        return load_answers(write_config(tmp_path, questions=qs))

    def test_a_wording_matches_the_whole_label(self, tmp_path):
        a = self._answers(tmp_path, {"q0": {"answer": ["California"], "wordings": ["state"]}})
        assert resolve(field(label="State", options=["California"]), a).value == "California"

    def test_a_wording_does_not_match_a_label_containing_it(self, tmp_path):
        a = self._answers(tmp_path, {"q0": {"answer": ["California"], "wordings": ["state"]}})
        for label in ("United States", "Please state their name", "Home state address"):
            assert resolve(field(label=label), a).action == "skip"

    def test_punctuation_and_case_do_not_break_a_wording(self, tmp_path):
        a = self._answers(
            tmp_path,
            {
                "q0": {"answer": "Teaching Assistant", "wordings": ["current position title"]},
            },
        )
        assert resolve(field(label="Current Position/Title"), a).value == "Teaching Assistant"

    def test_a_question_may_carry_both(self, tmp_path):
        a = self._answers(
            tmp_path,
            {
                "q0": {
                    "answer": "Open",
                    "keywords": ["pay expectation"],
                    "wordings": ["expected pay"],
                },
            },
        )
        assert resolve(field(label="Expected pay"), a).value == "Open"
        assert resolve(field(label="What is your pay expectation?"), a).value == "Open"

    def test_an_exact_wording_beats_an_earlier_keyword(self, tmp_path):
        a = self._answers(
            tmp_path,
            {
                "city": {"answer": "San Francisco", "keywords": ["currently located"]},
                "ny": {
                    "answer": ["No"],
                    "wordings": ["are you currently located in the new york area"],
                },
            },
        )
        r = resolve(
            field(label="Are you currently located in the New York area?", options=["Yes", "No"]), a
        )
        assert r.value == "No" and r.source == "bank:ny"

    def test_a_not_wording_is_never_guessed(self, tmp_path):
        a = load_answers(
            write_config(
                tmp_path,
                questions={
                    "referral": {
                        "answer": ["No"],
                        "keywords": ["employee referral"],
                        "not": ["If employee referral, referred by"],
                    }
                },
            )
        )
        assert resolve(field(label="If employee referral, referred by"), a).action == "skip"
        assert resolve(field(label="Employee referral?"), a).value == "No"

    def test_a_question_needs_a_way_to_be_found(self, tmp_path):
        with pytest.raises(AnswersError, match="wordings or keywords"):
            self._answers(tmp_path, {"q0": {"answer": "x"}})

    def test_two_questions_claiming_one_wording_is_an_error(self, tmp_path):
        with pytest.raises(AnswersError, match="both claim the wording"):
            self._answers(
                tmp_path,
                {
                    "q0": {"answer": "California", "wordings": ["state"]},
                    "q1": {"answer": "Massachusetts", "wordings": ["state"]},
                },
            )

    def test_a_work_authorization_wording_is_rejected(self, tmp_path):
        with pytest.raises(AnswersError, match="work-authorization question"):
            self._answers(tmp_path, {"q0": {"answer": "x", "wordings": ["citizenship"]}})


class TestLoad:
    def test_the_synthetic_fixture_loads(self, answers):
        assert answers.identity["email"] == "alex@example.com"
        assert answers.education["degree"] == "Master's Degree"
        assert answers.employment["company_name"] == "Widget Corp"
        assert answers.authorized_now and answers.requires_sponsorship
        assert answers.questions

    def test_missing_file_points_at_the_template(self, tmp_path):
        with pytest.raises(AnswersError, match=r"application_answers.example.yaml"):
            load_answers(tmp_path / "nope.yaml")

    def test_schema_version_must_match(self, tmp_path):
        with pytest.raises(AnswersError, match="schema_version"):
            load_answers(write_config(tmp_path, schema_version=1))

    @pytest.mark.parametrize("key", BS.IDENTITY_KEYS)
    def test_every_identity_key_is_required(self, tmp_path, answers, key, monkeypatch):
        monkeypatch.setitem(paths._overrides, "RESUME", tmp_path / "no_resume.yaml")
        identity = dict(answers.identity)
        del identity[key]
        with pytest.raises(AnswersError, match=f"identity.{key}"):
            load_answers(write_config(tmp_path, identity=identity))

    def test_a_key_the_bank_leaves_out_comes_from_the_resume(self, tmp_path, answers):
        """The example resume: `Alex Example`, a mailto: link, Widget Corp Jan 2023 - Jun 2025."""
        identity = {k: v for k, v in answers.identity.items() if k not in ("first_name", "email")}
        got = load_answers(
            write_config(tmp_path, identity=identity, employment={"title": "Cog Fitter"})
        )
        assert (got.identity["first_name"], got.identity["email"]) == ("Alex", "alex@example.com")
        assert got.employment["company_name"] == "Widget Corp"
        assert got.employment["title"] == "Cog Fitter"
        assert got.employment["end_month"] == "June" and got.employment["current_role"] is False

    def test_a_schema_2_bank_names_the_migration(self, tmp_path):
        with pytest.raises(AnswersError, match="config migrate"):
            load_answers(write_config(tmp_path, schema_version=2))

    def test_an_empty_identity_value_is_not_a_value(self, tmp_path, answers):
        identity = dict(answers.identity, phone="   ")
        with pytest.raises(AnswersError, match=r"identity.phone"):
            load_answers(write_config(tmp_path, identity=identity))

    def test_a_typo_in_a_block_key_is_not_silently_ignored(self, tmp_path, answers):
        identity = dict(answers.identity, emial="alex@example.com")
        with pytest.raises(AnswersError, match="unknown keys"):
            load_answers(write_config(tmp_path, identity=identity))

    def test_education_months_are_optional(self, tmp_path, answers, monkeypatch):
        monkeypatch.setitem(paths._overrides, "RESUME", tmp_path / "no_resume.yaml")
        education = {
            k: v for k, v in answers.education.items() if k not in ("start_month", "end_month")
        }
        loaded = load_answers(write_config(tmp_path, education=education))
        assert "start_month" not in loaded.education

    def test_the_employment_block_may_be_absent(self, tmp_path):
        assert load_answers(write_config(tmp_path, employment=None)).employment is None


class TestWorkAuthorizationLoading:
    def wa(self, **countries):
        return {
            "us_person": "park",
            "nationalities": [],
            "countries": {
                "default": {"authorized": False, "requires_sponsorship": True},
                **countries,
            },
        }

    def test_a_default_row_is_required(self, tmp_path):
        with pytest.raises(AnswersError, match="default"):
            load_answers(
                write_config(
                    tmp_path,
                    work_authorization={
                        "countries": {
                            "United States": {"authorized": True, "requires_sponsorship": False}
                        }
                    },
                )
            )

    def test_country_names_are_canonical(self, tmp_path):
        a = load_answers(
            write_config(
                tmp_path,
                work_authorization=self.wa(
                    USA={"authorized": True, "requires_sponsorship": False, "unrestricted": True}
                ),
            )
        )
        assert a.facts_for("United States").unrestricted

    def test_unrestricted_cannot_contradict_the_row(self, tmp_path):
        with pytest.raises(AnswersError, match="contradicts"):
            load_answers(
                write_config(
                    tmp_path,
                    work_authorization=self.wa(
                        **{
                            "United States": {
                                "authorized": True,
                                "requires_sponsorship": True,
                                "unrestricted": True,
                            }
                        }
                    ),
                )
            )

    def test_a_bare_yes_no_for_us_person_is_refused(self, tmp_path):
        wa = self.wa()
        wa["us_person"] = False
        with pytest.raises(AnswersError, match="quote it"):
            load_answers(write_config(tmp_path, work_authorization=wa))

    def test_a_bare_yes_is_too_generic_to_be_a_claim(self, tmp_path):
        with pytest.raises(AnswersError, match="too generic"):
            load_answers(write_config(tmp_path, claims={"Yes": {"authorized": True}}))

    def _floor(self, tmp_path, floor):
        return load_answers(
            write_config(
                tmp_path,
                work_authorization=self.wa(
                    Germany={
                        "authorized": False,
                        "requires_sponsorship": True,
                        "visa_salary_floor": floor,
                    }
                ),
            )
        )

    def test_a_visa_salary_floor_is_carried_on_the_row(self, tmp_path):
        a = self._floor(
            tmp_path, {"amount": 48300, "currency": "EUR", "per": "year", "note": " EU Blue Card "}
        )
        assert a.facts_for("Germany").visa_salary_floor == BS.SalaryFloor(
            amount=48300, currency="EUR", per="year", note="EU Blue Card"
        )
        assert a.facts_for("France").visa_salary_floor is None

    @pytest.mark.parametrize(
        "floor, match",
        [
            (48300, "amount, currency, per"),
            ({"amount": 48300, "currency": "EUR"}, "per"),
            ({"amount": 0, "currency": "EUR", "per": "year"}, "amount"),
            ({"amount": True, "currency": "EUR", "per": "year"}, "amount"),
            ({"amount": "48k", "currency": "EUR", "per": "year"}, "amount"),
            ({"amount": 48300, "currency": "euro", "per": "year"}, "currency"),
            ({"amount": 48300, "currency": "EUR", "per": "week"}, "per"),
            ({"amount": 48300, "currency": "EUR", "per": "year", "note": 5}, "note"),
            ({"amount": 48300, "currency": "EUR", "per": "year", "gross": True}, "unknown keys"),
        ],
    )
    def test_a_malformed_visa_salary_floor_is_refused(self, tmp_path, floor, match):
        with pytest.raises(AnswersError, match=f"visa_salary_floor.*{match}"):
            self._floor(tmp_path, floor)


class TestQuestionValidation:
    def test_a_question_needs_an_answer(self, tmp_path):
        with pytest.raises(AnswersError, match="answer"):
            load_answers(write_config(tmp_path, questions={"q": {"keywords": ["portfolio"]}}))

    def test_answer_may_be_a_list_of_candidates(self, tmp_path):
        qs = {
            "q0": {"answer": ["Careers Page", "Other"], "keywords": ["how did you hear"]},
        }
        loaded = load_answers(write_config(tmp_path, questions=qs))
        assert loaded.questions[0].answers == ("Careers Page", "Other")

    def test_a_fact_pointer_answers_from_the_fact(self, tmp_path):
        loaded = load_answers(
            write_config(
                tmp_path,
                questions={
                    "legal_first_name": {
                        "answer": {"fact": "identity.first_name"},
                        "keywords": ["legal first name"],
                    }
                },
            )
        )
        assert resolve(field(label="Legal first name"), loaded).value == "Alex"

    def test_a_fact_that_is_not_set_is_refused(self, tmp_path):
        with pytest.raises(AnswersError, match="not set"):
            load_answers(
                write_config(
                    tmp_path,
                    questions={
                        "x": {"answer": {"fact": "identity.nickname"}, "keywords": ["nickname"]}
                    },
                )
            )

    def test_overlapping_keywords_across_questions_raise(self, tmp_path):
        qs = {
            "q0": {"answer": "Open", "keywords": ["salary"]},
            "q1": {"answer": "150000", "keywords": ["desired salary"]},
        }
        with pytest.raises(AnswersError, match="overlaps"):
            load_answers(write_config(tmp_path, questions=qs))

    def test_a_short_keyword_is_refused(self, tmp_path):
        with pytest.raises(AnswersError, match="under"):
            load_answers(
                write_config(tmp_path, questions={"q0": {"answer": "x", "keywords": ["C++"]}})
            )

    @pytest.mark.parametrize(
        "keyword",
        [
            "sponsor",
            "visa",
            "work authorization",
            "authorized to work",
            "citizenship",
            "u.s. person",
        ],
    )
    def test_a_work_authorization_keyword_cannot_be_a_question(self, tmp_path, keyword):
        qs = {"q0": {"answer": "Yes", "keywords": [keyword]}}
        with pytest.raises(AnswersError, match="work-authorization question"):
            load_answers(write_config(tmp_path, questions=qs))


class TestResolutionProvenance:
    """A resolution names what answered it: an exact wording, or a keyword guess
    the session must confirm."""

    def test_a_keyword_guess_names_its_keyword(self, tmp_path):
        qs = {
            "site": {"answer": "https://x.example", "keywords": ["portfolio", "personal site"]},
        }
        answers = load_answers(write_config(tmp_path, questions=qs))
        resolution = resolve(field(label="Link to your personal site"), answers)
        assert resolution.tier == "B"
        assert resolution.source == "bank:site~personal site"
        assert resolution.by_keyword and resolution.question_id == "site"

    def test_an_exact_wording_names_its_question(self, tmp_path):
        qs = {"state": {"answer": ["Example State"], "wordings": ["state"]}}
        answers = load_answers(write_config(tmp_path, questions=qs))
        resolution = resolve(field(label="State", options=["Example State"]), answers)
        assert resolution.source == "bank:state"
        assert not resolution.by_keyword

    def test_a_parked_choice_still_names_its_question(self, tmp_path):
        qs = {
            "heard": {"answer": ["Careers Page"], "keywords": ["how did you hear"]},
        }
        answers = load_answers(write_config(tmp_path, questions=qs))
        resolution = resolve(
            field(
                label="How did you hear about this job?",
                required=True,
                kind="select",
                options=["Acme Careers Site", "LinkedIn"],
            ),
            answers,
        )
        assert resolution.action == "park"
        assert resolution.source == "bank:heard~how did you hear"

    def test_a_structural_tier_leaves_the_source_empty(self, answers, merged):
        reconciled = merged("form_minimal")
        email = next(f for f in reconciled.fields if f.id == "email")
        resolution = resolve(email, answers)
        assert resolution.tier == "A"
        assert resolution.source == ""


class TestMatchMode:
    """`mode: contains` — for short affirmations a board wraps in a full
    sentence ("Yes" inside "Yes, I am able and willing to work..."). A learned
    option spelling never changes the mode."""

    def test_mode_defaults_to_exact(self, tmp_path):
        qs = {"q0": {"answer": "https://x.example", "keywords": ["portfolio"]}}
        loaded = load_answers(write_config(tmp_path, questions=qs))
        assert loaded.questions[0].mode == "exact"

    def test_an_invalid_mode_is_rejected(self, tmp_path):
        qs = {"q0": {"answer": "x", "keywords": ["portfolio"], "mode": "fuzzy"}}
        with pytest.raises(AnswersError, match="mode"):
            load_answers(write_config(tmp_path, questions=qs))

    def test_a_contains_question_matches_inside_a_full_sentence_option(self, tmp_path):
        qs = {
            "q0": {
                "answer": ["I acknowledge", "Yes"],
                "keywords": ["acknowledge"],
                "mode": "contains",
            },
        }
        answers = load_answers(write_config(tmp_path, questions=qs))
        f = field(
            label="I acknowledge that this company has an in-office culture.",
            required=True,
            kind="select",
            options=[
                "Yes, I am able and willing to work in the office location listed.",
                "No, I am unable and/or unwilling to work in the office location listed.",
            ],
        )
        assert resolve(f, answers).value == (
            "Yes, I am able and willing to work in the office location listed."
        )

    def test_a_contains_question_still_parks_when_no_option_holds_any_candidate(self, tmp_path):
        qs = {
            "q0": {
                "answer": ["I acknowledge", "Yes"],
                "keywords": ["acknowledge"],
                "mode": "contains",
            },
        }
        answers = load_answers(write_config(tmp_path, questions=qs))
        f = field(
            label="I acknowledge the code of conduct.",
            required=True,
            kind="select",
            options=["Agreed", "Declined"],
        )
        assert resolve(f, answers).parked

    def test_exact_mode_does_not_match_inside_a_full_sentence(self, tmp_path):
        qs = {
            "q0": {"answer": ["I acknowledge", "Yes"], "keywords": ["acknowledge"]},
        }
        answers = load_answers(write_config(tmp_path, questions=qs))
        f = field(
            label="I acknowledge that this company has an in-office culture.",
            required=True,
            kind="select",
            options=[
                "Yes, I am able and willing to work in the office location listed.",
                "No, I am unable and/or unwilling to work in the office location listed.",
            ],
        )
        assert resolve(f, answers).parked


class TestIdentity:
    def test_the_plain_fields_come_from_the_identity_block(self, merged, answers):
        r = merged("form_minimal")
        assert resolve(r.by_id("first_name"), answers).value == "Alex"
        assert resolve(r.by_id("email"), answers).value == "alex@example.com"
        assert resolve(r.by_id("phone"), answers).value == "+1 555 0100"

    def test_country_and_location_resolve_though_no_api_declares_them(self, merged, answers):
        """`country` renders on every board and appears in no question array,
        so it has no option list to validate against."""
        r = merged("form_minimal")
        country = r.by_id("country")
        assert country.dom_only and country.required and not country.options
        assert resolve(country, answers).value == "United States"
        assert resolve(r.by_id("candidate-location"), answers).value.startswith("Exampletown")

    def test_a_country_only_location_field_takes_the_configured_country(self, answers):
        """Measured live: Ashby's `_systemfield_location` is city-level on some
        boards and country-only on others, with nothing in either payload
        saying which. On the coarse ones "Boston" returns no options at all.
        Falling back to `identity.country` answers the question the board is
        actually asking — the same ordered-candidate rule a Tier B answer uses."""
        field = MergedField(
            id="_systemfield_location",
            name="_systemfield_location",
            label="Preferred location",
            required=True,
            kind="react_select",
            section="basic",
            multi=False,
            options=(MergedOption(label="United States"), MergedOption(label="Palestine")),
        )
        assert resolve(field, answers).value == "United States"

    def test_the_city_still_wins_where_the_board_offers_it(self, answers):
        """The fallback is second, not a replacement."""
        field = MergedField(
            id="_systemfield_location",
            name="_systemfield_location",
            label="Preferred location",
            required=True,
            kind="react_select",
            section="basic",
            multi=False,
            options=(
                MergedOption(label="United States"),
                MergedOption(label=answers.identity["location"]),
            ),
        )
        assert resolve(field, answers).value == answers.identity["location"]

    def test_a_board_offering_neither_still_parks(self, answers):
        field = MergedField(
            id="_systemfield_location",
            name="_systemfield_location",
            label="Preferred location",
            required=True,
            kind="react_select",
            section="basic",
            multi=False,
            options=(MergedOption(label="Ireland"),),
        )
        assert resolve(field, answers).parked is True

    def test_the_fallback_is_keyed_on_the_dom_id_not_the_label(self, answers):
        """`identity.country` also feeds Greenhouse's phone dial-code widget,
        which is not a country question."""
        from applyourself.apply.answers import _identity_candidates

        assert len(_identity_candidates("_systemfield_location", "x", answers)) == 2
        assert _identity_candidates("email", "x", answers) == ("x",)

    def test_greenhouse_and_lever_location_fields_are_left_alone(self, answers):
        """They map to the same config key but carry no option list at plan
        time, so widening the fallback to the key would only change behaviour
        at fill time, on the highest-volume lane, on no evidence."""
        from applyourself.apply.answers import _identity_candidates

        for field_id in ("candidate-location", "location"):
            assert _identity_candidates(field_id, "x", answers) == ("x",)

    def test_the_two_file_inputs_are_deferred_to_the_planner(self, merged, answers):
        r = merged("form_minimal")
        for field_id in ("resume", "cover_letter"):
            assert resolve(r.by_id(field_id), answers) == Resolution(
                "defer", tier="A", reason=field_id
            )

    def test_preferred_name_fills_where_the_board_renders_it(self, merged, answers):
        assert resolve(merged("form_education").by_id("preferred_name"), answers).value == "Alex"


class TestDateFields:
    """A date field is the start-date question only when its label says so;
    "Date of birth" or "visa expiry" must never get the start date."""

    def _pin(self, monkeypatch):
        from datetime import date as _date

        monkeypatch.setattr(A, "_today", lambda: _date(2026, 9, 27))

    def test_a_start_date_field_gets_the_country_start_date(self, answers, monkeypatch):
        self._pin(monkeypatch)
        f = field(id="question_1", label="When can you start a new role?", kind="date")
        r = resolve(f, answers)
        assert (r.action, r.value) == ("fill", "10/11/2026")  # fixture: 14 days

    def test_another_date_question_is_not_the_start_date(self, answers):
        for label in ("Date of birth", "When does your OPT/visa expire?"):
            r = resolve(field(id="q", label=label, kind="date", required=True), answers)
            assert r.action == "park", label

    def test_a_country_with_no_start_in_days_parks(self, answers):
        a = dc_replace(answers, job_country="Germany")
        r = resolve(field(label="When can you start?", required=True), a)
        assert r.action == "park" and "Germany" in r.reason


class TestEducationAndEmployment:
    def test_entry_zero_fills_from_config(self, merged, answers):
        r = merged("form_education")
        assert resolve(r.by_id("school--0"), answers).value == "Example University"
        assert resolve(r.by_id("degree--0"), answers).value == "Master's Degree"
        assert resolve(r.by_id("start-year--0"), answers).value == "2019"
        assert resolve(r.by_id("start-month--0"), answers).value == "September"

    def test_a_required_education_field_with_no_option_list_still_fills(self, merged, answers):
        """school--0 and degree--0 are required on this board and offer no
        options at all — the value goes in and fill.py asserts it stuck."""
        r = merged("form_demographic")
        school = r.by_id("school--0")
        assert school.required and not school.options
        assert resolve(school, answers).action == "fill"

    def test_an_unset_optional_education_field_is_skipped(
        self, tmp_path, merged, answers, monkeypatch
    ):
        monkeypatch.setitem(paths._overrides, "RESUME", tmp_path / "no_resume.yaml")
        education = {k: v for k, v in answers.education.items() if k != "start_month"}
        thin = load_answers(write_config(tmp_path, education=education))
        assert resolve(merged("form_education").by_id("start-month--0"), thin).action == "skip"

    def test_a_school_alternate_is_offered_when_the_board_words_it_differently(
        self, tmp_path, answers
    ):
        """Ashby stacks name/country/domain into one option label."""
        listed = "Example University\nUnited States\nexample.edu"
        education = dict(answers.education, school_alternates=[listed])
        loaded = load_answers(write_config(tmp_path, education=education))
        f = field(
            id="school--0",
            section="education",
            kind="react_select",
            required=True,
            options=[listed, "Example University\nChina\nex.cn"],
        )
        assert resolve(f, loaded).value == listed

    def test_the_configured_school_still_wins_when_the_board_offers_it(self, tmp_path, answers):
        education = dict(
            answers.education, school_alternates=["Example University\nUnited States\nexample.edu"]
        )
        loaded = load_answers(write_config(tmp_path, education=education))
        f = field(
            id="school--0",
            section="education",
            kind="react_select",
            required=True,
            options=["Example University"],
        )
        assert resolve(f, loaded).value == "Example University"

    def test_a_board_offering_no_configured_spelling_parks(self, tmp_path, answers):
        """Never a guess between two same-named schools in two countries."""
        education = dict(
            answers.education, school_alternates=["Example University\nUnited States\nexample.edu"]
        )
        loaded = load_answers(write_config(tmp_path, education=education))
        f = field(
            id="school--0",
            section="education",
            kind="react_select",
            required=True,
            options=["Example University\nChina\nex.cn"],
        )
        assert resolve(f, loaded).action == "park"

    def test_school_alternates_stay_optional(self, merged, answers):
        """The key is absent from the fixture config and nothing breaks."""
        assert "school_alternates" not in answers.education
        assert resolve(merged("form_education").by_id("school--0"), answers).value == (
            "Example University"
        )

    def test_an_empty_school_alternates_list_is_refused(self, tmp_path, answers):
        education = dict(answers.education, school_alternates=[])
        with pytest.raises(AnswersError):
            load_answers(write_config(tmp_path, education=education))

    def test_the_employment_block_fills_including_the_checkbox(self, merged, answers):
        r = merged("form_employment")
        assert resolve(r.by_id("company-name-0"), answers).value == "Widget Corp"
        assert resolve(r.by_id("title-0"), answers).value == "Widget Operations Analyst"
        assert resolve(r.by_id("end-date-year-0"), answers).value == "2025"
        assert resolve(r.by_id("current-role-0_1"), answers).value is False

    def test_a_board_asking_for_employment_parks_when_the_block_is_absent(self, tmp_path, merged):
        """Rare — 1 board in 60 — but six of its seven fields are required."""
        without = load_answers(write_config(tmp_path, employment=None))
        parked = [
            f.id
            for f in merged("form_employment").by_section("employment")
            if resolve(f, without).parked
        ]
        assert parked == [
            "company-name-0",
            "title-0",
            "start-date-month-0",
            "start-date-year-0",
            "end-date-month-0",
            "end-date-year-0",
        ]


class TestEeoc:
    @pytest.mark.parametrize(
        "field_id,expected",
        [
            ("gender", "Decline To Self Identify"),
            ("veteran_status", "I don't wish to answer"),
            ("disability_status", "I do not want to answer"),
        ],
    )
    def test_each_eeoc_question_takes_its_own_opt_out_string(
        self, merged, answers, field_id, expected
    ):
        assert resolve(merged("form_minimal").by_id(field_id), answers).value == expected

    def test_hispanic_ethnicity_has_nothing_to_opt_out_against_statically(self, merged, answers):
        """DOM-only, so neither the API nor the served HTML carries its options
        and nothing here can match an opt-out string. Optional, so it is skipped;
        required, it parks. fill.py re-resolves it from the opened widget, which
        does offer Decline To Self Identify."""
        f = merged("form_minimal").by_id("hispanic_ethnicity")
        assert f.dom_only and not f.options and not f.required
        assert resolve(f, answers).action == "skip"

    def test_hispanic_ethnicity_opts_out_once_its_options_are_known(self, answers):
        """The live widget offers exactly these three."""
        resolution = resolve(
            field(
                id="hispanic_ethnicity",
                section="eeoc",
                kind="react_select",
                required=True,
                options=["Yes", "No", "Decline To Self Identify"],
            ),
            answers,
        )
        assert resolution.action == "fill"
        assert resolution.value == "Decline To Self Identify"

    def test_a_required_hispanic_ethnicity_with_no_decline_option_still_parks(self, answers):
        """Answering it substantively is a claim about the user, not a
        content-free opt-out, so it is not guessed."""
        assert (
            resolve(
                field(
                    id="hispanic_ethnicity",
                    section="eeoc",
                    kind="react_select",
                    required=True,
                    options=["Yes", "No"],
                ),
                answers,
            ).action
            == "park"
        )

    def test_the_eeoc_block_is_resolved_by_section_not_by_keyword(self, answers):
        """An employer-authored question that reads like EEOC stays in
        `questions` and goes to the bank — form_multiselect asks 'I identify my
        race as' as its own question, and nothing here answers it."""
        assert resolve(
            field(
                label="Disability Status", required=True, kind="react_select", options=["Yes", "No"]
            ),
            answers,
        ).parked

    def test_an_eeoc_question_with_no_opt_out_option_parks_when_required(self, answers):
        f = field(
            id="gender",
            section="eeoc",
            required=True,
            kind="react_select",
            options=["Female", "Male"],
        )
        assert resolve(f, answers).parked


class TestDemographic:
    def test_the_flagged_decline_option_wins(self, answers):
        f = field(
            id="4005807007",
            section="demographic",
            kind="react_select",
            multi=True,
            options=[
                MergedOption(label="Woman"),
                MergedOption(label="Opt out", decline_to_answer=True),
            ],
        )
        assert resolve(f, answers).value == ("Opt out",)

    def test_the_label_is_ignored_and_the_opt_out_string_is_matched(self, merged, answers):
        """Employer-authored labels vary — 13 distinct over 4 boards — so
        resolution is by option, never by question."""
        block = merged("form_demographic").by_section("demographic")
        assert block
        for f in block:
            assert (
                resolve(f, answers).value == ("I don't wish to answer",)
                if f.multi
                else resolve(f, answers).value == "I don't wish to answer"
            )

    def test_a_required_question_with_no_way_out_parks(self, answers):
        f = field(
            id="123",
            section="demographic",
            required=True,
            kind="react_select",
            options=["Yes", "No"],
        )
        assert resolve(f, answers).parked

    def test_an_optional_question_with_no_way_out_is_skipped(self, answers):
        f = field(id="123", section="demographic", kind="react_select", options=["Yes", "No"])
        assert resolve(f, answers).action == "skip"


class TestWorkAuthorization:
    SPONSORSHIP = [
        ("form_minimal", "require visa sponsorship"),
        ("form_multiselect", "Will you now or in the future require sponsorship?"),
        ("form_demographic", "require sponsorship for an employment"),
    ]
    AUTHORIZED = [
        ("form_demographic", "legally authorized to work in the United States"),
    ]

    @pytest.mark.parametrize("fixture,needle", SPONSORSHIP)
    def test_time_limited_will_require_sponsorship(self, merged, answers, fixture, needle):
        assert resolve(by_label(merged(fixture), needle), answers).value == "Yes"

    @pytest.mark.parametrize("fixture,needle", AUTHORIZED)
    def test_time_limited_is_authorized_today(self, merged, answers, fixture, needle):
        assert resolve(by_label(merged(fixture), needle), answers).value == "Yes"

    @pytest.mark.parametrize("fixture,needle", SPONSORSHIP + AUTHORIZED)
    def test_a_citizen_answers_the_two_families_differently(
        self, tmp_path, merged, answers, fixture, needle
    ):
        """The case one keyword cannot express: authorized yes,
        sponsorship no, on boards that ask both."""
        citizen = dc_replace(
            answers,
            countries={
                **answers.countries,
                "United States": BS.CountryFacts(
                    authorized=True, requires_sponsorship=False, unrestricted=True
                ),
            },
        )
        expected = "Yes" if (fixture, needle) in self.AUTHORIZED else "No"
        assert resolve(by_label(merged(fixture), needle), citizen).value == expected

    def test_an_authorization_question_naming_another_country_uses_its_row(self, merged, answers):
        """'Are you legally authourized to work in South Africa?' — no row for
        South Africa, so the default row answers: not authorized."""
        f = by_label(merged("form_multiselect"), "authourized to work in South Africa")
        assert resolve(f, answers).value == "No"

    def test_a_dropdown_of_prose_options_parks(self, answers):
        """A real board's 'Work Authorization' select offers three sentences,
        not Yes/No. Picking one is judgment."""
        f = field(
            label="Work Authorization",
            required=True,
            kind="react_select",
            options=[
                "I am authorized to work without sponsorship or restrictions for any employer in the U.S.",
                "I am ONLY allowed to work for my current employer in the U.S. and I will require "
                "sponsorship now or in the future to work in the US",
                "My status to work in the U.S. is unknown",
            ],
        )
        assert resolve(f, answers).parked

    def test_a_citizenship_dropdown_parks(self, merged, answers):
        f = by_label(merged("form_multiselect"), "Citizenship")
        assert resolve(f, answers).parked

    @pytest.mark.parametrize("needle", ["What visa type do you hold", "When does your visa expire"])
    def test_optional_visa_free_text_is_left_blank_not_parked(self, merged, answers, needle):
        f = by_label(merged("form_multiselect"), needle)
        assert not f.required
        assert resolve(f, answers).action == "skip"

    def test_a_negated_sponsorship_question_is_never_guessed(self, answers):
        f = field(
            label="Are you able to work without sponsorship, now or in the future?",
            required=True,
            kind="react_select",
            options=["Yes", "No"],
        )
        assert resolve(f, answers).parked

    def test_a_work_authorization_question_rendered_as_free_text_is_written_out(self, answers):
        """A yes/no question in a text box is still a yes/no question, and
        writing the word is the same answer as picking the option. This used to
        park; 11 groups in the 237-board harvest are exactly this shape, every
        one of them an ordinary sponsorship or authorization question."""
        f = field(
            label="Will you now or in the future require sponsorship?", required=True, kind="text"
        )
        r = resolve(f, answers)
        assert r.action == "fill"
        assert r.value == "Yes"  # time_limited requires it


class TestBankQuestions:
    def test_a_keyword_matches_the_normalized_label(self, merged, answers):
        r = merged("form_minimal")
        assert resolve(by_label(r, "LinkedIn"), answers).value == "https://linkedin.com/in/example"
        assert resolve(by_label(r, "Website"), answers).value == "https://example.com"

    def test_a_dropdown_answer_falls_through_to_an_option_the_board_offers(self, merged, answers):
        """Eight sampled boards, eight disjoint option sets for the same
        question. One string would miss almost all of them."""
        assert (
            resolve(by_label(merged("form_minimal"), "How did you hear"), answers).value == "Other"
        )
        assert (
            resolve(by_label(merged("form_multiselect"), "How did you hear"), answers).value
            == "Careers Page"
        )
        assert (
            resolve(by_label(merged("form_employment"), "How did you hear"), answers).value
            == "Other"
        )

    def test_the_same_question_fills_free_text_with_its_first_candidate(self, merged, answers):
        f = by_label(merged("form_demographic"), "How did you hear")
        assert f.kind == "text" and not f.options
        assert resolve(f, answers).value == "Careers Page"

    def test_an_answer_the_board_does_not_offer_parks_rather_than_guessing(self, answers):
        f = field(
            label="How did you hear about this job?",
            required=True,
            kind="react_select",
            options=["Recruiter", "Conference"],
        )
        parked = resolve(f, answers)
        assert parked.parked and "Careers Page" in parked.reason

    def test_a_multi_answer_comes_back_as_a_list(self, answers):
        f = field(
            label="What pronouns do you prefer to go by?",
            kind="react_select",
            multi=True,
            options=["She/Her", "He/Him", "They/Them"],
        )
        assert resolve(f, answers).value == ("They/Them",)

    def test_a_question_never_fills_a_file_input(self, answers):
        f = field(id="portfolio_upload", label="Portfolio", required=True, kind="file")
        assert resolve(f, answers).parked

    def test_question_order_is_the_precedence(self, tmp_path, merged):
        qs = {
            "q0": {"answer": "Two weeks.", "keywords": ["notice period"]},
            "q1": {"answer": "Fallback.", "keywords": ["what is your"]},
        }
        loaded = load_answers(write_config(tmp_path, questions=qs))
        f = by_label(merged("form_multiselect"), "What is your notice period?")
        assert resolve(f, loaded).value == "Two weeks."


class TestTierC:
    def test_an_unmatched_required_question_parks(self, merged, answers):
        f = by_label(merged("form_multiselect"), "why you're interested to work for")
        assert resolve(f, answers).parked

    def test_an_unmatched_optional_question_is_skipped(self, merged, answers):
        f = by_label(merged("form_employment"), "If yes, please explain.")
        assert resolve(f, answers).action == "skip"

    def test_a_board_of_employer_authored_diversity_questions_parks(self, merged, answers):
        """These are in `questions`, not the EEOC block, so Tier A2 does not
        reach them and nothing deterministic should."""
        r = merged("form_multiselect")
        for needle in ("LGBTQ+ Community", "I identify my race as", "Disability Status"):
            assert resolve(by_label(r, needle), answers).parked

    def test_every_field_of_every_fixture_resolves_to_a_known_action(self, merged, answers):
        from .conftest import FORM_FIXTURES

        for name in FORM_FIXTURES:
            for f in merged(name).fields:
                r = resolve(f, answers)
                assert r.action in {"fill", "skip", "park", "defer"}
                assert r.action != "fill" or r.value is not None
                # Nothing optional-and-unanswerable may block a submission, and
                # nothing required may be left silently empty.
                assert not (r.action == "skip" and f.required)


class TestTheShippedTemplate:
    def test_it_loads_through_the_real_loader(self):
        loaded = load_answers(paths.example(BS.EXAMPLE_NAME))
        assert loaded.identity["first_name"]
        assert "default" in loaded.countries

    def test_its_questions_answer_the_boards_in_the_fixtures(self, merged):
        """A template whose questions match nothing teaches the wrong shape."""
        loaded = load_answers(paths.example(BS.EXAMPLE_NAME))
        r = merged("form_minimal")
        assert resolve(by_label(r, "How did you hear"), loaded).action == "fill"
        assert resolve(by_label(r, "LinkedIn"), loaded).action == "fill"


def _yes_no_field(label: str, *, required: bool = True):
    """A required Yes/No select — the shape every work-authorization question
    in the fixtures actually has."""
    from applyourself.apply.domscan import DomOption

    return MergedField(
        id="q",
        name="q",
        label=label,
        required=required,
        kind="select",
        section="questions",
        multi=False,
        api_type="multi_value_single_select",
        options=(DomOption(value="1", label="Yes"), DomOption(value="0", label="No")),
    )


class TestTheQualifiedFamily:
    """A scope qualifier (permanence, freedom from sponsorship) is answered from
    the country's `unrestricted` fact — No for an OPT holder, Yes for a citizen.
    The plain question is answered from `authorized` alone."""

    QUALIFIED = [
        "Are you permanently authorized to work for any employer in the United States?",
        "I am authorized to work without sponsorship or restrictions for any employer in the U.S.",
        "Are you authorized to work in the United States on a permanent basis?",
        "Are you legally authorized to work in the U.S., now or in the future, "
        "without sponsorship?",
        "Are you legally authorized to work in the United States without sponsorship?",
        "Do you have unrestricted authorization to work in the United States?",
    ]
    PLAIN = [
        "Are you legally authorized to work in the United States today?",
        "Are you legally authorized to work in the U.S.?",
        "Are you legally authorized to work in the United States of America?",
        "Are you legally authorized to work in the United States for any employer?",
    ]
    OFFERED_AS_ALTERNATIVE = [
        "Are you authorized to work in the United States on a permanent or temporary basis?",
        "Are you authorized to work in the U.S. on a temporary or permanent basis?",
        "Are you legally authorized to work in the United States, with or without sponsorship?",
    ]

    @pytest.mark.parametrize("label", QUALIFIED)
    def test_an_opt_holder_answers_no(self, answers, label):
        r = resolve(_yes_no_field(label), answers)
        assert (r.action, r.value, r.tier) == ("fill", "No", "B0")

    @pytest.mark.parametrize("label", QUALIFIED)
    def test_a_citizen_answers_yes(self, answers, label):
        citizen = dc_replace(
            answers,
            countries={
                **answers.countries,
                "United States": BS.CountryFacts(
                    authorized=True, requires_sponsorship=False, unrestricted=True
                ),
            },
        )
        assert resolve(_yes_no_field(label), citizen).value == "Yes"

    @pytest.mark.parametrize("label", PLAIN)
    def test_the_plain_question_answers_from_authorized(self, answers, label):
        assert resolve(_yes_no_field(label), answers).value == "Yes"

    @pytest.mark.parametrize("label", OFFERED_AS_ALTERNATIVE)
    def test_an_alternation_parks(self, answers, label):
        r = resolve(_yes_no_field(label), answers)
        assert (r.action, r.tier) == ("park", "B0")

    def test_sponsorship_is_untouched_by_the_qualifier(self, answers):
        r = resolve(
            _yes_no_field(
                "Will you now or in the future require sponsorship for employment visa status?"
            ),
            answers,
        )
        assert r.value == "Yes"


class TestTheJobsCountry:
    """An unnamed country is the job's country; a named one is its own row."""

    COUNTRY_RELATIVE = [
        "Are you authorized to work in the country where the job is located?",
        "Are you legally authorized to work in the country this position is in?",
        "Are you authorized to work in the stated location of this role?",
        "Are you legally authorized to work in your current country of employment?",
    ]

    @pytest.mark.parametrize("label", COUNTRY_RELATIVE)
    def test_a_us_role_answers_yes(self, answers, label):
        assert resolve(_yes_no_field(label), answers).value == "Yes"

    @pytest.mark.parametrize("label", COUNTRY_RELATIVE)
    def test_a_german_role_answers_from_the_default_row(self, answers, label):
        assert (
            resolve(_yes_no_field(label), dc_replace(answers, job_country="Germany")).value == "No"
        )

    @pytest.mark.parametrize(
        "label",
        [
            "Are you legally authorised to work in the UK without employer sponsorship?",
            "Are you authorized to work in Canada?",
            "Are you authorized to work in Germany?",
        ],
    )
    def test_a_named_country_answers_from_its_own_row(self, answers, label):
        assert resolve(_yes_no_field(label), answers).value == "No"

    def test_either_country_is_enough(self, answers):
        r = resolve(
            _yes_no_field("Are you legally authorized to work in the US or Canada?"), answers
        )
        assert r.value == "Yes"


class TestLoaderRejectsSilentMisconfiguration:
    """Every case here used to load clean and then quietly do nothing."""

    def _cfg(self, tmp_path, mutate):
        base = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        mutate(base)
        p = tmp_path / "answers.yaml"
        p.write_text(yaml.safe_dump(base, sort_keys=False), encoding="utf-8")
        return p

    def test_a_mistyped_top_level_key_is_an_error(self, tmp_path):
        def rename(d):
            d["questionz"] = d.pop("questions")

        with pytest.raises(AnswersError, match="unknown top-level keys"):
            load_answers(self._cfg(tmp_path, rename))

    def test_employment_flags_are_rejected_outside_the_employment_block(self, tmp_path):
        def stray(d):
            d["identity"]["current_role"] = True

        with pytest.raises(AnswersError, match="unknown keys"):
            load_answers(self._cfg(tmp_path, stray))

    def test_the_flags_still_work_where_they_belong(self, tmp_path):
        def ok(d):
            d["employment"]["only_when_required"] = True

        assert load_answers(self._cfg(tmp_path, ok)).employment["only_when_required"] is True

    def test_a_short_wording_is_still_allowed(self, tmp_path):
        def ok(d):
            d["questions"] = {"state": {"answer": "Massachusetts", "wordings": ["State"]}}

        assert load_answers(self._cfg(tmp_path, ok)).questions[0].wordings == ("state",)

    def test_an_unknown_question_key_is_refused(self, tmp_path):
        def typo(d):
            d["questions"]["linkedin"]["keyword"] = ["x"]

        with pytest.raises(AnswersError, match="unknown keys"):
            load_answers(self._cfg(tmp_path, typo))


class TestSalary:
    """A figure question takes the figure the run computed for this role; a
    Yes/No money question takes the literal fallback; no figure parks."""

    def _answers(self, tmp_path, figure):
        base = load_answers(
            write_config(
                tmp_path,
                questions={
                    "salary": {
                        "answer": {"computed": "salary", "fallback": ["Yes"]},
                        "keywords": ["salary", "compensation"],
                    }
                },
            )
        )
        return dc_replace(base, salary_figure=figure)

    def test_a_text_field_takes_the_figure(self, tmp_path):
        r = resolve(
            field(label="What are your salary expectations?"), self._answers(tmp_path, 132000.0)
        )
        assert (r.action, r.value) == ("fill", "132000")

    def test_a_select_matches_the_figure_as_an_option(self, tmp_path):
        r = resolve(
            field(label="Desired compensation", options=["$132,000", "$150,000"]),
            self._answers(tmp_path, 132000.0),
        )
        assert r.value == "$132,000"

    def test_a_yes_no_money_question_is_not_a_figure(self, tmp_path):
        r = resolve(
            field(label="Are you comfortable with the salary range posted?", options=["Yes", "No"]),
            self._answers(tmp_path, 132000.0),
        )
        assert r.value == "Yes"

    def test_no_figure_parks(self, tmp_path):
        r = resolve(
            field(label="What are your salary expectations?", required=True),
            self._answers(tmp_path, None),
        )
        assert r.action == "park"


class TestRoleFileAnswers:
    """ "Why us" comes from this role's company_answers.yaml, never the bank."""

    def _answers(self, tmp_path, company_answers):
        base = load_answers(
            write_config(
                tmp_path,
                questions={
                    "why_company": {
                        "answer": {"role_file": "why_company"},
                        "keywords": ["why do you want to work"],
                    }
                },
            )
        )
        return dc_replace(base, company_answers=company_answers)

    def test_it_fills_from_the_role_file(self, tmp_path):
        a = self._answers(tmp_path, {"why_company": "Because widgets."})
        r = resolve(field(label="Why do you want to work here?", kind="textarea"), a)
        assert r.value == "Because widgets."

    def test_it_parks_without_one(self, tmp_path):
        a = self._answers(tmp_path, {})
        r = resolve(field(label="Why do you want to work here?", kind="textarea", required=True), a)
        assert r.action == "park" and "company-answers" in r.reason


class TestLocationAlternates:
    """Boards do not agree on how to spell a place: one abbreviates state and
    country where another spells both out, and the chooser is exact-match, so
    one config string cannot answer both. `identity.location_alternates` states
    the other spellings; nothing is inferred.
    """

    LOCATION = "Exampletown, Example State, United States"
    ALTERNATE = "Exampletown, ES, USA"

    def _answers(self, tmp_path, alternates=None):
        import yaml as _yaml

        data = _yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        data["identity"]["location"] = self.LOCATION
        if alternates is not None:
            data["identity"]["location_alternates"] = alternates
        path = tmp_path / "application_answers.yaml"
        path.write_text(_yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return load_answers(path)

    def _field(self, field_id, options):
        from applyourself.apply.reconcile import MergedField, MergedOption

        return MergedField(
            id=field_id,
            name=field_id,
            label="Current location",
            required=True,
            kind="react_select",
            section="questions",
            multi=False,
            options=tuple(MergedOption(label=o, value=o) for o in options),
        )

    def test_an_alternate_answers_a_board_that_words_it_differently(self, tmp_path):
        from applyourself.apply.answers import resolve

        a = self._answers(tmp_path, [self.ALTERNATE])
        r = resolve(self._field("location", (self.ALTERNATE,)), a)
        assert (r.action, r.value) == ("fill", self.ALTERNATE)

    def test_a_blind_chooser_carries_the_listed_spellings(self, tmp_path):
        """With no option list the first spelling is picked; the others travel
        with it so the read-back accepts whichever one the board settles on."""
        from applyourself.apply.answers import resolve

        a = self._answers(tmp_path, [self.ALTERNATE])
        r = resolve(self._field("location", ()), a)
        assert r.value == self.LOCATION
        assert r.alternates == (self.LOCATION, self.ALTERNATE)

    def test_the_primary_location_still_wins_where_it_is_offered(self, tmp_path):
        from applyourself.apply.answers import resolve

        a = self._answers(tmp_path, [self.ALTERNATE])
        r = resolve(self._field("candidate-location", (self.LOCATION, self.ALTERNATE)), a)
        assert r.value == self.LOCATION, (
            "the configured location is the first candidate, not an alternate"
        )

    def test_a_board_offering_neither_parks_rather_than_guessing(self, tmp_path):
        """A measured board offered five same-named cities in five countries.
        A near-miss here sends the application out naming the wrong one."""
        from applyourself.apply.answers import resolve

        a = self._answers(tmp_path, [self.ALTERNATE])
        r = resolve(
            self._field(
                "location", ("Exampletown, Otherprovince, GTM", "Exampletown, Otherstate, VEN")
            ),
            a,
        )
        assert r.action == "park"

    def test_the_key_is_optional(self, tmp_path):
        from applyourself.apply.answers import resolve

        a = self._answers(tmp_path)
        assert a.identity.get("location_alternates") is None
        r = resolve(self._field("location", (self.LOCATION,)), a)
        assert r.action == "fill"

    def test_alternates_do_not_leak_into_other_identity_fields(self, tmp_path):
        from applyourself.apply.answers import _identity_candidates

        a = self._answers(tmp_path, [self.ALTERNATE])
        assert _identity_candidates("email", a.identity["email"], a, "email") == (
            a.identity["email"],
        )

    def test_a_non_list_is_refused(self, tmp_path):
        from applyourself.apply.bank_schema import AnswersError

        with pytest.raises(AnswersError, match="location_alternates"):
            self._answers(tmp_path, self.ALTERNATE)

    def test_an_empty_string_entry_is_refused(self, tmp_path):
        from applyourself.apply.bank_schema import AnswersError

        with pytest.raises(AnswersError, match="location_alternates"):
            self._answers(tmp_path, [""])


class TestSelfIdSignature:
    """The self-identification form attests who filled it in, and the board
    states the rule itself: "Name and date are only required if you filled out
    Disability status." We answer the disability question, so both become
    required — and neither is an opt-out question, so the option matching in
    `_resolve_eeoc` had nothing to match and skipped them. The browser's own
    constraint validation then refused the whole form: the submit click fired
    no request, the page never navigated, and the role was recorded applied on
    a submission the board never received.
    """

    def _field(self, field_id, **kw):
        return field(id=field_id, name=field_id, section="eeoc", kind="text", **kw)

    def test_the_signature_is_the_configured_name(self, tmp_path):
        a = load_answers(write_config(tmp_path))
        r = resolve(self._field("eeo[disabilitySignature]", required=True), a)
        expected = f"{a.identity['first_name']} {a.identity['last_name']}"
        assert (r.action, r.value) == ("fill", expected)

    def test_the_date_is_today_in_the_boards_format(self, tmp_path, monkeypatch):
        from datetime import date as _date

        import applyourself.apply.answers as A

        monkeypatch.setattr(A, "_today", lambda: _date(2026, 8, 19))
        a = load_answers(write_config(tmp_path))
        r = resolve(self._field("eeo[disabilitySignatureDate]", required=True), a)
        assert (r.action, r.value) == ("fill", "08/19/2026")

    def test_greenhouses_spelling_of_the_same_two_fields_resolves(self, tmp_path):
        a = load_answers(write_config(tmp_path))
        assert resolve(self._field("disability_signature"), a).action == "fill"
        assert resolve(self._field("disability_signature_date"), a).action == "fill"

    def test_the_opt_out_questions_are_untouched(self, tmp_path):
        """The signature branch must not swallow the four questions that do
        have an opt-out to pick."""
        a = load_answers(write_config(tmp_path))
        r = resolve(
            field(
                id="eeo[disability]",
                name="eeo[disability]",
                section="eeoc",
                kind="select",
                required=False,
                options=[
                    "Yes, I have a disability, or have had one in the past",
                    "I do not want to answer",
                ],
            ),
            a,
        )
        assert (r.action, r.value) == ("fill", "I do not want to answer")

    def test_it_is_tier_a2_like_the_rest_of_the_eeoc_block(self, tmp_path):
        a = load_answers(write_config(tmp_path))
        assert resolve(self._field("eeo[disabilitySignature]"), a).tier == "A2"


class TestOnlyWhenRequired:
    """A question flagged `only_when_required` leaves an optional field blank."""

    def _answers(self, tmp_path):
        qs = {
            "q0": {
                "answer": "Widget Corp",
                "wordings": ["current company"],
                "only_when_required": True,
            },
        }
        return load_answers(write_config(tmp_path, questions=qs))

    def test_an_optional_field_is_skipped(self, tmp_path):
        r = resolve(field(label="Current company", required=False), self._answers(tmp_path))
        assert r.action == "skip"

    def test_a_required_field_still_fills(self, tmp_path):
        r = resolve(field(label="Current company", required=True), self._answers(tmp_path))
        assert (r.action, r.value) == ("fill", "Widget Corp")

    def test_a_non_bool_flag_is_refused(self, tmp_path):
        qs = {
            "q0": {"answer": "X", "wordings": ["current company"], "only_when_required": "yes"},
        }
        with pytest.raises(AnswersError, match="only_when_required"):
            load_answers(write_config(tmp_path, questions=qs))


MY_DEMOGRAPHICS = {
    "answers": {
        "gender": ["Male", "Man"],
        "race": ["Asian", "South Asian"],
        "hispanic_ethnicity": ["No", "Not Hispanic or Latino"],
        "veteran_status": [
            "I am not a protected veteran",
            "No, I am not a veteran or active member",
            "No",
        ],
        "disability_status": [
            "No, I do not have a disability and have not had one in the past",
            "No",
        ],
    }
}


class TestOwnDemographicAnswers:
    """`demographics.answers`: your answer first, in the board's spelling;
    decline only where the board offers none of your spellings."""

    @pytest.fixture
    def mine(self, tmp_path):
        return load_answers(write_config(tmp_path, demographics=MY_DEMOGRAPHICS))

    @pytest.mark.parametrize(
        "field_id,options,expected",
        [
            ("gender", ["Decline To Self Identify", "Female", "Male"], "Male"),
            ("eeo[gender]", ["Male", "Female", "Decline to self-identify"], "Male"),
            ("hispanic_ethnicity", ["Yes", "No", "Decline To Self Identify"], "No"),
            ("race", ["Decline To Self Identify", "White", "Asian"], "Asian"),
            (
                "eeo[race]",
                [
                    "White (Not Hispanic or Latino)",
                    "Asian (Not Hispanic or Latino)",
                    "Decline to self-identify",
                ],
                "Asian (Not Hispanic or Latino)",
            ),
            (
                "veteran_status",
                [
                    "I identify as one or more of the classifications of a protected veteran",
                    "I am not a protected veteran",
                    "I don't wish to answer",
                ],
                "I am not a protected veteran",
            ),
            (
                "disability_status",
                [
                    "Yes, I have a disability, or have had one in the past",
                    "No, I do not have a disability and have not had one in the past",
                    "I do not want to answer",
                ],
                "No, I do not have a disability and have not had one in the past",
            ),
        ],
    )
    def test_each_eeoc_field_takes_your_answer(self, mine, field_id, options, expected):
        r = resolve(field(id=field_id, section="eeoc", kind="react_select", options=options), mine)
        assert r.value == expected and r.tier == "A2"

    def test_a_free_labelled_demographic_question_is_found_by_its_label(self, mine):
        cases = [
            (
                "How would you describe your gender identity? (mark all that apply)",
                ["Man", "Non-binary", "Woman", "I don't wish to answer"],
                ("Man",),
            ),
            (
                "How would you describe your racial/ethnic background? (mark all that apply)",
                ["East Asian", "South Asian", "Southeast Asian", "I don't wish to answer"],
                ("South Asian",),
            ),
            (
                "Are you a veteran or active member of the United States Armed Forces?",
                [
                    "Yes, I am a veteran or active member",
                    "No, I am not a veteran or active member",
                    "I don't wish to answer",
                ],
                ("No, I am not a veteran or active member",),
            ),
            (
                "What is your race and ethnicity? (Select as many that apply.)",
                [
                    "Asian: Individuals with origins in any of the original peoples of Asia.",
                    "White: Individuals with origins in Europe.",
                ],
                ("Asian: Individuals with origins in any of the original peoples of Asia.",),
            ),
        ]
        for label, options, expected in cases:
            f = field(
                id="4011224007",
                label=label,
                section="demographic",
                kind="checkbox_group",
                multi=True,
                options=options,
            )
            assert resolve(f, mine).value == expected, label

    def test_asian_never_matches_inside_another_group(self, mine):
        f = field(
            id="race",
            section="eeoc",
            kind="react_select",
            options=["East Asian", "Southeast Asian", "I don't wish to answer"],
        )
        assert resolve(f, mine).value == "I don't wish to answer"

    def test_a_short_answer_never_prefix_matches_a_decline(self, mine):
        """Your "No" is not the start of "No, prefer not to say"."""
        f = field(
            id="hispanic_ethnicity",
            section="eeoc",
            kind="react_select",
            required=True,
            options=["Yes", "No, prefer not to say"],
        )
        assert resolve(f, mine).parked

    def test_topics_you_did_not_state_still_decline(self, mine):
        for label in (
            "Do you identify as transgender? (select one)",
            "How would you describe your sexual orientation?",
        ):
            f = field(
                id="4011227007",
                label=label,
                section="demographic",
                kind="react_select",
                options=["Yes", "No", "I don't wish to answer"],
            )
            assert resolve(f, mine).value == "I don't wish to answer", label

    def test_none_of_your_spellings_declines_and_says_so(self, mine):
        f = field(
            id="gender",
            section="eeoc",
            kind="react_select",
            options=["Female", "Other", "Decline To Self Identify"],
        )
        r = resolve(f, mine)
        assert r.value == "Decline To Self Identify" and "none of" in r.reason

    def test_without_the_block_everything_declines(self, answers):
        f = field(
            id="gender",
            section="eeoc",
            kind="react_select",
            options=["Male", "Female", "Decline To Self Identify"],
        )
        assert resolve(f, answers).value == "Decline To Self Identify"

    @pytest.mark.parametrize(
        "block,needle",
        [
            ({"answers": {"pronouns": ["he/him"]}}, "a topic is one of"),
            ({"no_decline_fallback": {"gender": "Male"}}, "only `answers`"),
            ({"answers": {"gender": []}}, "must not be empty"),
        ],
    )
    def test_the_loader_refuses_a_bad_block(self, tmp_path, block, needle):
        with pytest.raises(AnswersError, match=needle):
            load_answers(write_config(tmp_path, demographics=block))


class TestFactWordings:
    """A yes/no work-authorization or export-control label, judged once against
    one fact, answered from that fact on every later board."""

    DEEMED = "Does the deemed export rule apply to you?"

    def bank(self, tmp_path, us_person="no", **judged):
        data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        data["work_authorization"]["us_person"] = us_person
        return load_answers(
            write_config(
                tmp_path,
                work_authorization=data["work_authorization"],
                fact_wordings=judged or {self.DEEMED: {"fact": "us_person", "yes_when": False}},
            )
        )

    def yes_no(self, label):
        return field(id="q", label=label, kind="react_select", required=True, options=["Yes", "No"])

    def test_an_export_question_parks_until_judged(self, answers):
        assert resolve(self.yes_no(self.DEEMED), answers).parked

    def test_a_judged_export_wording_follows_us_person(self, tmp_path):
        assert resolve(self.yes_no(self.DEEMED), self.bank(tmp_path, "no")).value == "Yes"
        assert resolve(self.yes_no(self.DEEMED), self.bank(tmp_path, "yes")).value == "No"

    def test_park_us_person_still_parks(self, tmp_path):
        assert resolve(self.yes_no(self.DEEMED), self.bank(tmp_path, "park")).parked

    def test_a_negated_sponsorship_question_follows_the_country(self, tmp_path):
        label = "Are you able to work in the United States without sponsorship?"
        bank = self.bank(tmp_path, **{label: {"fact": "requires_sponsorship", "yes_when": False}})
        assert resolve(self.yes_no(label), bank).value == "No"

    def test_only_the_exact_wording_is_answered(self, tmp_path):
        other = "Does the deemed export rule apply to you in Canada?"
        assert resolve(self.yes_no(other), self.bank(tmp_path)).parked

    @pytest.mark.parametrize(
        "spec,needle",
        [
            ({"fact": "us_person"}, "fact, yes_when"),
            ({"fact": "salary", "yes_when": True}, "fact: one of"),
        ],
    )
    def test_the_loader_refuses_a_bad_entry(self, tmp_path, spec, needle):
        with pytest.raises(AnswersError, match=needle):
            self.bank(tmp_path, **{self.DEEMED: spec})

    def test_an_ordinary_question_is_refused(self, tmp_path):
        with pytest.raises(AnswersError, match="ordinary question"):
            self.bank(
                tmp_path,
                **{"Are you willing to relocate?": {"fact": "authorized", "yes_when": True}},
            )


class TestCoverLetterSetting:
    def test_default_is_when_required(self, answers):
        assert answers.cover_letter == "when_required"

    def test_an_unknown_value_is_refused(self, tmp_path):
        with pytest.raises(AnswersError, match="cover_letter"):
            load_answers(write_config(tmp_path, cover_letter="always"))
