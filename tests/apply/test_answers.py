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

from src.apply import answers as A
from src.apply.answers import AnswersError, Resolution, load_answers, resolve
from src.apply.reconcile import MergedField, MergedOption

from .conftest import FIXTURES

PREFS = FIXTURES / "preferences_time_limited.md"
CONFIG = FIXTURES / "application_answers.yaml"


def field(**kw) -> MergedField:
    """A merged field with everything but the interesting bits defaulted."""
    base = dict(
        id="question_1", name="question_1", label="", required=False,
        kind="text", section="questions", multi=False, options=(),
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


class TestExactRules:
    """Some labels are too short to keyword. "State" is a required dropdown on
    3 of 39 live boards, and a `state` substring also hits "United States" and
    "please state their name"."""

    def _answers(self, tmp_path, rules):
        return load_answers(write_config(tmp_path, rules=rules), PREFS)

    def test_an_exact_rule_matches_the_whole_label(self, tmp_path):
        a = self._answers(tmp_path, [{"exact": ["state"], "answer": ["California"]}])
        assert resolve(field(label="State", options=["California"]), a).value == "California"

    def test_an_exact_rule_does_not_match_a_label_containing_it(self, tmp_path):
        a = self._answers(tmp_path, [{"exact": ["state"], "answer": ["California"]}])
        for label in ("United States", "Please state their name", "Home state address"):
            assert resolve(field(label=label), a).action == "skip"

    def test_punctuation_and_case_do_not_break_an_exact_match(self, tmp_path):
        a = self._answers(tmp_path, [
            {"exact": ["current position title"], "answer": "Teaching Assistant"},
        ])
        assert resolve(field(label="Current Position/Title"), a).value == "Teaching Assistant"

    def test_a_rule_may_carry_both_match_and_exact(self, tmp_path):
        a = self._answers(tmp_path, [
            {"match": ["pay expectation"], "exact": ["salary"], "answer": "Open"},
        ])
        assert resolve(field(label="Salary"), a).value == "Open"
        assert resolve(field(label="What is your pay expectation?"), a).value == "Open"

    def test_a_rule_with_neither_is_rejected(self, tmp_path):
        with pytest.raises(AnswersError, match="non-empty match or exact"):
            self._answers(tmp_path, [{"answer": "x"}])

    def test_two_rules_claiming_the_same_exact_label_is_an_error(self, tmp_path):
        with pytest.raises(AnswersError, match="both match the exact label"):
            self._answers(tmp_path, [
                {"exact": ["state"], "answer": "California"},
                {"exact": ["state"], "answer": "Massachusetts"},
            ])

    def test_a_substring_rule_shadowing_an_exact_one_is_an_error(self, tmp_path):
        # Otherwise which one wins depends on file order, and the exact rule
        # exists precisely because that label is dangerous to keyword.
        with pytest.raises(AnswersError, match="would shadow"):
            self._answers(tmp_path, [
                {"exact": ["state"], "answer": "California"},
                {"match": ["stat"], "answer": "something else"},
            ])

    def test_a_work_authorization_keyword_is_rejected_in_exact_too(self, tmp_path):
        with pytest.raises(AnswersError, match="work-authorization keyword"):
            self._answers(tmp_path, [{"exact": ["citizenship"], "answer": "x"}])


class TestLoad:
    def test_the_synthetic_fixture_loads(self, answers):
        assert answers.identity["email"] == "alex@example.com"
        assert answers.education["degree"] == "Master's Degree"
        assert answers.employment["company_name"] == "Widget Corp"
        assert answers.status == "time_limited"
        assert answers.rules

    def test_missing_file_points_at_the_template(self, tmp_path):
        with pytest.raises(AnswersError, match="application_answers.example.yaml"):
            load_answers(tmp_path / "nope.yaml", PREFS)

    def test_schema_version_must_match(self, tmp_path):
        with pytest.raises(AnswersError, match="schema_version"):
            load_answers(write_config(tmp_path, schema_version=2), PREFS)

    @pytest.mark.parametrize("key", A.IDENTITY_KEYS)
    def test_every_identity_key_is_required(self, tmp_path, answers, key):
        identity = dict(answers.identity)
        del identity[key]
        with pytest.raises(AnswersError, match=f"identity.{key}"):
            load_answers(write_config(tmp_path, identity=identity), PREFS)

    def test_an_empty_identity_value_is_not_a_value(self, tmp_path, answers):
        identity = dict(answers.identity, phone="   ")
        with pytest.raises(AnswersError, match="identity.phone"):
            load_answers(write_config(tmp_path, identity=identity), PREFS)

    def test_a_typo_in_a_block_key_is_not_silently_ignored(self, tmp_path, answers):
        identity = dict(answers.identity, emial="alex@example.com")
        with pytest.raises(AnswersError, match="unknown keys"):
            load_answers(write_config(tmp_path, identity=identity), PREFS)

    def test_education_months_are_optional(self, tmp_path, answers):
        education = {k: v for k, v in answers.education.items()
                     if k not in ("start_month", "end_month")}
        loaded = load_answers(write_config(tmp_path, education=education), PREFS)
        assert "start_month" not in loaded.education

    def test_the_employment_block_may_be_absent(self, tmp_path):
        assert load_answers(write_config(tmp_path, employment=None), PREFS).employment is None

    def test_status_must_be_one_of_the_three(self, tmp_path):
        with pytest.raises(AnswersError, match="citizen_or_pr"):
            load_answers(write_config(tmp_path, work_authorization={"status": "maybe"}), PREFS)

    def test_the_two_work_authorization_answers_are_derived_not_configured(self):
        """Independently settable booleans are booleans that can contradict
        each other, so the file states a status and nothing else."""
        assert A.WORK_AUTHORIZATION_STATUSES["citizen_or_pr"] == (True, False)
        assert A.WORK_AUTHORIZATION_STATUSES["needs_sponsorship_now"] == (False, True)
        assert A.WORK_AUTHORIZATION_STATUSES["time_limited"] == (True, True)


class TestRuleValidation:
    def test_a_rule_needs_a_non_empty_match(self, tmp_path):
        with pytest.raises(AnswersError, match="match"):
            load_answers(write_config(tmp_path, rules=[{"match": [], "answer": "x"}]), PREFS)

    def test_a_rule_needs_an_answer(self, tmp_path):
        with pytest.raises(AnswersError, match="answer"):
            load_answers(write_config(tmp_path, rules=[{"match": ["portfolio"]}]), PREFS)

    def test_answer_may_be_a_list_of_candidates(self, tmp_path):
        rules = [{"match": ["how did you hear"], "answer": ["Careers Page", "Other"]}]
        loaded = load_answers(write_config(tmp_path, rules=rules), PREFS)
        assert loaded.rules[0].answers == ("Careers Page", "Other")

    def test_overlapping_keywords_across_rules_raise(self, tmp_path):
        rules = [
            {"match": ["salary"], "answer": "Open"},
            {"match": ["desired salary"], "answer": "150000"},
        ]
        with pytest.raises(AnswersError, match="overlaps"):
            load_answers(write_config(tmp_path, rules=rules), PREFS)

    @pytest.mark.parametrize(
        "keyword",
        ["sponsor", "visa", "work authorization", "authorized to work", "citizenship"],
    )
    def test_a_work_authorization_keyword_cannot_be_a_rule(self, tmp_path, keyword):
        """One rule cannot answer both 'are you authorized to work' and 'will
        you require sponsorship' — five of the captured boards ask both."""
        rules = [{"match": [keyword], "answer": "Yes"}]
        with pytest.raises(AnswersError, match="work_authorization.status"):
            load_answers(write_config(tmp_path, rules=rules), PREFS)


class TestPreferencesCrossCheck:
    def test_the_real_preferences_section_reads_as_one_status(self):
        """The repo's own preferences.md states F-1 OPT and, two lines later,
        'requiring US security clearance or citizenship'. The second must not
        register as a citizenship claim."""
        assert A.preferences_statuses(PREFS.read_text(encoding="utf-8")) == {"time_limited"}
        assert A.preferences_statuses(
            (FIXTURES / "preferences_citizen.md").read_text(encoding="utf-8")
        ) == {"citizen_or_pr"}

    def test_a_contradiction_is_a_hard_error(self, tmp_path):
        config = write_config(tmp_path, work_authorization={"status": "citizen_or_pr"})
        with pytest.raises(AnswersError, match="states 'time_limited'"):
            load_answers(config, PREFS)

    def test_no_derivable_status_is_a_hard_error(self, tmp_path):
        prefs = tmp_path / "preferences.md"
        prefs.write_text("## Work authorization\n- It's complicated.\n", encoding="utf-8")
        with pytest.raises(AnswersError, match="no recognizable status"):
            load_answers(CONFIG, prefs)

    def test_the_preferences_template_states_all_three_and_is_rejected(self, tmp_path):
        """preferences.example.md carries all three bullets and says to keep
        one. An unedited copy must not pass the check."""
        with pytest.raises(AnswersError, match="at once"):
            load_answers(CONFIG, A.PROFILE / "preferences.example.md")

    def test_a_missing_preferences_file_does_not_skip_the_check(self, tmp_path):
        with pytest.raises(AnswersError, match="cannot be cross-checked"):
            load_answers(CONFIG, tmp_path / "gone.md")

    def test_only_the_work_authorization_section_is_read(self, tmp_path):
        prefs = tmp_path / "preferences.md"
        prefs.write_text(
            "## Work authorization\n- F-1 OPT, STEM-eligible.\n\n"
            "## Preferences\n- Would relocate for a green card sponsor.\n",
            encoding="utf-8",
        )
        assert load_answers(CONFIG, prefs).status == "time_limited"


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
            id="_systemfield_location", name="_systemfield_location",
            label="Preferred location", required=True, kind="react_select",
            section="basic", multi=False,
            options=(MergedOption(label="United States"),
                     MergedOption(label="Palestine")),
        )
        assert resolve(field, answers).value == "United States"

    def test_the_city_still_wins_where_the_board_offers_it(self, answers):
        """The fallback is second, not a replacement."""
        field = MergedField(
            id="_systemfield_location", name="_systemfield_location",
            label="Preferred location", required=True, kind="react_select",
            section="basic", multi=False,
            options=(MergedOption(label="United States"),
                     MergedOption(label=answers.identity["location"])),
        )
        assert resolve(field, answers).value == answers.identity["location"]

    def test_a_board_offering_neither_still_parks(self, answers):
        field = MergedField(
            id="_systemfield_location", name="_systemfield_location",
            label="Preferred location", required=True, kind="react_select",
            section="basic", multi=False,
            options=(MergedOption(label="Ireland"),),
        )
        assert resolve(field, answers).parked is True

    def test_the_fallback_is_keyed_on_the_dom_id_not_the_label(self, answers):
        """`identity.country` also feeds Greenhouse's phone dial-code widget,
        which is not a country question."""
        from src.apply.answers import _identity_candidates
        assert len(_identity_candidates("_systemfield_location", "x", answers)) == 2
        assert _identity_candidates("email", "x", answers) == ("x",)

    def test_greenhouse_and_lever_location_fields_are_left_alone(self, answers):
        """They map to the same config key but carry no option list at plan
        time, so widening the fallback to the key would only change behaviour
        at fill time, on the highest-volume lane, on no evidence."""
        from src.apply.answers import _identity_candidates
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

    def test_an_unset_optional_education_field_is_skipped(self, tmp_path, merged, answers):
        education = {k: v for k, v in answers.education.items() if k != "start_month"}
        thin = load_answers(write_config(tmp_path, education=education), PREFS)
        assert resolve(merged("form_education").by_id("start-month--0"), thin).action == "skip"

    def test_the_employment_block_fills_including_the_checkbox(self, merged, answers):
        r = merged("form_employment")
        assert resolve(r.by_id("company-name-0"), answers).value == "Widget Corp"
        assert resolve(r.by_id("title-0"), answers).value == "Widget Operations Analyst"
        assert resolve(r.by_id("end-date-year-0"), answers).value == "2025"
        assert resolve(r.by_id("current-role-0_1"), answers).value is False

    def test_a_board_asking_for_employment_parks_when_the_block_is_absent(
        self, tmp_path, merged
    ):
        """Rare — 1 board in 60 — but six of its seven fields are required."""
        without = load_answers(write_config(tmp_path, employment=None), PREFS)
        parked = [
            f.id for f in merged("form_employment").by_section("employment")
            if resolve(f, without).parked
        ]
        assert parked == [
            "company-name-0", "title-0", "start-date-month-0",
            "start-date-year-0", "end-date-month-0", "end-date-year-0",
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

    def test_hispanic_ethnicity_has_nothing_to_opt_out_against_statically(
        self, merged, answers
    ):
        """DOM-only, so neither the API nor the served HTML carries its options
        and nothing here can match an opt-out string. Optional, so it is skipped;
        required, it parks. fill.py re-resolves it from the opened widget, which
        does offer Decline To Self Identify."""
        f = merged("form_minimal").by_id("hispanic_ethnicity")
        assert f.dom_only and not f.options and not f.required
        assert resolve(f, answers).action == "skip"

    def test_hispanic_ethnicity_opts_out_once_its_options_are_known(self, answers):
        """The live widget offers exactly these three."""
        resolution = resolve(field(
            id="hispanic_ethnicity", section="eeoc", kind="react_select", required=True,
            options=["Yes", "No", "Decline To Self Identify"],
        ), answers)
        assert resolution.action == "fill"
        assert resolution.value == "Decline To Self Identify"

    def test_a_required_hispanic_ethnicity_with_no_decline_option_still_parks(self, answers):
        """Answering it substantively is a claim about the user, not a
        content-free opt-out, so it is not guessed."""
        assert resolve(field(
            id="hispanic_ethnicity", section="eeoc", kind="react_select", required=True,
            options=["Yes", "No"],
        ), answers).action == "park"

    def test_the_eeoc_block_is_resolved_by_section_not_by_keyword(self, answers):
        """An employer-authored question that reads like EEOC stays in
        `questions` and is rule-matched — form_multiselect asks 'I identify my
        race as' as its own question, and nothing here answers it."""
        assert resolve(field(label="Disability Status", required=True,
                             kind="react_select", options=["Yes", "No"]), answers).parked

    def test_an_eeoc_question_with_no_opt_out_option_parks_when_required(self, answers):
        f = field(id="gender", section="eeoc", required=True,
                  kind="react_select", options=["Female", "Male"])
        assert resolve(f, answers).parked


class TestDemographic:
    def test_the_flagged_decline_option_wins(self, answers):
        f = field(id="4005807007", section="demographic", kind="react_select", multi=True,
                  options=[MergedOption(label="Woman"),
                           MergedOption(label="Opt out", decline_to_answer=True)])
        assert resolve(f, answers).value == ("Opt out",)

    def test_the_label_is_ignored_and_the_opt_out_string_is_matched(self, merged, answers):
        """Employer-authored labels vary — 13 distinct over 4 boards — so
        resolution is by option, never by question."""
        block = merged("form_demographic").by_section("demographic")
        assert block
        for f in block:
            assert resolve(f, answers).value == ("I don't wish to answer",) if f.multi \
                else resolve(f, answers).value == "I don't wish to answer"

    def test_a_required_question_with_no_way_out_parks(self, answers):
        f = field(id="123", section="demographic", required=True, kind="react_select",
                  options=["Yes", "No"])
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
        """The case one keyword rule cannot express: authorized yes,
        sponsorship no, on boards that ask both."""
        citizen = load_answers(
            write_config(tmp_path, work_authorization={"status": "citizen_or_pr"}),
            FIXTURES / "preferences_citizen.md",
        )
        expected = "Yes" if (fixture, needle) in self.AUTHORIZED else "No"
        assert resolve(by_label(merged(fixture), needle), citizen).value == expected

    def test_an_authorization_question_naming_another_country_parks(self, merged, answers):
        """'Are you legally authourized to work in South Africa?' — misspelled,
        and US authorization says nothing about it."""
        f = by_label(merged("form_multiselect"), "authourized to work in South Africa")
        assert resolve(f, answers).parked

    def test_a_dropdown_of_prose_options_parks(self, answers):
        """A real board's 'Work Authorization' select offers three sentences,
        not Yes/No. Picking one is judgment."""
        f = field(label="Work Authorization", required=True, kind="react_select", options=[
            "I am authorized to work without sponsorship or restrictions for any employer in the U.S.",
            "I am ONLY allowed to work for my current employer in the U.S. and I will require "
            "sponsorship now or in the future to work in the US",
            "My status to work in the U.S. is unknown",
        ])
        assert resolve(f, answers).parked

    def test_a_citizenship_dropdown_parks(self, merged, answers):
        f = by_label(merged("form_multiselect"), "Citizenship")
        assert resolve(f, answers).parked

    @pytest.mark.parametrize(
        "needle", ["What visa type do you hold", "When does your visa expire"]
    )
    def test_optional_visa_free_text_is_left_blank_not_parked(self, merged, answers, needle):
        f = by_label(merged("form_multiselect"), needle)
        assert not f.required
        assert resolve(f, answers).action == "skip"

    def test_a_negated_sponsorship_question_is_never_guessed(self, answers):
        f = field(label="Are you able to work without sponsorship, now or in the future?",
                  required=True, kind="react_select", options=["Yes", "No"])
        assert resolve(f, answers).parked

    def test_a_work_authorization_question_rendered_as_free_text_is_written_out(
        self, answers
    ):
        """A yes/no question in a text box is still a yes/no question, and
        writing the word is the same answer as picking the option. This used to
        park; 11 groups in the 237-board harvest are exactly this shape, every
        one of them an ordinary sponsorship or authorization question."""
        f = field(label="Will you now or in the future require sponsorship?",
                  required=True, kind="text")
        r = resolve(f, answers)
        assert r.action == "fill"
        assert r.value == "Yes"          # time_limited requires it


class TestRules:
    def test_a_keyword_matches_the_normalized_label(self, merged, answers):
        r = merged("form_minimal")
        assert resolve(by_label(r, "LinkedIn"), answers).value == "https://linkedin.com/in/example"
        assert resolve(by_label(r, "Website"), answers).value == "https://example.com"

    def test_a_dropdown_answer_falls_through_to_an_option_the_board_offers(
        self, merged, answers
    ):
        """Eight sampled boards, eight disjoint option sets for the same
        question. One string would miss almost all of them."""
        assert resolve(
            by_label(merged("form_minimal"), "How did you hear"), answers
        ).value == "Other"
        assert resolve(
            by_label(merged("form_multiselect"), "How did you hear"), answers
        ).value == "Careers Page"
        assert resolve(
            by_label(merged("form_employment"), "How did you hear"), answers
        ).value == "Other"

    def test_the_same_rule_fills_free_text_with_its_first_candidate(self, merged, answers):
        f = by_label(merged("form_demographic"), "How did you hear")
        assert f.kind == "text" and not f.options
        assert resolve(f, answers).value == "Careers Page"

    def test_an_answer_the_board_does_not_offer_parks_rather_than_guessing(self, answers):
        f = field(label="How did you hear about this job?", required=True,
                  kind="react_select", options=["Recruiter", "Conference"])
        parked = resolve(f, answers)
        assert parked.parked and "Careers Page" in parked.reason

    def test_a_multi_answer_comes_back_as_a_list(self, answers):
        f = field(label="What pronouns do you prefer to go by?", kind="react_select",
                  multi=True, options=["She/Her", "He/Him", "They/Them"])
        assert resolve(f, answers).value == ("They/Them",)

    def test_a_rule_never_fills_a_file_input(self, answers):
        f = field(id="portfolio_upload", label="Portfolio", required=True, kind="file")
        assert resolve(f, answers).parked

    def test_rule_order_is_the_precedence(self, tmp_path, merged):
        rules = [
            {"match": ["notice period"], "answer": "Two weeks."},
            {"match": ["what is your"], "answer": "Fallback."},
        ]
        loaded = load_answers(write_config(tmp_path, rules=rules), PREFS)
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
        loaded = load_answers(A.EXAMPLE_PATH, PREFS)
        assert loaded.identity["first_name"]
        assert loaded.status in A.WORK_AUTHORIZATION_STATUSES

    def test_its_rules_answer_the_boards_in_the_fixtures(self, merged):
        """A template whose rules match nothing teaches the wrong shape."""
        loaded = load_answers(A.EXAMPLE_PATH, PREFS)
        r = merged("form_minimal")
        assert resolve(by_label(r, "How did you hear"), loaded).action == "fill"
        assert resolve(by_label(r, "LinkedIn"), loaded).action == "fill"


def _yes_no_field(label: str, *, required: bool = True):
    """A required Yes/No select — the shape every work-authorization question
    in the fixtures actually has (§5)."""
    from src.apply.domscan import DomOption
    return MergedField(
        id="q", name="q", label=label, required=required, kind="select",
        section="questions", multi=False, api_type="multi_value_single_select",
        options=(DomOption(value="1", label="Yes"), DomOption(value="0", label="No")),
    )



class TestTheAuthorizationFamilyNeverGuessesAQualifiedQuestion:
    """`authorized_now` answers exactly one question: may you work in the US
    today. Any scope qualifier asks something else, and answering it from the
    status states a falsehood for `time_limited` — a false legal claim
    submitted under the user's real name.

    Both wrong labels below are verbatim from the captured live-board
    fixtures, not invented.
    """

    QUALIFIED = [
        "Are you permanently authorized to work for any employer in the United States?",
        "I am authorized to work without sponsorship or restrictions for any "
        "employer in the U.S.",
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
    ]

    @pytest.mark.parametrize("label", QUALIFIED)
    def test_a_qualified_question_is_never_answered(self, answers, label):
        r = resolve(_yes_no_field(label), answers)
        assert r.action == "park", f"answered {r.value!r} to a question it cannot know"
        assert r.tier == "B0"

    @pytest.mark.parametrize("label", PLAIN)
    def test_the_plain_question_still_resolves(self, answers, label):
        # The guard must not park everything — that would block every board.
        r = resolve(_yes_no_field(label), answers)
        assert r.action == "fill"
        assert r.value == "Yes"          # time_limited is authorized today

    def test_the_sponsorship_family_is_untouched_by_the_guard(self, answers):
        # "now or in the future" is a qualifier on the authorization question
        # but the normal phrasing of the sponsorship one.
        r = resolve(_yes_no_field(
            "Will you now or in the future require sponsorship for employment "
            "visa status?"), answers)
        assert r.action == "fill"
        assert r.value == "Yes"          # time_limited will need it later


class TestEmployerBreadthIsNotAScopeQualifier:
    """"for any employer" reads like a qualifier and is not one. It asks about
    employer-tying, and a time-limited status is not employer-tied the way an
    H-1B is — the honest answer is Yes."""

    def test_for_any_employer_alone_resolves_from_the_status(self, answers):
        r = resolve(_yes_no_field(
            "Are you legally authorized to work in the United States for any "
            "employer?"), answers)
        assert r.action == "fill"
        assert r.value == "Yes"

    def test_paired_with_a_real_qualifier_it_still_parks(self, answers):
        """The permanence claim is the thing that cannot be answered; dropping
        "for any employer" from the qualifier set must not take this with it."""
        r = resolve(_yes_no_field(
            "Are you permanently authorized to work for any employer in the "
            "United States?"), answers)
        assert r.action == "park"


class TestAnUnnamedCountryIsTheJobsCountry:
    """The queue only ever holds roles that passed discovery's US location
    allowlist, so "the country where this role is located" is a US question.
    Demanding an explicit "United States" parked 14 of 21 required work-auth
    questions across 53 live Ashby boards for no gain."""

    COUNTRY_RELATIVE = [
        "Are you authorized to work in the country where the job is located?",
        "Are you legally authorized to work in the country this position is in?",
        "Are you authorized to work in the stated location of this role?",
        "Are you legally authorized to work in your current country of employment?",
    ]
    # "Do you have the legal **right** to work in..." reaches the country check
    # too now — `_AUTHORIZED_FAMILY` learned that spelling, along with "eligible
    # to work" and "have valid work authorization", in the corpus pass. Every
    # real phrasing of all three lives in test_work_auth_corpus.py.

    @pytest.mark.parametrize("label", COUNTRY_RELATIVE)
    def test_a_country_relative_question_resolves(self, answers, label):
        r = resolve(_yes_no_field(label), answers)
        assert r.action == "fill"
        assert r.value == "Yes"          # time_limited is authorized today

    NAMES_ELSEWHERE = [
        "Are you legally authorised to work in the UK without employer sponsorship?",
        "Are you authorized to work in Canada?",
        "Are you legally authourized to work in South Africa?",
        "Are you authorized to work in Germany?",
    ]

    @pytest.mark.parametrize("label", NAMES_ELSEWHERE)
    def test_a_question_naming_another_country_still_parks(self, answers, label):
        """US authorization says nothing about these, and the location
        allowlist gives no cover for a country the question names outright."""
        r = resolve(_yes_no_field(label), answers)
        assert r.action == "park"
        assert r.tier == "B0"

    def test_the_us_named_alongside_another_country_resolves(self, answers):
        """Checked after the US test on purpose, so an either/or question is
        answered rather than parked."""
        r = resolve(_yes_no_field(
            "Are you legally authorized to work in the US or Canada?"), answers)
        assert r.action == "fill"
        assert r.value == "Yes"


class TestScopeQualifiedAnswerIsConfiguredNotInferred:
    """The park above is the *default*, not a policy. Whether "permanently
    authorized for any employer" is Yes or No is a fact about the user, so it
    is theirs to state once in config; `src/` only reads it (R7).

    Without this the questions are unanswerable by anything: they resolve at
    tier B0, and B0 parks are deliberately not reachable by the command
    session's Tier C overrides, so a parked one means applying by hand.
    """

    QUALIFIED = TestTheAuthorizationFamilyNeverGuessesAQualifiedQuestion.QUALIFIED
    PLAIN = TestTheAuthorizationFamilyNeverGuessesAQualifiedQuestion.PLAIN

    @pytest.mark.parametrize("label", QUALIFIED)
    def test_no_answers_every_qualified_question_no(self, answers, label):
        a = dc_replace(answers, scope_qualified_answer="no")
        r = resolve(_yes_no_field(label), a)
        assert r.action == "fill"
        assert r.value == "No"
        assert r.tier == "B0"

    @pytest.mark.parametrize("label", QUALIFIED)
    def test_yes_answers_every_qualified_question_yes(self, answers, label):
        a = dc_replace(answers, scope_qualified_answer="yes")
        r = resolve(_yes_no_field(label), a)
        assert r.action == "fill"
        assert r.value == "Yes"

    @pytest.mark.parametrize("label", PLAIN)
    @pytest.mark.parametrize("setting", ["park", "yes", "no"])
    def test_the_plain_question_never_consults_the_setting(self, answers, label, setting):
        """The setting governs qualified questions only. "Are you authorized to
        work in the US today?" is answered by `status`, and a user who sets
        `no` for the qualified family must not start telling boards they cannot
        work here at all."""
        a = dc_replace(answers, scope_qualified_answer=setting)
        r = resolve(_yes_no_field(label), a)
        assert r.action == "fill"
        assert r.value == "Yes"

    # The qualifier is one branch of an alternation, so the question is wider
    # than the setting assumes and "No" would be a false self-disqualification.
    OFFERED_AS_ALTERNATIVE = [
        "Are you authorized to work in the United States on a permanent or "
        "temporary basis?",
        "Are you authorized to work in the U.S. on a temporary or permanent basis?",
        "Are you legally authorized to work in the United States, with or "
        "without sponsorship?",
    ]

    @pytest.mark.parametrize("label", OFFERED_AS_ALTERNATIVE)
    @pytest.mark.parametrize("setting", ["park", "yes", "no"])
    def test_an_alternation_parks_whatever_the_setting_says(self, answers, label,
                                                            setting):
        a = dc_replace(answers, scope_qualified_answer=setting)
        r = resolve(_yes_no_field(label), a)
        assert r.action == "park", f"answered {r.value!r} to a widened question"
        assert r.tier == "B0"

    def test_a_citizen_never_reaches_the_setting(self, answers):
        """`citizen_or_pr` already answers the qualified family from `status`
        — the switch exists for the statuses that cannot."""
        a = dc_replace(answers, status="citizen_or_pr", scope_qualified_answer="no")
        r = resolve(_yes_no_field(self.QUALIFIED[0]), a)
        assert r.action == "fill"
        assert r.value == "Yes"

    def test_the_default_is_park(self, answers):
        assert answers.scope_qualified_answer == "park"


class TestScopeQualifiedAnswerLoading:
    def _cfg(self, tmp_path, value):
        base = yaml.safe_load((FIXTURES / "application_answers.yaml").read_text(
            encoding="utf-8"))
        base["work_authorization"]["scope_qualified_answer"] = value
        p = tmp_path / "answers.yaml"
        p.write_text(yaml.safe_dump(base, sort_keys=False), encoding="utf-8")
        return p

    def _load(self, path):
        return load_answers(path, FIXTURES / "preferences_time_limited.md")

    @pytest.mark.parametrize("value", ["park", "yes", "no"])
    def test_the_three_settings_load(self, tmp_path, value):
        assert self._load(self._cfg(tmp_path, value)).scope_qualified_answer == value

    def test_an_unquoted_yaml_boolean_is_refused_not_coerced(self, tmp_path):
        """`scope_qualified_answer: no` (unquoted) parses as the boolean False.
        Mapping that to "no" would let YAML's own quirk decide a legal answer,
        so it is an error and the message says to quote it."""
        with pytest.raises(AnswersError, match="quote it"):
            self._load(self._cfg(tmp_path, False))

    def test_an_unrecognized_setting_is_refused(self, tmp_path):
        with pytest.raises(AnswersError, match="scope_qualified_answer"):
            self._load(self._cfg(tmp_path, "maybe"))

    def test_an_unknown_key_in_the_block_is_refused(self, tmp_path):
        """The block was the one place with no unknown-key check, so
        `scope_qualifed_answer` (typo) would silently leave the default."""
        base = yaml.safe_load((FIXTURES / "application_answers.yaml").read_text(
            encoding="utf-8"))
        base["work_authorization"]["scope_qualifed_answer"] = "no"
        p = tmp_path / "answers.yaml"
        p.write_text(yaml.safe_dump(base, sort_keys=False), encoding="utf-8")
        with pytest.raises(AnswersError, match="unknown keys"):
            self._load(p)


class TestLoaderRejectsSilentMisconfiguration:
    """Every case here used to load clean and then quietly do nothing."""

    def _cfg(self, tmp_path, mutate):
        base = yaml.safe_load((FIXTURES / "application_answers.yaml").read_text(
            encoding="utf-8"))
        mutate(base)
        p = tmp_path / "answers.yaml"
        p.write_text(yaml.safe_dump(base, sort_keys=False), encoding="utf-8")
        return p

    def test_a_mistyped_top_level_key_is_an_error(self, tmp_path):
        # `rulez:` loaded with zero rules; every Tier B question then parked
        # and nothing said why.
        def rename(d):
            d["rulez"] = d.pop("rules")
        with pytest.raises(AnswersError, match="unknown top-level keys"):
            load_answers(self._cfg(tmp_path, rename),
                          FIXTURES / "preferences_time_limited.md")

    def test_employment_flags_are_rejected_outside_the_employment_block(self, tmp_path):
        def stray(d):
            d["identity"]["current_role"] = True
        with pytest.raises(AnswersError, match="unknown keys"):
            load_answers(self._cfg(tmp_path, stray),
                          FIXTURES / "preferences_time_limited.md")

    def test_the_flags_still_work_where_they_belong(self, tmp_path):
        def ok(d):
            d.setdefault("employment", {
                "company_name": "Widget Corp", "title": "Widget Engineer",
                "start_month": "January", "start_year": "2020",
                "end_month": "June", "end_year": "2024",
            })
            d["employment"]["only_when_required"] = True
        answers = load_answers(self._cfg(tmp_path, ok),
                                FIXTURES / "preferences_time_limited.md")
        assert answers.employment["only_when_required"] is True

    def test_a_keyword_that_collapses_to_one_character_is_refused(self, tmp_path):
        # `C++` -> `c`, which matched "What are your salary expectations?".
        def degenerate(d):
            d["rules"] = [{"match": ["C++"], "answer": "5 years"}]
        with pytest.raises(AnswersError, match="under 3 characters"):
            load_answers(self._cfg(tmp_path, degenerate),
                          FIXTURES / "preferences_time_limited.md")

    def test_a_short_exact_label_is_still_allowed(self, tmp_path):
        # `exact:` compares the whole label, so shortness is harmless — this is
        # the documented escape for labels that cannot be keyworded (§4's
        # `State` dropdown).
        def ok(d):
            d["rules"] = [{"exact": ["State"], "answer": "Massachusetts"}]
        answers = load_answers(self._cfg(tmp_path, ok),
                                FIXTURES / "preferences_time_limited.md")
        assert answers.rules[0].exact == ("state",)
