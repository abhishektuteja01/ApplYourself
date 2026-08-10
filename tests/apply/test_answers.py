"""What goes into a field, and what refuses to.

The resolution cases run over the five reconciled DOM/API fixture pairs rather
than hand-built fields, because the questions that break this module are the
ones no one would think to invent: a sponsorship question next to an
authorization question, a work-authorization dropdown whose options are three
sentences, "How did you hear about this job?" with a different option list on
every board.
"""
from __future__ import annotations

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

    def test_hispanic_ethnicity_is_left_blank(self, merged, answers):
        """DOM-only, so no option list to opt out against, and optional on
        every board observed."""
        f = merged("form_minimal").by_id("hispanic_ethnicity")
        assert f.dom_only and not f.options and not f.required
        assert resolve(f, answers).action == "skip"

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

    def test_a_work_authorization_question_rendered_as_free_text_parks(self, answers):
        f = field(label="Will you now or in the future require sponsorship?",
                  required=True, kind="text")
        assert resolve(f, answers).parked


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
