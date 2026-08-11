"""The fill sequence, driven against a fake page.

What is worth testing offline is the ordering and the refusals, not Playwright:
attachments before fields, config overwriting whatever an upload parsed into the
form, and a react-select that did not stick counting as a failure rather than a
filled field. The selector work is what the live run at step 6 checks.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src import paths
from src.apply import fill as F
from src.apply.fill import FillError, FillResult, fill_plan
from src.apply.plan import FieldPlan, FilePlan, Plan

REPO_ROOT = paths.REPO_ROOT


def field(**kw) -> FieldPlan:
    base = dict(id="q1", name="q1", label="A question", kind="text",
                section="questions", required=False, multi=False, value="v", tier="B")
    base.update(kw)
    return FieldPlan(**base)


def plan(fields=(), files=(), form_url="https://boards.greenhouse.io/embed/job_app?token=1") -> Plan:
    return Plan(
        job_id="a1b2c3d4", board="gasketworks", token="1", form_url=form_url,
        company="Gasket Works", title="Widget Engineer", out_dir=Path("/tmp"),
        fields=tuple(fields), files=tuple(files), unmapped=(), draftable=(), skipped=(),
        submit_selector="#application-form button[type=submit]", submit_disabled=False,
    )


class FakeDriver:
    """A page that records what was done to it, in order."""

    def __init__(self, *, values=None, options=None, sticks=True, expanded_after=False,
                 kinds=None):
        self.calls: list[tuple] = []
        self.kinds = dict(kinds or {})         # id -> kind the DOM really shows
        self.values = dict(values or {})       # id -> what the field reads back
        self.options = dict(options or {})     # id -> what the listbox offers
        self.sticks = sticks                   # does a selection register
        self.expanded_after = expanded_after   # listbox left open after clicking
        self._selected: dict[str, str] = {}
        self._typed: str | None = None
        self._last_field: str | None = None
        self._files: dict[str, tuple[str, ...]] = {}
        self.uploads_stick = True              # does set_files actually attach
        self.upload_readback = True            # None = input gone, unreadable
        self.confirms = False                  # board acknowledges the submit

    def goto(self, url):
        self.calls.append(("goto", url))

    def resolve_kind(self, field_id, planned):
        """A board that renders what it declared. `kinds` overrides one field,
        the way Ashby's DOM read does."""
        return self.kinds.get(field_id, planned)

    def set_yesno(self, field_id, label):
        self.calls.append(("yesno", field_id, label))
        self._selected[field_id] = label

    def settle(self):
        self.calls.append(("settle",))

    def value_of(self, field_id):
        return self._selected.get(field_id, self.values.get(field_id, ""))

    def selected_label(self, field_id):
        return self._selected.get(field_id, "")

    def is_expanded(self, field_id):
        return self.expanded_after and field_id in self._selected

    def set_files(self, field_id, path):
        self.calls.append(("attach", field_id, Path(path).name))
        if self.uploads_stick:
            self._files[field_id] = (Path(path).name,)

    def attached_files(self, field_id):
        if self.upload_readback is None:
            return None                      # detached node: cannot verify
        return self._files.get(field_id, ())

    def fill_text(self, field_id, value):
        self.calls.append(("fill", field_id, value))
        self.values[field_id] = value

    def set_checkbox(self, field_id, checked):
        self.calls.append(("checkbox", field_id, checked))
        self.values[field_id] = str(checked)

    def check_group_option(self, field_id, label):
        self.calls.append(("check_group", field_id, label))

    def type_into(self, field_id, value):
        self.calls.append(("type", field_id, value))
        self._typed, self._last_field = value, field_id

    def open_options(self, field_id):
        self.calls.append(("open", field_id))
        self._last_field = field_id
        return tuple(self.options.get(field_id, ()))

    def visible_options(self):
        return tuple(self.options.get(self._last_field, ()))

    def click_option(self, label):
        offered = self.options.get(self._last_field)
        if offered is not None and label not in offered:
            return False
        if not self.sticks:
            return True
        self.calls.append(("select", self._last_field, label))
        self._selected[self._last_field] = label
        return True

    def close(self):
        self.calls.append(("close",))

    def select_native(self, field_id, label):
        self.calls.append(("select_native", field_id, label))
        offered = self.options.get(field_id)
        if offered is not None and label not in offered:
            return  # left unselected, same as a real <select> rejecting the label
        self._selected[field_id] = label

    def selected_option_label(self, field_id):
        return self._selected.get(field_id, "")

    def check_radio_group(self, field_id, label):
        offered = self.options.get(field_id)
        if offered is not None and label not in offered:
            raise FillError(f"{field_id}: no radio labelled {label!r}")
        self.calls.append(("check_radio", field_id, label))
        self._selected[field_id] = label

    def submit_disabled_now(self, selector):
        return False

    def click_submit(self, selector):
        self.calls.append(("click_submit", selector))

    def wait_for_captcha(self):
        self.calls.append(("wait_for_captcha",))

    def submission_confirmed(self):
        self.calls.append(("submission_confirmed",))
        return self.confirms


@pytest.fixture
def resume(tmp_path):
    p = tmp_path / "Alex_Example_Resume.pdf"
    p.write_bytes(b"%PDF-1.4")
    return p


class TestOrder:
    def test_attachments_go_first_then_the_page_settles(self, resume):
        # An upload can trigger a parse that writes into fields, so nothing may
        # be read or written before it has landed.
        d = FakeDriver()
        fill_plan(plan(
            fields=[field(id="first_name", value="Alex")],
            files=[FilePlan(id="resume", label="Resume/CV", required=True, path=resume)],
        ), d)
        kinds = [c[0] for c in d.calls]
        assert kinds.index("attach") < kinds.index("settle") < kinds.index("fill")

    def test_the_form_is_opened_before_anything_is_touched(self):
        d = FakeDriver()
        fill_plan(plan(fields=[field()]), d)
        assert d.calls[0][0] == "goto"

    def test_no_settle_when_there_is_nothing_to_attach(self):
        d = FakeDriver()
        fill_plan(plan(fields=[field()]), d)
        assert "settle" not in [c[0] for c in d.calls]

    def test_nothing_ever_clicks_submit(self, resume):
        d = FakeDriver()
        fill_plan(plan(
            fields=[field()],
            files=[FilePlan(id="resume", label="R", required=True, path=resume)],
        ), d)
        assert not any("submit" in str(c).lower() for c in d.calls)


class TestConfigWins:
    def test_a_prefilled_field_is_overwritten_not_left_alone(self):
        # This is the resume-parser case: the value is already there and wrong.
        d = FakeDriver(values={"first_name": "Parsed Name"})
        result = fill_plan(plan(fields=[field(id="first_name", value="Alex")]), d)
        assert ("fill", "first_name", "Alex") in d.calls
        assert d.values["first_name"] == "Alex"
        assert result.outcomes[0].before == "Parsed Name"
        assert result.outcomes[0].after == "Alex"

    def test_prefilled_fields_are_reported(self):
        d = FakeDriver(values={"first_name": "Parsed", "email": ""})
        result = fill_plan(plan(fields=[
            field(id="first_name", value="Alex"), field(id="email", value="a@b.c"),
        ]), d)
        assert result.prefilled == ("first_name",)

    def test_a_field_that_does_not_hold_what_was_written_fails(self):
        class Stubborn(FakeDriver):
            def fill_text(self, field_id, value):
                self.calls.append(("fill", field_id, value))  # writes nothing

        d = Stubborn()
        result = fill_plan(plan(fields=[field(id="phone", value="+1 555 0100")]), d)
        assert result.ok is False
        assert "phone" in result.failures[0]


class TestReactSelect:
    def test_a_selection_that_sticks_is_a_fill(self):
        d = FakeDriver(options={"country": ("United States", "Canada")})
        result = fill_plan(plan(fields=[
            field(id="country", kind="react_select", value="United States"),
        ]), d)
        assert result.ok
        assert ("select", "country", "United States") in d.calls
        assert result.outcomes[0].after == "United States"

    def test_typing_comes_before_selecting(self):
        d = FakeDriver(options={"country": ("United States",)})
        fill_plan(plan(fields=[
            field(id="country", kind="react_select", value="United States"),
        ]), d)
        kinds = [c[0] for c in d.calls]
        assert kinds.index("type") < kinds.index("select")

    def test_a_value_matching_no_option_fails_and_says_what_was_offered(self):
        d = FakeDriver(options={"degree--0": ("Bachelor's Degree", "Doctorate")})
        result = fill_plan(plan(fields=[
            field(id="degree--0", kind="react_select", value="Master's Degree"),
        ]), d)
        assert result.ok is False
        assert "no option matching" in result.failures[0]
        assert "Bachelor's Degree" in result.failures[0]

    def test_a_selection_that_silently_does_not_stick_fails(self):
        # The failure this whole assert exists for: the widget accepts the
        # keystrokes and ends up empty.
        d = FakeDriver(options={"country": ("United States",)}, sticks=False)
        result = fill_plan(plan(fields=[
            field(id="country", kind="react_select", value="United States"),
        ]), d)
        assert result.ok is False
        assert "is empty" in result.failures[0]

    def test_a_listbox_left_open_fails(self):
        d = FakeDriver(options={"country": ("United States",)}, expanded_after=True)
        result = fill_plan(plan(fields=[
            field(id="country", kind="react_select", value="United States"),
        ]), d)
        assert result.ok is False
        assert "still open" in result.failures[0]

    def test_options_read_off_the_widget_are_recorded(self):
        # For hispanic_ethnicity and every other select whose options exist
        # nowhere but the opened widget.
        d = FakeDriver(options={"country": ("United States", "Canada")})
        result = fill_plan(plan(fields=[
            field(id="country", kind="react_select", value="United States"),
        ]), d)
        assert result.observed_options["country"] == ("United States", "Canada")

    def test_a_select_with_no_option_list_still_asserts(self):
        # country on 32 of 45 boards: nothing to validate against beforehand.
        d = FakeDriver()
        result = fill_plan(plan(fields=[
            field(id="country", kind="react_select", value="United States"),
        ]), d)
        assert result.ok
        assert result.outcomes[0].after == "United States"


class TestParkedProbe:
    """A parked select is never filled, so its options would otherwise never be
    read — and that list exists nowhere else."""

    def _parked(self, **kw):
        from src.apply.plan import Unmapped
        base = dict(id="hispanic_ethnicity", label="Are you Hispanic/Latino?",
                    required=True, kind="react_select", section="eeoc",
                    tier="A2", reason="no option list", options=())
        base.update(kw)
        return Unmapped(**base)

    def _plan_with(self, parked, fields=()):
        p = plan(fields=fields)
        return Plan(**{**p.__dict__, "unmapped": (parked,)})

    def test_a_parked_react_select_has_its_options_read(self):
        d = FakeDriver(options={"hispanic_ethnicity": ("Yes", "No", "Decline")})
        result = fill_plan(self._plan_with(self._parked()), d)
        assert result.observed_options["hispanic_ethnicity"] == ("Yes", "No", "Decline")

    def test_the_probe_never_selects_anything(self):
        d = FakeDriver(options={"hispanic_ethnicity": ("Yes", "No")})
        fill_plan(self._plan_with(self._parked()), d)
        assert not any(c[0] == "select" for c in d.calls)
        assert not any(c[0] == "type" for c in d.calls)

    def test_a_parked_text_question_is_not_probed(self):
        d = FakeDriver(options={"question_9": ("never", "read")})
        result = fill_plan(
            self._plan_with(self._parked(id="question_9", kind="textarea")), d
        )
        assert result.observed_options == {}
        assert not any(c[0] == "open" for c in d.calls)

    def test_a_probe_that_raises_does_not_fail_the_run(self):
        class Exploding(FakeDriver):
            def open_options(self, field_id):
                raise RuntimeError("widget gone")

        d = Exploding()
        result = fill_plan(self._plan_with(self._parked()), d)
        assert result.ok

    def test_the_probe_does_not_overwrite_options_read_while_filling(self):
        d = FakeDriver(options={"country": ("United States +1",)})
        p = plan(fields=[field(id="country", kind="react_select", value="United States")])
        p = Plan(**{**p.__dict__, "unmapped": (self._parked(id="country"),)})
        result = fill_plan(p, d)
        assert result.observed_options["country"] == ("United States +1",)


class TestRecovery:
    """A park caused by a missing option list, not by an unanswerable question.

    Live, `hispanic_ethnicity` offers Yes / No / Decline To Self Identify — the
    Tier A2 opt-out already covers that; it just had nothing to match against
    until the widget was opened.
    """

    def _parked(self, **kw):
        from src.apply.plan import Unmapped
        base = dict(id="hispanic_ethnicity", label="Are you Hispanic/Latino?",
                    required=True, kind="react_select", section="eeoc",
                    tier="A2", reason="no option list to opt out against", options=())
        base.update(kw)
        return Unmapped(**base)

    def _plan_with(self, parked):
        return Plan(**{**plan().__dict__, "unmapped": (parked,)})

    def test_the_eeoc_opt_out_applies_once_the_options_are_readable(self, answers):
        d = FakeDriver(options={
            "hispanic_ethnicity": ("Yes", "No", "Decline To Self Identify"),
        })
        result = fill_plan(self._plan_with(self._parked()), d, answers)
        assert result.recovered == ["hispanic_ethnicity"]
        assert ("select", "hispanic_ethnicity", "Decline To Self Identify") in d.calls
        assert result.ok

    def test_the_recovered_outcome_says_so(self, answers):
        d = FakeDriver(options={"hispanic_ethnicity": ("Yes", "No", "Decline To Self Identify")})
        result = fill_plan(self._plan_with(self._parked()), d, answers)
        assert "recovered at fill time" in result.outcomes[-1].note

    def test_a_widget_with_no_opt_out_stays_parked(self, answers):
        d = FakeDriver(options={"hispanic_ethnicity": ("Yes", "No")})
        result = fill_plan(self._plan_with(self._parked()), d, answers)
        assert result.recovered == []
        assert not any(c[0] == "select" for c in d.calls)

    def test_a_tier_c_question_is_never_invented_an_answer(self, answers):
        # Nothing about reading options makes an unanswerable question answerable.
        d = FakeDriver(options={"question_9": ("Yes", "No")})
        result = fill_plan(self._plan_with(self._parked(
            id="question_9", section="questions", tier="C",
            label="What interests you about our product?",
            reason="no rule matches this question",
        )), d, answers)
        assert result.recovered == []

    def test_no_answers_means_no_recovery_only_a_read(self, answers):
        d = FakeDriver(options={"hispanic_ethnicity": ("Decline To Self Identify",)})
        result = fill_plan(self._plan_with(self._parked()), d, None)
        assert result.recovered == []
        assert result.observed_options["hispanic_ethnicity"]

    def test_a_recovered_field_that_will_not_take_the_value_is_not_claimed(self, answers):
        d = FakeDriver(options={"hispanic_ethnicity": ("Decline To Self Identify",)},
                       sticks=False)
        result = fill_plan(self._plan_with(self._parked()), d, answers)
        assert result.recovered == []


class TestRelabelRetry:
    """`country` is planned as "United States" and offered as "United States +1"
    on all 24 live boards that render it — it is the phone dial-code select."""

    def test_the_planned_string_is_re_resolved_against_the_real_options(self, answers):
        d = FakeDriver(options={"country": ("United States +1", "Canada +1")})
        result = fill_plan(plan(fields=[
            field(id="country", kind="react_select", value="United States"),
        ]), d, answers)
        assert result.ok
        assert ("select", "country", "United States +1") in d.calls

    def test_no_retry_without_the_answer_config(self):
        d = FakeDriver(options={"country": ("United States +1",)})
        result = fill_plan(plan(fields=[
            field(id="country", kind="react_select", value="United States"),
        ]), d, None)
        assert result.ok is False

    def test_a_genuinely_absent_option_still_fails(self, answers):
        d = FakeDriver(options={"country": ("Canada +1", "Mexico +52")})
        result = fill_plan(plan(fields=[
            field(id="country", kind="react_select", value="United States"),
        ]), d, answers)
        assert result.ok is False
        assert "no option matching" in result.failures[0]

    def test_the_retry_is_not_a_fuzzy_match(self, answers):
        # "United States" must not reach "United States Minor Outlying Islands".
        d = FakeDriver(options={
            "country": ("United States Minor Outlying Islands +246", "Canada +1"),
        })
        result = fill_plan(plan(fields=[
            field(id="country", kind="react_select", value="United States"),
        ]), d, answers)
        assert result.ok is False


class TestOtherWidgets:
    def test_a_checkbox_takes_a_boolean(self):
        d = FakeDriver()
        fill_plan(plan(fields=[
            field(id="current-role-0", kind="checkbox", value=False),
        ]), d)
        assert ("checkbox", "current-role-0", False) in d.calls

    def test_a_checkbox_group_ticks_every_label(self):
        d = FakeDriver()
        fill_plan(plan(fields=[
            field(id="question_1[]", kind="checkbox_group", multi=True,
                  value=("Alpha", "Beta")),
        ]), d)
        assert ("check_group", "question_1[]", "Alpha") in d.calls
        assert ("check_group", "question_1[]", "Beta") in d.calls


class TestNativeSelect:
    """Lever's `<select>` — no react-select, no listbox, no typing (§12a)."""

    def test_selecting_a_real_option_is_a_fill(self):
        d = FakeDriver(options={"eeo[gender]": ("Male", "Female")})
        result = fill_plan(plan(fields=[
            field(id="eeo[gender]", kind="select", value="Female"),
        ]), d)
        assert result.ok
        assert ("select_native", "eeo[gender]", "Female") in d.calls
        assert result.outcomes[0].after == "Female"

    def test_a_value_the_select_does_not_offer_fails(self):
        d = FakeDriver(options={"eeo[gender]": ("Male", "Female")})
        result = fill_plan(plan(fields=[
            field(id="eeo[gender]", kind="select", value="Nonbinary"),
        ]), d)
        assert result.ok is False
        assert "eeo[gender]" in result.failures[0]

    def test_never_typed_into_unlike_a_react_select(self):
        d = FakeDriver(options={"eeo[gender]": ("Male", "Female")})
        fill_plan(plan(fields=[field(id="eeo[gender]", kind="select", value="Female")]), d)
        assert not any(c[0] == "type" for c in d.calls)


class TestRadioGroup:
    def test_checking_a_real_option_is_a_fill(self):
        d = FakeDriver(options={"eeo[race]": ("Decline to self-identify", "Asian")})
        result = fill_plan(plan(fields=[
            field(id="eeo[race]", kind="radio_group", value="Decline to self-identify"),
        ]), d)
        assert result.ok
        assert ("check_radio", "eeo[race]", "Decline to self-identify") in d.calls

    def test_no_matching_radio_fails(self):
        d = FakeDriver(options={"eeo[race]": ("Asian",)})
        result = fill_plan(plan(fields=[
            field(id="eeo[race]", kind="radio_group", value="Decline to self-identify"),
        ]), d)
        assert result.ok is False
        assert "eeo[race]" in result.failures[0]


class TestLeverBrowserDriverSelectorBoundary:
    def test_locates_by_name_attribute_not_id(self):
        calls = []

        class FakePage:
            def locator(self, selector):
                calls.append(selector)
                class L:
                    def click(self_inner):
                        pass
                return L()

        driver = F.LeverBrowserDriver(FakePage())
        driver._locator("name")
        assert calls == ['#application-form [name="name"]']


class TestCaptchaWait:
    def test_submit_waits_for_captcha_when_the_plan_requires_it(self):
        d = FakeDriver()
        p = plan(fields=[])
        from dataclasses import replace
        captcha_plan = replace(p, requires_captcha=True)
        F.submit(captcha_plan, FillResult(form_url="x"), d)
        assert ("click_submit", captcha_plan.submit_selector) in d.calls
        assert ("wait_for_captcha",) in d.calls

    def test_submit_does_not_wait_when_the_plan_does_not_require_it(self):
        d = FakeDriver()
        F.submit(plan(fields=[]), FillResult(form_url="x"), d)
        assert ("wait_for_captcha",) not in d.calls


class TestAttachments:
    def test_a_missing_file_fails_rather_than_uploading_nothing(self, tmp_path):
        d = FakeDriver()
        result = fill_plan(plan(files=[
            FilePlan(id="resume", label="R", required=True, path=tmp_path / "gone.pdf"),
        ]), d)
        assert result.ok is False
        assert "is gone" in result.failures[0]
        assert not any(c[0] == "attach" for c in d.calls)

    def test_the_attachment_is_recorded_by_name(self, resume):
        d = FakeDriver()
        result = fill_plan(plan(files=[
            FilePlan(id="resume", label="R", required=True, path=resume),
        ]), d)
        assert result.outcomes[0].action == "attached"
        assert result.outcomes[0].after == resume.name


class TestFailureIsolation:
    def test_one_bad_field_does_not_stop_the_rest(self):
        d = FakeDriver(options={"country": ("Canada",)})
        result = fill_plan(plan(fields=[
            field(id="country", kind="react_select", value="United States"),
            field(id="email", value="a@b.c"),
        ]), d)
        assert len(result.failures) == 1
        assert ("fill", "email", "a@b.c") in d.calls
        assert [o.action for o in result.outcomes] == ["failed", "filled"]


class TestDriverGuards:
    def test_no_module_but_fill_names_the_driver(self):
        """The patchright swap point (§9): if the driver is *imported* anywhere
        else under src/, swapping it stops being a one-line change.

        Walks the AST rather than grepping the source. A substring scan both
        false-fires on the word appearing in a comment and would miss an
        aliased import; and it was cwd-relative, so from any directory other
        than the repo root it scanned zero files and passed vacuously.
        """
        offenders = []
        for path in sorted((REPO_ROOT / "src").rglob("*.py")):
            if path.name == "fill.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                if any(n.split(".")[0] in ("playwright", "patchright") for n in names):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
        assert offenders == []

    def test_the_guard_would_actually_catch_an_offender(self, tmp_path):
        """The guard above is only worth having if it fires. A scan that
        passes vacuously looks identical to a clean tree."""
        mod = tmp_path / "rogue.py"
        mod.write_text("from playwright.sync_api import sync_playwright\n", encoding="utf-8")
        found = [
            n for n in ast.walk(ast.parse(mod.read_text(encoding="utf-8")))
            if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("playwright")
        ]
        assert found, "the AST predicate must match a real offending import"

    def test_the_import_guard_explains_how_to_install(self, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def no_playwright(name, *args, **kwargs):
            if name.startswith("playwright"):
                raise ImportError("nope")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_playwright)
        with pytest.raises(SystemExit, match="uv sync --group apply"):
            F._require_playwright()

    def test_the_profile_dir_is_not_the_users_chrome(self):
        assert F.USER_DATA_DIR.name == ".apply_profile"
        assert "Application Support" not in str(F.USER_DATA_DIR)


class TestAnUploadIsReadBack:
    """The file input was the one write with no read-back (§9 step 4).

    An input that silently ends up empty leaves `failures` empty, so
    `blocking_questions` is empty, the submit guard passes, and the
    application goes out with no resume attached.
    """

    def test_an_upload_that_does_not_stick_is_a_failure_not_a_success(self, resume):
        d = FakeDriver()
        d.uploads_stick = False
        with pytest.raises(FillError, match="holds no file"):
            F._attach(d, FilePlan(id="resume", label="Resume/CV", required=True, path=resume))

    def test_an_upload_holding_a_different_file_is_a_failure(self, resume):
        d = FakeDriver()

        def wrong(field_id, path):
            d.calls.append(("attach", field_id, Path(path).name))
            d._files[field_id] = ("SomeoneElses_Resume.pdf",)

        d.set_files = wrong
        with pytest.raises(FillError, match="SomeoneElses_Resume.pdf"):
            F._attach(d, FilePlan(id="resume", label="Resume/CV", required=True, path=resume))

    def test_a_normal_upload_still_reports_attached(self, resume):
        d = FakeDriver()
        out = F._attach(d, FilePlan(id="resume", label="Resume/CV", required=True, path=resume))
        assert out.action == "attached"
        assert out.after == resume.name


class TestTheDriverSetAndTheShortlistAgree:
    def test_submittable_ats_matches_the_drivers_that_exist(self):
        """`shortlist.py` marks a role manual-apply from
        `detect.SUBMITTABLE_ATS`; `apply run` refuses it from
        `fill._DRIVER_NAMES`. If those drift, the shortlist promises a
        submission the queue will not make — or hides one it would."""
        from src.apply.detect import SUBMITTABLE_ATS
        assert set(F._DRIVER_NAMES) == set(SUBMITTABLE_ATS)


class TestUploadVerificationIsConfirmatoryNotPunitive:
    """`None` (could not read the input back) and `()` (read it, it is empty)
    mean different things.

    Verified on a live Greenhouse board: uploading to `cover_letter` detaches
    the input — React replaces the subtree — so the node is unreadable a
    moment later even though the file landed. An earlier version of this
    check treated that as a failed upload, which put a spurious entry in
    `failures` and would have blocked a perfectly good submission.
    """

    def test_an_unreadable_input_is_attached_but_flagged_unverified(self, resume):
        d = FakeDriver()
        d.upload_readback = None                     # node gone after upload
        out = F._attach(d, FilePlan(id="cover_letter", label="Cover Letter",
                                     required=False, path=resume))
        assert out.action == "attached"
        assert "unverified" in out.note

    def test_an_unreadable_input_does_not_block_submission(self, resume):
        d = FakeDriver()
        d.upload_readback = None
        result = fill_plan(plan(files=[FilePlan(
            id="cover_letter", label="Cover Letter", required=True, path=resume)]), d)
        assert result.failures == []

    def test_a_readable_but_empty_input_is_still_a_failure(self, resume):
        # The real bug this check exists for: the upload silently did nothing.
        d = FakeDriver()
        d.uploads_stick = False                      # readable, and empty
        with pytest.raises(FillError, match="holds no file"):
            F._attach(d, FilePlan(id="resume", label="Resume/CV",
                                   required=True, path=resume))
