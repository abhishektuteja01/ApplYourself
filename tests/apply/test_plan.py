"""What would be typed into which selector, and what refuses to be planned.

Everything here runs offline against the five captured DOM/API pairs. The plan
is the last checkpoint before a browser exists, so the tests that matter are the
ones about what must NOT happen: a required field quietly dropping out, a
guessed value going into one, a lock file being uploaded as a resume.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.apply import plan as P
from src.apply.answers import Resolution
from src.apply.greenhouse import BoardForm, Posting
from src.apply.plan import (
    FieldPlan,
    FilePlan,
    Plan,
    PlanError,
    Skipped,
    Unmapped,
    build_plan,
    find_artifact,
    plan_for_board,
)
from src.apply.reconcile import MergedField, MergedOption, Reconciled

from .conftest import FORM_FIXTURES


def field(**kw) -> MergedField:
    base = dict(
        id="question_1", name="question_1", label="A question", required=False,
        kind="text", section="questions", multi=False, options=(),
    )
    base.update(kw)
    return MergedField(**base)


def one(fields, api_only=()) -> Reconciled:
    return Reconciled(fields=tuple(fields), api_only=tuple(api_only))


def plan_of(name, merged, answers, out_dir, **kw) -> Plan:
    return build_plan(merged(name), answers, out_dir, **kw)


class TestEveryFixture:
    """Invariants that must hold on all five boards, not just the easy one."""

    @pytest.mark.parametrize("name", FORM_FIXTURES)
    def test_every_rendered_field_is_accounted_for_exactly_once(
        self, name, merged, answers, tailor_dir
    ):
        reconciled = merged(name)
        plan = build_plan(reconciled, answers, tailor_dir)
        planned = [i.id for group in (plan.fields, plan.files, plan.unmapped,
                                      plan.draftable, plan.skipped)
                   for i in group]
        assert sorted(planned) == sorted(f.id for f in reconciled.fields)
        assert len(planned) == len(set(planned))

    @pytest.mark.parametrize("name", FORM_FIXTURES)
    def test_every_required_field_is_filled_attached_or_parked(
        self, name, merged, answers, tailor_dir
    ):
        reconciled = merged(name)
        plan = build_plan(reconciled, answers, tailor_dir)
        resolved = {f.id for f in plan.fields} | {f.id for f in plan.files}
        parked = {u.id for u in plan.unmapped}
        for f in reconciled.fields:
            if f.required:
                assert f.id in resolved or f.id in parked, f.id

    @pytest.mark.parametrize("name", FORM_FIXTURES)
    def test_no_planned_value_is_blank(self, name, merged, answers, tailor_dir):
        for f in build_plan(merged(name), answers, tailor_dir).fields:
            if isinstance(f.value, bool):
                continue
            values = f.value if isinstance(f.value, tuple) else (f.value,)
            assert values and all(str(v).strip() for v in values), f.id

    @pytest.mark.parametrize("name", FORM_FIXTURES)
    def test_a_parked_question_carries_no_value_anywhere(
        self, name, merged, answers, tailor_dir
    ):
        plan = build_plan(merged(name), answers, tailor_dir)
        parked = {u.id for u in plan.unmapped}
        assert parked.isdisjoint({f.id for f in plan.fields})
        assert parked.isdisjoint({f.id for f in plan.files})

    @pytest.mark.parametrize("name", FORM_FIXTURES)
    def test_react_selects_are_flagged_for_the_selection_assert(
        self, name, merged, answers, tailor_dir
    ):
        # The widget that silently accepts a string matching no option (§9).
        for f in build_plan(merged(name), answers, tailor_dir).fields:
            assert f.needs_selection_assert == (f.kind == "react_select")


class TestFullyResolvedBoard:
    """form_education is the one captured board every field of which resolves."""

    def test_it_is_ready_to_submit(self, merged, answers, tailor_dir):
        plan = build_plan(merged("form_education"), answers, tailor_dir,
                          submit_selector="#application-form button[type=submit]")
        assert plan.unmapped == ()
        assert plan.parked is False
        assert plan.submittable is True
        assert plan.required_parked == ()

    def test_identity_and_education_values_come_from_config(self, merged, answers, tailor_dir):
        plan = build_plan(merged("form_education"), answers, tailor_dir)
        by_id = {f.id: f for f in plan.fields}
        assert by_id["email"].value == "alex@example.com"
        assert by_id["email"].tier == "A"
        assert by_id["school--0"].value == "Example University"
        assert by_id["degree--0"].value == "Master's Degree"

    def test_both_artifacts_are_attached(self, merged, answers, tailor_dir):
        plan = build_plan(merged("form_education"), answers, tailor_dir)
        attached = {f.id: f.path for f in plan.files}
        assert attached["resume"].name == "Alex_Example_Resume.pdf"
        assert attached["cover_letter"].name == "Alex_Example_Cover_Letter.pdf"

    def test_no_submit_selector_means_not_submittable(self, merged, answers, tailor_dir):
        plan = build_plan(merged("form_education"), answers, tailor_dir,
                          submit_selector=None)
        assert plan.parked is False
        assert plan.submittable is False


class TestUnmapped:
    def test_an_unmatched_question_keeps_its_label_and_options(
        self, merged, answers, tailor_dir
    ):
        reconciled = merged("form_minimal")
        plan = build_plan(reconciled, answers, tailor_dir)
        assert plan.parked is True
        source = {f.id: f for f in reconciled.fields}
        for u in plan.unmapped:
            assert u.label == source[u.id].label
            assert u.label.strip()
            assert u.options == tuple(o.label for o in source[u.id].options)
            assert u.reason

    def test_required_parked_lists_only_the_required_ones(self, merged, answers, tailor_dir):
        plan = build_plan(merged("form_multiselect"), answers, tailor_dir)
        assert set(plan.required_parked) == {u.id for u in plan.unmapped if u.required}
        assert plan.submittable is False

    def test_an_optional_unmatched_free_text_is_draftable_not_parked(
        self, answers, tailor_dir
    ):
        """An optional "why do you want to work here" must not go out blank
        just because it does not block submission."""
        plan = build_plan(
            one([field(id="question_9", label="Why do you want to work here?",
                       kind="textarea", required=False)]),
            answers, tailor_dir,
        )
        assert plan.unmapped == ()
        assert plan.skipped == ()
        assert [d.id for d in plan.draftable] == ["question_9"]
        assert plan.draftable[0].label == "Why do you want to work here?"

    def test_a_draftable_question_does_not_park_the_role(self, answers, tailor_dir):
        plan = build_plan(
            one([field(id="question_9", label="Additional Information",
                       kind="textarea", required=False)]),
            answers, tailor_dir,
        )
        assert plan.parked is False

    def test_an_optional_checkbox_is_skipped_not_drafted(self, answers, tailor_dir):
        # A stray checkbox is not something a person would write an answer into.
        plan = build_plan(
            one([field(id="question_9", label="Subscribe?", kind="checkbox",
                       required=False)]),
            answers, tailor_dir,
        )
        assert plan.draftable == ()
        assert [s.id for s in plan.skipped] == ["question_9"]


class TestOverrides:
    """`/apply`'s per-run Tier C overrides (§10/§15) — the only way a required
    Tier C question can ever leave unmapped[] without judgment landing in
    this module or a company-specific answer leaking into
    profile/application_answers.yaml."""

    def test_an_override_resolves_a_required_tier_c_park(self, answers, tailor_dir):
        why_us = field(id="question_9", label="Why do you want to work here?",
                        kind="textarea", required=True)
        plan = build_plan(one([why_us]), answers, tailor_dir)
        assert [u.id for u in plan.unmapped] == ["question_9"]

        plan = build_plan(
            one([why_us]), answers, tailor_dir,
            overrides={"question_9": ("Drafted from bullets.md.", "C1")},
        )
        assert plan.unmapped == ()
        assert plan.parked is False
        assert plan.fields[0].value == "Drafted from bullets.md."
        assert plan.fields[0].tier == "C1"

    def test_an_override_resolves_an_optional_tier_c_draftable(self, answers, tailor_dir):
        why_us = field(id="question_9", label="Why do you want to work here?",
                        kind="textarea", required=False)
        plan = build_plan(
            one([why_us]), answers, tailor_dir,
            overrides={"question_9": ("From company_answers.md.", "C2")},
        )
        assert plan.draftable == ()
        assert plan.fields[0].value == "From company_answers.md."
        assert plan.fields[0].tier == "C2"

    def test_missing_override_still_parks(self, answers, tailor_dir):
        why_us = field(id="question_9", label="Why do you want to work here?",
                        kind="textarea", required=True)
        plan = build_plan(
            one([why_us]), answers, tailor_dir,
            overrides={"some_other_field": ("x", "C1")},
        )
        assert [u.id for u in plan.unmapped] == ["question_9"]

    def test_an_override_never_touches_a_non_tier_c_field(self, answers, tailor_dir):
        # first_name is Tier A — an override keyed to it must be ignored, not
        # silently overwrite an identity answer.
        plan = build_plan(
            one([field(id="first_name", label="First Name", kind="text")]),
            answers, tailor_dir,
            overrides={"first_name": ("Somebody Else", "C1")},
        )
        assert plan.fields[0].value != "Somebody Else"

    def test_a_bad_override_value_raises_not_parks(self, answers, tailor_dir):
        # A tuple into a single-valued textarea is exactly what _check_value
        # exists to catch — a resolver bug (here, the command feeding a bad
        # override) must not read as an unanswerable question.
        why_us = field(id="question_9", label="Why do you want to work here?",
                        kind="textarea", required=True)
        with pytest.raises(PlanError):
            build_plan(
                one([why_us]), answers, tailor_dir,
                overrides={"question_9": (("a", "b"), "C1")},
            )

    def test_plan_for_board_passes_overrides_through(
        self, merged, answers, tailor_dir, scan, payload
    ):
        from src.apply.schema import parse_schema

        name = "form_minimal"
        board_form = BoardForm(
            posting=Posting(token="1", url_slug=None), slug="gasketworks", html="",
            scan=scan(name), schema=parse_schema(payload(name)), reconciled=merged(name),
        )
        plan = plan_for_board(board_form, answers, tailor_dir, job_id="a1b2c3d4")
        parked = {u.id: u for u in plan.unmapped}
        assert parked  # form_minimal parks on at least one Tier C question

        # The parked question here is a Yes/No react-select, so the override
        # has to name an option the widget actually offers.
        field_id = next(iter(parked))
        assert parked[field_id].options == ("Yes", "No")

        overridden = plan_for_board(
            board_form, answers, tailor_dir, job_id="a1b2c3d4",
            overrides={field_id: ("No", "C1")},
        )
        assert field_id not in [u.id for u in overridden.unmapped]
        assert [f.value for f in overridden.fields if f.id == field_id] == ["No"]

    def test_an_override_the_widget_does_not_offer_parks_instead_of_planning_it(
        self, merged, answers, tailor_dir, scan, payload
    ):
        """An override is LLM-drafted prose. Every deterministic path into a
        select is checked against the offered options; this one used to skip
        that, so the plan read READY and the role died in the browser."""
        from src.apply.schema import parse_schema

        name = "form_minimal"
        board_form = BoardForm(
            posting=Posting(token="1", url_slug=None), slug="gasketworks", html="",
            scan=scan(name), schema=parse_schema(payload(name)), reconciled=merged(name),
        )
        field_id = next(u.id for u in
                        plan_for_board(board_form, answers, tailor_dir,
                                        job_id="a1b2c3d4").unmapped)

        overridden = plan_for_board(
            board_form, answers, tailor_dir, job_id="a1b2c3d4",
            overrides={field_id: ("An answer.", "C1")},
        )
        still_parked = {u.id: u for u in overridden.unmapped}
        assert field_id in still_parked
        assert "matches none of the options" in still_parked[field_id].reason

    def test_a_jd_override_supersedes_a_tier_b_rule(self, answers, tailor_dir):
        # "salary" matches the fixture's Tier B rule ("Open -- targeting
        # market rate"). A "JD" override -- the figure /apply read straight
        # off the JD -- must win over that static default.
        salary = field(id="salary_expectation", label="Salary Expectation",
                        kind="text", required=False)
        plan = build_plan(one([salary]), answers, tailor_dir)
        assert plan.fields[0].value == "Open — targeting market rate for the level."
        assert plan.fields[0].tier == "B"

        plan = build_plan(
            one([salary]), answers, tailor_dir,
            overrides={"salary_expectation": ("145000", "JD")},
        )
        assert plan.fields[0].value == "145000"
        assert plan.fields[0].tier == "JD"

    def test_a_jd_tagged_override_never_touches_a_non_tier_b_field(self, answers, tailor_dir):
        # "JD" is the one tag that can cross a tier boundary (into Tier B
        # only) -- it must not become a general escape hatch onto identity,
        # EEOC or work-authorization fields.
        plan = build_plan(
            one([field(id="first_name", label="First Name", kind="text")]),
            answers, tailor_dir,
            overrides={"first_name": ("Somebody Else", "JD")},
        )
        assert plan.fields[0].value != "Somebody Else"

    def test_a_c1_tagged_override_never_supersedes_a_tier_b_rule(self, answers, tailor_dir):
        # Only "JD" gets this power. A C1/C2 draft must stay Tier-C-only, so
        # it can never silently clobber a configured Tier B fact.
        salary = field(id="salary_expectation", label="Salary Expectation",
                        kind="text", required=False)
        plan = build_plan(
            one([salary]), answers, tailor_dir,
            overrides={"salary_expectation": ("145000", "C1")},
        )
        assert plan.fields[0].value == "Open — targeting market rate for the level."
        assert plan.fields[0].tier == "B"

    def test_an_override_is_canonicalized_to_the_boards_own_spelling(
        self, merged, answers, tailor_dir, scan, payload
    ):
        """`"no"` is the right answer spelled wrong. react-select matches on
        exact visible text, so planning the literal lowercase string would
        fail at fill time for no reason."""
        from src.apply.schema import parse_schema

        name = "form_minimal"
        board_form = BoardForm(
            posting=Posting(token="1", url_slug=None), slug="gasketworks", html="",
            scan=scan(name), schema=parse_schema(payload(name)), reconciled=merged(name),
        )
        field_id = next(u.id for u in
                        plan_for_board(board_form, answers, tailor_dir,
                                        job_id="a1b2c3d4").unmapped)

        overridden = plan_for_board(
            board_form, answers, tailor_dir, job_id="a1b2c3d4",
            overrides={field_id: ("no", "C1")},
        )
        assert [f.value for f in overridden.fields if f.id == field_id] == ["No"]


class TestEmploymentSwitch:
    """`employment.only_when_required` — a config switch, not a fixed policy.
    The block holds one role, which for anyone not currently employed is the
    most recent one rather than a current one."""

    def _answers(self, tmp_path, **employment):
        import yaml
        from src.apply.answers import load_answers
        from .conftest import FIXTURES
        data = yaml.safe_load(
            (FIXTURES / "application_answers.yaml").read_text(encoding="utf-8")
        )
        data["employment"] = {**data["employment"], **employment}
        path = tmp_path / "answers.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return load_answers(path, FIXTURES / "preferences_time_limited.md")

    def _block(self, required: bool):
        return one([
            field(id="company-name-0", name="company-name-0", label="Company Name",
                  section="employment", required=required),
            field(id="title-0", name="title-0", label="Title",
                  section="employment", required=required),
        ])

    def test_off_by_default_the_block_fills_wherever_it_renders(self, tmp_path, tailor_dir):
        a = self._answers(tmp_path)
        assert a.employment_only_when_required is False
        plan = build_plan(self._block(required=False), a, tailor_dir)
        assert {f.id for f in plan.fields} == {"company-name-0", "title-0"}

    def test_on_an_optional_block_is_left_empty(self, tmp_path, tailor_dir):
        a = self._answers(tmp_path, only_when_required=True)
        plan = build_plan(self._block(required=False), a, tailor_dir)
        assert plan.fields == ()
        assert {s.id for s in plan.skipped} == {"company-name-0", "title-0"}
        assert "only_when_required" in plan.skipped[0].reason

    def test_on_a_required_block_still_fills(self, tmp_path, tailor_dir):
        a = self._answers(tmp_path, only_when_required=True)
        plan = build_plan(self._block(required=True), a, tailor_dir)
        assert {f.id for f in plan.fields} == {"company-name-0", "title-0"}

    def test_the_whole_block_follows_the_required_ones(self, tmp_path, tailor_dir):
        # Filling company and title while leaving an optional end date blank
        # would read as "still works there".
        a = self._answers(tmp_path, only_when_required=True)
        mixed = one([
            field(id="company-name-0", name="company-name-0", label="Company",
                  section="employment", required=True),
            field(id="end-date-year-0", name="end-date-year-0", label="End year",
                  section="employment", required=False),
        ])
        plan = build_plan(mixed, a, tailor_dir)
        assert {f.id for f in plan.fields} == {"company-name-0", "end-date-year-0"}

    def test_the_switch_does_not_touch_the_education_block(self, tmp_path, tailor_dir):
        a = self._answers(tmp_path, only_when_required=True)
        plan = build_plan(one([
            field(id="school--0", name="school--0", label="School",
                  section="education", required=False),
        ]), a, tailor_dir)
        assert [f.id for f in plan.fields] == ["school--0"]

    def test_an_unknown_flag_is_still_rejected(self, tmp_path):
        from src.apply.answers import AnswersError
        with pytest.raises(AnswersError, match="unknown keys"):
            self._answers(tmp_path, only_when_requried=True)   # typo on purpose


class TestArtifacts:
    def _dir(self, tmp_path, *names) -> Path:
        out = tmp_path / "dir"
        out.mkdir()
        for n in names:
            (out / n).write_bytes(b"x")
        return out

    def test_pdf_wins_over_docx(self, tmp_path):
        out = self._dir(tmp_path, "A_B_Resume.pdf", "A_B_Resume.docx")
        path, reason = find_artifact(out, "resume")
        assert path.name == "A_B_Resume.pdf" and reason == ""

    def test_docx_is_the_fallback(self, tmp_path):
        # 4 of the 120 real dirs have a docx and no pdf.
        out = self._dir(tmp_path, "A_B_Resume.docx")
        assert find_artifact(out, "resume")[0].name == "A_B_Resume.docx"

    @pytest.mark.parametrize("name", [
        "Alex_Example_Resume.pdf",   # the usual spelling, 116 real dirs
        "Alex_Example_resume.pdf",   # 3 real dirs
        "Alex_example_resume.pdf",   # 1 real dir
        "resume.pdf",                # the legacy bare name, 12 real dirs
    ])
    def test_real_world_resume_spellings_all_match(self, tmp_path, name):
        assert find_artifact(self._dir(tmp_path, name), "resume")[0].name == name

    def test_word_lock_files_are_never_uploaded(self, tmp_path):
        # One real dir holds a ~$-prefixed lock file beside the document.
        out = self._dir(tmp_path, "~$A_B_Resume.docx", "A_B_Resume.docx")
        assert find_artifact(out, "resume")[0].name == "A_B_Resume.docx"

    def test_a_lock_file_alone_is_nothing_to_attach(self, tmp_path):
        out = self._dir(tmp_path, "~$A_B_Resume.docx")
        path, reason = find_artifact(out, "resume")
        assert path is None and "no pdf/docx" in reason

    def test_a_cover_letter_is_not_mistaken_for_a_resume(self, tmp_path):
        out = self._dir(tmp_path, "A_B_Cover_Letter.pdf")
        assert find_artifact(out, "resume")[0] is None
        assert find_artifact(out, "cover_letter")[0].name == "A_B_Cover_Letter.pdf"

    @pytest.mark.parametrize("name", ["A_B_Cover_Letter.pdf", "A_B_cover-letter.pdf",
                                      "cover letter.pdf"])
    def test_cover_letter_spellings(self, tmp_path, name):
        assert find_artifact(self._dir(tmp_path, name), "cover_letter")[0].name == name

    def test_two_candidates_are_refused_rather_than_guessed(self, tmp_path):
        out = self._dir(tmp_path, "A_B_Resume.pdf", "Old_Resume.pdf")
        path, reason = find_artifact(out, "resume")
        assert path is None
        assert "2 candidates" in reason and "cannot choose" in reason

    def test_ambiguity_at_pdf_does_not_fall_through_to_docx(self, tmp_path):
        # Two resumes is a question, not a reason to reach for a third file.
        out = self._dir(tmp_path, "A.resume.pdf", "B_resume.pdf", "C_Resume.docx")
        assert find_artifact(out, "resume")[0] is None

    def test_other_files_in_the_dir_are_ignored(self, tmp_path):
        out = self._dir(tmp_path, "resume.md", "trace.md", "A_B_Resume.pdf")
        assert find_artifact(out, "resume")[0].name == "A_B_Resume.pdf"

    def test_an_unknown_artifact_id_is_not_an_artifact(self, tmp_path):
        assert find_artifact(self._dir(tmp_path), "portfolio")[0] is None


class TestMissingArtifacts:
    def test_a_required_resume_with_nothing_to_attach_parks(self, answers, tmp_path):
        out = tmp_path / "empty"
        out.mkdir()
        plan = build_plan(
            one([field(id="resume", name="resume", label="Resume/CV",
                       kind="file", required=True)]),
            answers, out,
        )
        assert [u.id for u in plan.unmapped] == ["resume"]
        assert "no pdf/docx" in plan.unmapped[0].reason
        assert plan.files == ()

    def test_an_optional_cover_letter_with_nothing_to_attach_is_skipped(
        self, answers, tmp_path
    ):
        out = tmp_path / "empty"
        out.mkdir()
        plan = build_plan(
            one([field(id="cover_letter", name="cover_letter", label="Cover Letter",
                       kind="file", required=False)]),
            answers, out,
        )
        assert plan.unmapped == ()
        assert [s.id for s in plan.skipped] == ["cover_letter"]

    def test_a_required_cover_letter_with_nothing_to_attach_still_parks(
        self, answers, tmp_path
    ):
        # `eligible_queue()` no longer pre-filters on cover_letters[] -- this
        # is the mechanism that has to catch a genuinely-needed-but-missing
        # cover letter now, at plan/run time instead.
        out = tmp_path / "empty"
        out.mkdir()
        plan = build_plan(
            one([field(id="cover_letter", name="cover_letter", label="Cover Letter",
                       kind="file", required=True)]),
            answers, out,
        )
        assert [u.id for u in plan.unmapped] == ["cover_letter"]
        assert plan.unmapped[0].required is True
        assert plan.parked is True

    def test_a_board_that_never_asks_for_a_cover_letter_is_never_parked_on_one(
        self, answers, tmp_path
    ):
        # No cover_letter field at all, and no C-tier company/motivational
        # question either -- the "genuinely doesn't need one" case /apply's
        # Step 2b relies on to skip /cover-letter and self-promote.
        out = tmp_path / "empty"
        out.mkdir()
        (out / "Alex_Example_Resume.pdf").write_bytes(b"x")
        plan = build_plan(
            one([field(id="resume", name="resume", label="Resume/CV",
                       kind="file", required=True)]),
            answers, out,
        )
        assert plan.unmapped == ()
        assert plan.parked is False
        assert not any(u.id == "cover_letter" for u in plan.unmapped)

    def test_ambiguous_artifacts_park_a_required_input(self, answers, tmp_path):
        out = tmp_path / "two"
        out.mkdir()
        (out / "A_Resume.pdf").write_bytes(b"x")
        (out / "B_Resume.pdf").write_bytes(b"x")
        plan = build_plan(
            one([field(id="resume", name="resume", label="Resume/CV",
                       kind="file", required=True)]),
            answers, out,
        )
        assert "cannot choose" in plan.unmapped[0].reason

    def test_a_missing_out_dir_is_an_error_before_anything_else(self, answers, tmp_path):
        with pytest.raises(PlanError, match="not a directory"):
            build_plan(one([field()]), answers, tmp_path / "nope")

    def test_a_file_that_is_not_a_directory_is_refused(self, answers, tmp_path):
        f = tmp_path / "afile"
        f.write_bytes(b"x")
        with pytest.raises(PlanError, match="not a directory"):
            build_plan(one([field()]), answers, f)


class TestValueSanity:
    """answers.py handing plan.py something the widget cannot take is a bug, and
    a bug parked looks exactly like a question nothing could answer."""

    def _with_resolution(self, monkeypatch, resolution):
        monkeypatch.setattr(P, "resolve", lambda f, a: resolution)

    def test_a_list_into_a_single_select_raises(self, monkeypatch, answers, tailor_dir):
        self._with_resolution(monkeypatch, Resolution("fill", value=("A", "B"), tier="B"))
        with pytest.raises(PlanError, match="list value for single-valued"):
            build_plan(one([field(kind="react_select")]), answers, tailor_dir)

    def test_a_scalar_into_a_checkbox_group_raises(self, monkeypatch, answers, tailor_dir):
        self._with_resolution(monkeypatch, Resolution("fill", value="A", tier="B"))
        with pytest.raises(PlanError, match="scalar value for multi-valued"):
            build_plan(one([field(kind="checkbox_group", multi=True)]), answers, tailor_dir)

    @pytest.mark.parametrize("value", ["", "   "])
    def test_a_blank_value_raises(self, monkeypatch, answers, tailor_dir, value):
        self._with_resolution(monkeypatch, Resolution("fill", value=value, tier="A"))
        with pytest.raises(PlanError, match="blank value"):
            build_plan(one([field()]), answers, tailor_dir)

    def test_an_empty_option_list_raises(self, monkeypatch, answers, tailor_dir):
        self._with_resolution(monkeypatch, Resolution("fill", value=(), tier="B"))
        with pytest.raises(PlanError, match="empty list"):
            build_plan(one([field(kind="checkbox_group", multi=True)]), answers, tailor_dir)

    def test_a_blank_option_in_a_list_raises(self, monkeypatch, answers, tailor_dir):
        self._with_resolution(monkeypatch, Resolution("fill", value=("A", " "), tier="B"))
        with pytest.raises(PlanError, match="blank option"):
            build_plan(one([field(kind="checkbox_group", multi=True)]), answers, tailor_dir)

    def test_a_boolean_outside_a_checkbox_raises(self, monkeypatch, answers, tailor_dir):
        self._with_resolution(monkeypatch, Resolution("fill", value=True, tier="A"))
        with pytest.raises(PlanError, match="boolean value for a text"):
            build_plan(one([field()]), answers, tailor_dir)

    def test_a_checkbox_takes_a_boolean(self, monkeypatch, answers, tailor_dir):
        self._with_resolution(monkeypatch, Resolution("fill", value=False, tier="A"))
        plan = build_plan(one([field(id="current-role-0", kind="checkbox")]),
                          answers, tailor_dir)
        assert plan.fields[0].value is False

    def test_a_non_string_value_raises(self, monkeypatch, answers, tailor_dir):
        self._with_resolution(monkeypatch, Resolution("fill", value=2021, tier="A"))
        with pytest.raises(PlanError, match="value is int"):
            build_plan(one([field(kind="number")]), answers, tailor_dir)

    def test_an_unknown_action_raises(self, monkeypatch, answers, tailor_dir):
        self._with_resolution(monkeypatch, Resolution("maybe", value="x"))
        with pytest.raises(PlanError, match="unknown resolution action"):
            build_plan(one([field()]), answers, tailor_dir)

    def test_a_required_field_resolving_to_skip_raises(self, monkeypatch, answers, tailor_dir):
        # answers.py only skips optional fields; if that ever changes, a
        # required field would go out empty rather than parking the role.
        self._with_resolution(monkeypatch, Resolution("skip", reason="left alone"))
        with pytest.raises(PlanError, match="may only fill or park"):
            build_plan(one([field(required=True)]), answers, tailor_dir)


class TestAccountedForGuard:
    """The guard against a required field falling out between scan and plan."""

    def _plan(self, out_dir, **kw) -> Plan:
        base = dict(
            job_id="", board="", token="", form_url="", company="", title="",
            out_dir=out_dir, fields=(), files=(), unmapped=(), draftable=(), skipped=(),
            submit_selector=None, submit_disabled=False, api_only=(),
        )
        base.update(kw)
        return Plan(**base)

    def test_a_dropped_field_raises(self, tailor_dir):
        with pytest.raises(PlanError, match="vanished between scan and plan"):
            P._assert_accounted_for(one([field(id="a")]), self._plan(tailor_dir))

    def test_a_field_planned_twice_raises(self, tailor_dir):
        f = FieldPlan(id="a", name="a", label="", kind="text", section="questions",
                      required=False, multi=False, value="x", tier="A")
        s = Skipped(id="a", label="", tier="A", reason="")
        with pytest.raises(PlanError, match="more than once"):
            P._assert_accounted_for(one([field(id="a")]),
                                    self._plan(tailor_dir, fields=(f,), skipped=(s,)))

    def test_a_field_the_form_does_not_render_raises(self, tailor_dir):
        f = FieldPlan(id="ghost", name="ghost", label="", kind="text",
                      section="questions", required=False, multi=False,
                      value="x", tier="A")
        with pytest.raises(PlanError, match="does not render"):
            P._assert_accounted_for(one([]), self._plan(tailor_dir, fields=(f,)))

    def test_a_required_field_only_skipped_raises(self, tailor_dir):
        s = Skipped(id="a", label="", tier="C", reason="")
        with pytest.raises(PlanError, match="neither resolved nor parked"):
            P._assert_accounted_for(one([field(id="a", required=True)]),
                                    self._plan(tailor_dir, skipped=(s,)))


class TestPassThrough:
    def test_api_only_is_carried_for_diagnosis(self, merged, answers, tailor_dir):
        reconciled = merged("form_minimal")
        plan = build_plan(reconciled, answers, tailor_dir)
        assert plan.api_only == reconciled.api_only

    def test_submit_state_is_carried(self, merged, answers, tailor_dir):
        plan = build_plan(merged("form_minimal"), answers, tailor_dir,
                          submit_selector="#s", submit_disabled=True)
        assert (plan.submit_selector, plan.submit_disabled) == ("#s", True)

    def test_plan_for_board_carries_the_board_identifiers(
        self, merged, answers, tailor_dir, scan, payload
    ):
        from src.apply.schema import parse_schema

        name = "form_education"
        board = BoardForm(
            posting=Posting(token="1000003", url_slug=None),
            slug="bushinggroup",
            html="",
            scan=scan(name),
            schema=parse_schema(payload(name)),
            reconciled=merged(name),
        )
        plan = plan_for_board(board, answers, tailor_dir, job_id="a1b2c3d4")
        assert plan.job_id == "a1b2c3d4"
        assert plan.board == "bushinggroup"
        assert plan.token == "1000003"
        assert plan.form_url.endswith("token=1000003")
        assert plan.submit_selector == board.scan.submit_selector
        assert plan.company == board.schema.company_name
