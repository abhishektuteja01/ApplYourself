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
from dataclasses import replace

from src.apply.plan import FieldPlan, FilePlan, Plan

REPO_ROOT = paths.REPO_ROOT


def field(**kw) -> FieldPlan:
    base = dict(id="q1", name="q1", label="A question", kind="text",
                section="questions", required=False, multi=False, value="v", tier="B")
    base.update(kw)
    return FieldPlan(**base)


def plan(fields=(), files=(), form_url="https://boards.greenhouse.io/embed/job_app?token=1") -> Plan:
    # `ats` defaults to a board with no measured request marker — most tests
    # here are about ordering/guard behaviour, not the marker feature, and
    # must not silently start asserting on whichever real board picks one up
    # next. Tests that care use `plan_ashby()` or set `ats` explicitly.
    return Plan(
        job_id="a1b2c3d4", board="gasketworks", token="1", form_url=form_url,
        company="Gasket Works", title="Widget Engineer", out_dir=Path("/tmp"),
        fields=tuple(fields), files=tuple(files), unmapped=(), draftable=(), skipped=(),
        submit_selector="#application-form button[type=submit]", submit_disabled=False,
        ats="lever",
    )


def plan_ashby(**kw) -> Plan:
    """A plan on a board whose submit request we know how to watch."""
    return replace(plan(**kw), ats="ashby")


class FakePage:
    """Just enough page for the post-submit capture to read."""

    def __init__(self, text="Thank you for applying to Gasket Works.",
                 url="https://boards.greenhouse.io/confirmation"):
        self._text, self.url = text, url

    def title(self):
        return "Application submitted"

    def locator(self, selector):
        return self

    def inner_text(self, timeout=None):
        return self._text

    def screenshot(self, path=None, full_page=False):
        Path(path).write_bytes(b"\x89PNG")


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
        self._checked_groups: dict[str, set[str]] = {}
        self._typed: str | None = None
        self._last_field: str | None = None
        self._files: dict[str, tuple[str, ...]] = {}
        self.uploads_stick = True              # does set_files actually attach
        self.upload_readback = True            # None = input gone, unreadable
        self.upload_widget = None              # what the widget shows: T/F/None
        self.confirms = False                  # board acknowledges the submit
        self._page = FakePage()                # what the post-submit capture reads
        self.refusals: list[str] = []          # the board's refusal, per click
        self.named_missing: list[list[str]] = []  # labels named missing, per refusal

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

    def upload_shows(self, field_id, filename):
        """The widget's own verdict. None = no widget to read, which is every
        pre-existing test and keeps them on the `attached_files` path."""
        return self.upload_widget

    def fill_text(self, field_id, value):
        self.calls.append(("fill", field_id, value))
        self.values[field_id] = value

    def set_checkbox(self, field_id, checked):
        self.calls.append(("checkbox", field_id, checked))
        self.values[field_id] = str(checked)

    def check_group_option(self, field_id, label):
        self.calls.append(("check_group", field_id, label))
        self._checked_groups.setdefault(field_id, set()).add(label)

    def checked_group_labels(self, field_id):
        return tuple(self._checked_groups.get(field_id, ()))

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

    def checked_radio_label(self, field_id):
        return self._selected.get(field_id, "")

    def submit_disabled_now(self, selector):
        return False

    def click_submit(self, selector):
        self.calls.append(("click_submit", selector))

    def wait_for_captcha(self):
        self.calls.append(("wait_for_captcha",))

    def submission_confirmed(self):
        self.calls.append(("submission_confirmed",))
        return self.confirms

    def watch_submit_requests(self, ats, sink):
        """No board markers in these fakes, so nothing is recorded and
        confirmation falls back to the page text."""

    def wait(self, ms):
        self.calls.append(("wait", ms))

    def submission_refused(self):
        self.calls.append(("submission_refused",))
        # A list, so a test can refuse the first click and accept the retry.
        return self.refusals.pop(0) if self.refusals else ""

    def missing_field_labels(self):
        self.calls.append(("missing_field_labels",))
        # A list, so a test can name fields on one refusal and none on another.
        return tuple(self.named_missing.pop(0)) if self.named_missing else ()

    @property
    def page(self):
        """The post-submit capture reads the page directly. `None` stands in
        for a driver that cannot be read, which is the swallowed path."""
        return self._page


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
        attach = kinds.index("attach")
        after = [i for i, k in enumerate(kinds) if k == "settle" and i > attach]
        assert after, "the upload must be allowed to land"
        assert after[0] < kinds.index("fill")

    def test_the_page_settles_before_the_first_upload_too(self, resume):
        # The upload widget is wired by script that lands after the form
        # element does. Clicking Attach too early is refused outright.
        d = FakeDriver()
        fill_plan(plan(
            files=[FilePlan(id="resume", label="Resume/CV", required=True, path=resume)],
        ), d)
        kinds = [c[0] for c in d.calls]
        assert kinds.index("settle") < kinds.index("attach")

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


class TestFieldsArePacedNotBurst:
    """b9a009ad: every field read back correct right after `fill_plan`, but a
    different subset came back missing on the board's own validation each
    live run — fixed live by pacing Ashby's fields a second apart, giving
    whatever client-side debounce reads that state room to settle between
    writes. Applied to every board: nothing about a client-side debounce is
    Ashby-specific, and Greenhouse/Lever fields are cheap to pace even though
    neither has shown this failure.
    """

    def test_ashby_waits_between_fields(self):
        d = FakeDriver()
        fill_plan(plan_ashby(fields=[field(id="q1"), field(id="q2")]), d)
        kinds = [c[0] for c in d.calls]
        assert kinds.count("wait") == 2
        assert ("wait", F.FIELD_PACE_MS) in d.calls

    def test_greenhouse_and_lever_are_paced_too(self):
        d = FakeDriver()
        fill_plan(plan(fields=[field(id="q1"), field(id="q2")]), d)
        kinds = [c[0] for c in d.calls]
        assert kinds.count("wait") == 2
        assert ("wait", F.FIELD_PACE_MS) in d.calls


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
        """The driver swap point (§9): if the driver is *imported* anywhere
        else under src/, swapping it stops being a one-line change.

        `browser.py` is the one place the driver is named now — `fill.py` and
        `ashby.py` both import `require_playwright`/`launch` from there rather
        than naming `playwright` themselves, which is exactly what keeps this
        guard meaningful for both.

        Walks the AST rather than grepping the source. A substring scan both
        false-fires on the word appearing in a comment and would miss an
        aliased import; and it was cwd-relative, so from any directory other
        than the repo root it scanned zero files and passed vacuously.
        """
        offenders = []
        for path in sorted((REPO_ROOT / "src").rglob("*.py")):
            if path.name in ("fill.py", "browser.py"):
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                if any(n.split(".")[0] == "playwright" for n in names):
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


class TestTheWidgetOutranksTheInput:
    """Live on a Greenhouse board, `set_input_files` on the visually-hidden
    input left the page rendering "Cannot read properties of undefined (reading
    'uploadFile')" while `input.files` read back correct. `_attach` reported
    `attached` with no note and `failures` stayed empty, so the submit guard
    would have passed and sent an application with no resume on it.
    """

    def test_a_rendered_upload_error_fails_even_when_the_input_reads_back(
            self, resume):
        d = FakeDriver()
        d.uploads_stick = True                       # input holds the file...
        d.upload_widget = False                      # ...but the widget errored
        with pytest.raises(FillError, match="rejected"):
            F._attach(d, FilePlan(id="resume", label="Resume/CV",
                                   required=True, path=resume))

    def test_that_error_blocks_the_submission(self, resume):
        d = FakeDriver()
        d.upload_widget = False
        result = fill_plan(plan(files=[FilePlan(
            id="resume", label="Resume/CV", required=True, path=resume)]), d)
        assert result.failures, "a refused resume must block the submit guard"

    def test_a_widget_showing_the_filename_is_attached_even_if_the_input_is_gone(
            self, resume):
        d = FakeDriver()
        d.upload_readback = None                     # input torn out by React
        d.upload_widget = True                       # but the name is on screen
        out = F._attach(d, FilePlan(id="resume", label="Resume/CV",
                                     required=True, path=resume))
        assert out.action == "attached"
        assert out.note == "", "a visible filename is verification, not a caveat"

    def test_an_unreadable_widget_falls_back_to_the_input(self, resume):
        # None means "no widget to read" — every board but Greenhouse today.
        d = FakeDriver()
        d.upload_widget = None
        d.uploads_stick = False
        with pytest.raises(FillError, match="holds no file"):
            F._attach(d, FilePlan(id="resume", label="Resume/CV",
                                   required=True, path=resume))


class TestThePostSubmitCapture:
    """`CONFIRMATION_MARKERS` was written before any confirmation page had been
    seen, and the first real submission matched none of it. The page that would
    have settled it was on screen and closed unrecorded. This keeps it.
    """

    def test_it_records_what_the_board_rendered(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = FakeDriver()
        out = F.capture_submit_evidence(d, plan())
        assert out is not None and out.exists()
        body = out.read_text(encoding="utf-8")
        assert "Thank you for applying" in body, "the rendered text is the point"
        assert "https://boards.greenhouse.io/confirmation" in body
        assert "a1b2c3d4" in body

    def test_it_saves_a_screenshot_beside_the_text(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        out = F.capture_submit_evidence(FakeDriver(), plan())
        assert out.with_suffix(".png").exists()

    def test_a_capture_failure_never_fails_the_submission(self, tmp_path,
                                                          monkeypatch):
        # Diagnostics must not turn an application that went out into an error.
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = FakeDriver()
        d._page = None                               # unreadable driver
        assert F.capture_submit_evidence(d, plan()) is None

    def test_the_submit_path_hangs_the_capture_off_the_result(self, tmp_path,
                                                              monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = FakeDriver()
        result = FillResult(form_url="x")
        F.submit(plan(), result, d)
        assert result.submitted
        assert result.evidence is not None and result.evidence.exists()


class TestAshbyFindsItsFileFields:
    """`ashby.py` aliases a resume/cover-letter field's real DOM path to the
    canonical `resume`/`cover_letter` id so the answer rules match it, and
    carries the real path through as `FilePlan.name`. `_attach` looks the
    field up by `.name`, not `.id`, so the driver never has to undo the
    alias itself — otherwise every Ashby upload waits out the full locator
    timeout on a field that is on the page. Live on Andera's board, and on
    Nen, where the real path is not the usual `_systemfield_resume`.
    """

    def test_the_resume_upload_is_looked_up_by_its_real_dom_path(self):
        page = _RecordingPage()
        F.AshbyBrowserDriver(page)._entry("_systemfield_resume")
        assert "_systemfield_resume" in page.selectors[0]

    def test_an_unaliased_field_is_looked_up_verbatim(self):
        # Employer-authored questions are UUIDs and must pass through.
        page = _RecordingPage()
        F.AshbyBrowserDriver(page)._entry("f81a7cc9-0d9a-4922-800b")
        assert "f81a7cc9-0d9a-4922-800b" in page.selectors[0]

    def test_attach_uses_name_not_id_for_the_dom_lookup(self, resume):
        # Nen's board: `id` is the canonical alias `resume`, but the real
        # DOM path is an employer-authored UUID, not `_systemfield_resume`.
        d = FakeDriver()
        upload = FilePlan(
            id="resume", name="f81a7cc9-0d9a-4922-800b", label="Resume/CV",
            required=True, path=resume,
        )
        F._attach(d, upload)
        assert d.calls[0] == ("attach", "f81a7cc9-0d9a-4922-800b", resume.name)


class _ClickPage:
    """Records what was done to the submit button and doubles as its own
    `keyboard`, since only the event sequence matters here."""

    def __init__(self):
        self.events: list[tuple] = []
        self.keyboard = self

    def locator(self, selector):
        self.events.append(("locate", selector))
        return self

    @property
    def first(self):
        return self

    def scroll_into_view_if_needed(self):
        self.events.append(("scroll",))

    def click(self):
        self.events.append(("click",))

    def focus(self):
        self.events.append(("focus",))

    def press(self, key):
        self.events.append(("press", key))


class TestToggleFieldsAreReverifiedBeforeTheClick:
    """Live on Take2: a yes/no toggle read back correctly right when
    `_apply_field` set it, then came back cleared by the time of the actual
    submit click — a different field each retry, which pointed at something
    external (the board's own "Autofill from resume" feature) resetting a
    toggle after the fill already finished. `submit()` re-checks every
    yes/no, radio-group, checkbox-group, select and text field immediately
    before the click and re-applies any that drifted, whatever caused it.
    Checkbox groups aren't known to drift the same way — this is precaution,
    not a repro.

    Live on b9a009ad (Ashby): a `select`-planned field Ashby rendered as a
    radio group, and a plain text field, both drifted and were never caught —
    the reverify only ever compared against `field.kind` as planned, which a
    `select`-planned field never equals `"radio_group"`, and text fields were
    not reverified at all.
    """

    def test_a_drifted_toggle_is_reapplied_before_the_click(self, tmp_path,
                                                             monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = FakeDriver()
        d._selected["q1"] = "No"  # drifted since fill_plan set it to "Yes"
        p = plan(fields=[field(id="q1", kind="yesno", value="Yes")])
        result = FillResult(form_url="x")
        F.submit(p, result, d)
        assert d._selected["q1"] == "Yes"
        assert ("yesno", "q1", "Yes") in d.calls

    def test_an_already_correct_toggle_is_left_alone(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = FakeDriver()
        d._selected["q1"] = "Yes"
        p = plan(fields=[field(id="q1", kind="yesno", value="Yes")])
        result = FillResult(form_url="x")
        F.submit(p, result, d)
        assert ("yesno", "q1", "Yes") not in d.calls

    def test_a_drifted_radio_group_is_reapplied_before_the_click(self, tmp_path,
                                                                   monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = FakeDriver()
        d._selected["q1"] = "No"  # drifted since fill_plan set it to "Yes"
        p = plan(fields=[field(id="q1", kind="radio_group", value="Yes")])
        result = FillResult(form_url="x")
        F.submit(p, result, d)
        assert d._selected["q1"] == "Yes"
        assert ("check_radio", "q1", "Yes") in d.calls

    def test_an_already_correct_radio_group_is_left_alone(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = FakeDriver()
        d._selected["q1"] = "Yes"
        p = plan(fields=[field(id="q1", kind="radio_group", value="Yes")])
        result = FillResult(form_url="x")
        F.submit(p, result, d)
        assert ("check_radio", "q1", "Yes") not in d.calls

    def test_a_dropped_checkbox_group_option_is_reapplied_before_the_click(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = FakeDriver()
        d._checked_groups["q1"] = {"Python"}  # "Go" drifted out since fill_plan
        p = plan(fields=[field(id="q1", kind="checkbox_group", value=("Python", "Go"))])
        result = FillResult(form_url="x")
        F.submit(p, result, d)
        assert d.checked_group_labels("q1") == ("Python", "Go") or set(
            d.checked_group_labels("q1")) == {"Python", "Go"}
        assert ("check_group", "q1", "Go") in d.calls
        assert ("check_group", "q1", "Python") not in d.calls

    def test_an_already_correct_checkbox_group_is_left_alone(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = FakeDriver()
        d._checked_groups["q1"] = {"Python", "Go"}
        p = plan(fields=[field(id="q1", kind="checkbox_group", value=("Python", "Go"))])
        result = FillResult(form_url="x")
        F.submit(p, result, d)
        assert ("check_group", "q1", "Python") not in d.calls
        assert ("check_group", "q1", "Go") not in d.calls

    def test_a_drifted_text_field_is_refilled_before_the_click(self, tmp_path,
                                                                monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = FakeDriver()
        d.values["q1"] = "https://linkedin.com/in/wrong"  # drifted after fill_plan
        p = plan(fields=[field(id="q1", kind="text",
                               value="https://linkedin.com/in/right")])
        result = FillResult(form_url="x")
        F.submit(p, result, d)
        assert d.values["q1"] == "https://linkedin.com/in/right"
        assert ("fill", "q1", "https://linkedin.com/in/right") in d.calls

    def test_an_already_correct_text_field_is_left_alone(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = FakeDriver()
        d.values["q1"] = "https://linkedin.com/in/right"
        p = plan(fields=[field(id="q1", kind="text",
                               value="https://linkedin.com/in/right")])
        result = FillResult(form_url="x")
        F.submit(p, result, d)
        assert ("fill", "q1", "https://linkedin.com/in/right") not in d.calls

    def test_a_select_planned_field_ashby_rendered_as_radio_group_is_reverified(
            self, tmp_path, monkeypatch):
        # b9a009ad: `field.kind` is the API-declared "select", but Ashby
        # rendered (and filled) it as a live radio group — the reverify must
        # follow the live kind, not the planned one, or it silently skips
        # exactly this field.
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = FakeDriver(kinds={"q1": "radio_group"})
        d._selected["q1"] = "No experience with AI agents"  # drifted
        p = plan(fields=[field(id="q1", kind="select", value="I have built agents")])
        result = FillResult(form_url="x")
        F.submit(p, result, d)
        assert d._selected["q1"] == "I have built agents"
        assert ("check_radio", "q1", "I have built agents") in d.calls

    def test_a_drifted_native_select_is_reapplied_before_the_click(self, tmp_path,
                                                                    monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = FakeDriver(options={"q1": ("Yes", "No")})
        d._selected["q1"] = "No"
        p = plan(fields=[field(id="q1", kind="select", value="Yes")])
        result = FillResult(form_url="x")
        F.submit(p, result, d)
        assert d._selected["q1"] == "Yes"
        assert ("select_native", "q1", "Yes") in d.calls

    def test_reverify_runs_again_on_every_retry_not_just_once(self, tmp_path,
                                                                monkeypatch):
        # A single reverify call before the retry loop only catches a drift
        # that happens once. `_reverify_fields` must run right before *every*
        # click, including the retry, or a re-drift between attempts (e.g.
        # during the `settle()` wait each attempt does) goes uncaught again.
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)

        class RedriftingDriver(FakeDriver):
            def click_submit(self, selector):
                super().click_submit(selector)
                if len([c for c in self.calls if c[0] == "click_submit"]) == 1:
                    self._selected["q1"] = "No"  # drifts again after the 1st click

        d = RedriftingDriver()
        d._selected["q1"] = "No"
        d.refusals = ["updating your application"]  # forces a retry
        p = plan(fields=[field(id="q1", kind="yesno", value="Yes")])
        result = FillResult(form_url="x")
        F.submit(p, result, d)
        assert [c for c in d.calls if c[0] == "click_submit"].__len__() == 2
        assert [c for c in d.calls if c == ("yesno", "q1", "Yes")].__len__() == 2
        assert d._selected["q1"] == "Yes"


class TestAshbySubmitClick:
    """A plain Playwright click misses Ashby's React `onClick` handler often
    enough that a first attempt firing no request is the normal case, not a
    bug — see `AshbyBrowserDriver.click_submit`. The retry has to reach a
    different code path, not just click the same button again.
    """

    def test_first_attempt_is_a_scrolled_plain_click(self):
        page = _ClickPage()
        F.AshbyBrowserDriver(page).click_submit("#submit")
        assert ("scroll",) in page.events
        assert ("click",) in page.events
        assert not any(e[0] in ("focus", "press") for e in page.events)

    def test_retry_escalates_to_focus_and_a_real_enter(self):
        page = _ClickPage()
        driver = F.AshbyBrowserDriver(page)
        driver.click_submit("#submit")
        page.events.clear()
        driver.click_submit("#submit")
        assert ("focus",) in page.events
        assert ("press", "Enter") in page.events
        assert not any(e[0] == "click" for e in page.events)


class _RecordingPage:
    """Records the selectors asked for, and nothing else."""

    def __init__(self):
        self.selectors: list[str] = []

    def locator(self, selector):
        self.selectors.append(selector)
        return self

    @property
    def first(self):
        return self


class TestARefusedClickIsNotASubmission:
    """Live on Ashby: the click was refused because the resume upload was still
    in flight ("We're updating your application ... please try again when
    they're finished"), and the form stayed on screen. `submitted` was set the
    instant the click returned, so the role transitioned to `applied` and would
    never have been applied to again. Nothing was sent.
    """

    def test_an_explicit_refusal_leaves_submitted_false(self, tmp_path,
                                                        monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = FakeDriver()
        d.refusals = ["updating your application"] * F.SUBMIT_ATTEMPTS
        result = FillResult(form_url="x")
        F.submit(plan(), result, d)
        assert result.submitted is False
        assert "refused" in result.submit_error

    def test_it_retries_because_a_refusal_proves_nothing_was_sent(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = FakeDriver()
        d.refusals = ["updating your application"]   # first click only
        result = FillResult(form_url="x")
        F.submit(plan(), result, d)
        assert result.submitted is True, "the retry went through"
        assert [c for c in d.calls if c[0] == "click_submit"].__len__() == 2

    def test_a_quiet_board_is_still_treated_as_submitted(self, tmp_path,
                                                          monkeypatch):
        # The default must not invert: absence of a refusal means it went out,
        # or every unrecognised confirmation page becomes a duplicate.
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = FakeDriver()
        result = FillResult(form_url="x")
        F.submit(plan(), result, d)
        assert result.submitted is True
        assert [c for c in d.calls if c[0] == "click_submit"].__len__() == 1

    def test_the_page_is_given_time_before_it_is_judged(self, tmp_path,
                                                        monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = FakeDriver()
        F.submit(plan(), FillResult(form_url="x"), d)
        kinds = [c[0] for c in d.calls]
        assert kinds.index("click_submit") < kinds.index("wait")
        assert kinds.index("wait") < kinds.index("submission_confirmed")

    def test_uploads_are_allowed_to_finish_before_the_click(self, tmp_path,
                                                            monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = FakeDriver()
        F.submit(plan(), FillResult(form_url="x"), d)
        kinds = [c[0] for c in d.calls]
        assert kinds.index("settle") < kinds.index("click_submit")

    def test_a_refused_click_is_never_reported_as_confirmed(self, tmp_path,
                                                             monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = FakeDriver()
        d.confirms = True                      # a stale marker still on screen
        d.refusals = ["updating your application"] * F.SUBMIT_ATTEMPTS
        result = FillResult(form_url="x")
        F.submit(plan(), result, d)
        assert result.confirmed is False


class TestNamedMissingFieldsAreReappliedAfterARefusal:
    """Live on b9a009ad (Ashby): every field the board named as missing was
    already correct in the DOM — right value, right radio checked — both
    right after the original fill and in the post-refusal screenshot.
    `_reverify_fields` found nothing to fix because a read-back that already
    matches looks identical whether the board's internal state is fine or
    not. `submit()` now reads the board's own "Missing entry for required
    field: X" text after a refusal and forces a fresh write on exactly the
    fields it named, whether or not they already read correctly, before the
    retry click.
    """

    def test_a_field_the_board_named_missing_is_rewritten_even_though_it_reads_correct(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = FakeDriver(values={"q1": "https://linkedin.com/in/right"})  # already correct
        d.refusals = ["missing entry for required field"]  # first click only
        d.named_missing = [["LinkedIn Profile"]]
        p = plan(fields=[field(id="q1", kind="text", label="LinkedIn Profile",
                               value="https://linkedin.com/in/right")])
        result = FillResult(form_url="x")
        F.submit(p, result, d)
        assert result.submitted is True
        assert ("fill", "q1", "https://linkedin.com/in/right") in d.calls

    def test_a_field_the_board_did_not_name_is_left_alone(self, tmp_path,
                                                            monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = FakeDriver(values={"q1": "v", "q2": "w"})
        d.refusals = ["missing entry for required field"]
        d.named_missing = [["Only Q1"]]
        p = plan(fields=[
            field(id="q1", kind="text", label="Only Q1", value="v"),
            field(id="q2", kind="text", label="Some Other Question", value="w"),
        ])
        result = FillResult(form_url="x")
        F.submit(p, result, d)
        assert ("fill", "q1", "v") in d.calls
        assert ("fill", "q2", "w") not in d.calls

    def test_a_named_radio_group_field_is_reapplied_too(self, tmp_path,
                                                          monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = FakeDriver(kinds={"q1": "radio_group"})
        d._selected["q1"] = "Yes"  # already correct
        d.refusals = ["missing entry for required field"]
        d.named_missing = [["Which best describes it?"]]
        p = plan(fields=[field(id="q1", kind="select", label="Which best describes it?",
                               value="Yes")])
        result = FillResult(form_url="x")
        F.submit(p, result, d)
        assert ("check_radio", "q1", "Yes") in d.calls

    def test_no_named_fields_means_no_reapply_attempt(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = FakeDriver(values={"q1": "v"})
        d.refusals = ["updating your application"]  # a refusal that names nothing
        p = plan(fields=[field(id="q1", kind="text", label="Q1", value="v")])
        result = FillResult(form_url="x")
        F.submit(p, result, d)
        assert [c for c in d.calls if c[0] == "fill"] == []


class TestTheSubmitRequestIsTheRealConfirmation:
    """Ashby's button rendered, enabled, clickable, with the right text — and
    clicking it fired no request at all. The page afterwards looked exactly like
    a form waiting to be submitted, which is also what it was. Watching for the
    board's own submit call is the only signal that separates those.
    """

    def _driver_that_fires(self, ats="ashby"):
        d = FakeDriver()
        marker = F.SUBMIT_REQUEST_MARKERS[ats][0]

        def watch(_ats, sink):
            d._sink = sink

        def click(selector):
            d.calls.append(("click_submit", selector))
            d._sink.append(f"200 https://x/api/graphql?op={marker}")

        d.watch_submit_requests = watch
        d.click_submit = click
        return d

    def test_a_recorded_submit_response_confirms_on_its_own(self, tmp_path,
                                                            monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = self._driver_that_fires()
        d.confirms = False                  # no marker text anywhere
        result = FillResult(form_url="x")
        F.submit(plan_ashby(), result, d)
        assert result.submitted is True
        assert result.confirmed is True, "the board accepted it; text is irrelevant"

    def test_a_click_that_fires_nothing_is_ambiguous_not_failed(self, tmp_path,
                                                                 monkeypatch):
        # Airwallex (64dcf405): the request landed late enough that the old
        # fixed wait had already read it as silence, even though the board's
        # own confirmation email proved the click went through. Absence alone
        # must never flip `submitted` back off, or a real submission gets
        # re-clicked on the next attempt.
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = FakeDriver()                    # never records a request
        result = FillResult(form_url="x")
        F.submit(plan_ashby(), result, d)
        assert result.submitted is True
        assert [c for c in d.calls if c[0] == "click_submit"].__len__() == 1, (
            "ambiguity alone must not justify a second click"
        )
        assert "ambiguous" in result.submit_error

    def test_a_refused_first_attempts_request_does_not_confirm_a_silent_retry(
        self, tmp_path, monkeypatch,
    ):
        # Attempt 1 fires a real (marker-matching) request but the board
        # refuses the click, so it is retried. Attempt 2's click fires
        # nothing at all. `result.submit_requests` (the full-run aggregate,
        # kept for diagnostics/evidence) is non-empty because of attempt 1 —
        # but confirmation must be judged on the attempt that actually went
        # out, not on stale traffic from one the board already rejected.
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        marker = F.SUBMIT_REQUEST_MARKERS["ashby"][0]
        d = FakeDriver()
        d.refusals = ["updating your application"]  # first click only

        sinks: list[list[str]] = []

        def watch(_ats, sink):
            sinks.append(sink)

        clicks = 0

        def click(selector):
            nonlocal clicks
            clicks += 1
            d.calls.append(("click_submit", selector))
            if clicks == 1:
                sinks[-1].append(f"200 https://x/api/graphql?op={marker}")
            # attempt 2's click fires nothing

        d.watch_submit_requests = watch
        d.click_submit = click
        result = FillResult(form_url="x")
        F.submit(plan_ashby(), result, d)

        assert clicks == 2
        assert result.submitted is True, "attempt 2's click was never refused"
        assert result.submit_requests, "attempt 1's request is kept for diagnostics"
        assert result.confirmed is False, (
            "attempt 1's rejected click must not confirm attempt 2's silent one"
        )

    def test_an_unwatched_board_still_falls_back_to_page_text(self, tmp_path,
                                                              monkeypatch):
        # Lever has no measured marker yet. It must keep the old
        # behaviour — assume submitted — or every Lever role breaks.
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        assert "lever" not in F.SUBMIT_REQUEST_MARKERS
        d = FakeDriver()
        result = FillResult(form_url="x")
        F.submit(plan(), result, d)
        assert result.submitted is True

    def test_greenhouse_is_measured_too(self, tmp_path, monkeypatch):
        # boards.greenhouse.io/embed/<board>/jobs/<id>, measured live off
        # observe.ai's real submit — a 200 there is the same trustworthy
        # signal Ashby's op= name gives.
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = self._driver_that_fires(ats="greenhouse")
        result = FillResult(form_url="x")
        F.submit(plan(), result, d)
        assert result.submitted is True


class TestAshbyValidationErrorIsARefusalNotASuccess:
    """Measured live on Take2: the submit request fired (a 200 to
    ApiSubmitSingleApplicationFormAction — GraphQL's transport-level success,
    not the application's), but the board's own validation rejected the click
    and re-rendered the form with this banner. A request marker alone would
    have read this as submitted; `submission_refused()` is what catches it.
    """

    def test_the_corrections_banner_is_a_refusal(self):
        page = FakePage(text=(
            "Your form needs corrections\n"
            "Missing entry for required field: Are you legally authorized "
            "to work in the United States?"
        ))
        driver = F.BrowserDriver(page)
        assert driver.submission_refused() == "your form needs corrections"


class TestGreenhouseSubmitMarkerIgnoresItsOwnGetLoad:
    """`job-boards.greenhouse.io/embed/job_app` — the iframe's initial GET —
    contains the same `boards.greenhouse.io/embed/` substring as the real POST
    submit (it's the `job-` prefix, not a different path). The method gate is
    what tells them apart; without it every page load would read as a
    submission.
    """

    class _FakePage:
        def __init__(self):
            self.handler = None

        def on(self, _event, handler):
            self.handler = handler

    class _FakeResponse:
        def __init__(self, url, status, method):
            self.url, self.status = url, status

            class _Req:
                pass
            self.request = _Req()
            self.request.method = method

    def test_a_get_to_the_embed_path_is_not_recorded(self):
        page = self._FakePage()
        driver = F.BrowserDriver(page)
        sink: list[str] = []
        driver.watch_submit_requests("greenhouse", sink)
        page.handler(self._FakeResponse(
            "https://job-boards.greenhouse.io/embed/job_app?for=acme", 200, "GET"))
        assert sink == []

    def test_a_post_to_the_embed_path_is_recorded(self):
        page = self._FakePage()
        driver = F.BrowserDriver(page)
        sink: list[str] = []
        driver.watch_submit_requests("greenhouse", sink)
        page.handler(self._FakeResponse(
            "https://boards.greenhouse.io/embed/acme/jobs/123", 200, "POST"))
        assert sink == ["200 https://boards.greenhouse.io/embed/acme/jobs/123"]


class TestMissingFieldLabelsParsesTheBoardsOwnComplaint:
    """`b9a009ad`'s real banner, verbatim: one "Missing entry for required
    field: X" line per unanswered question."""

    class _TextPage:
        def __init__(self, text):
            self._text = text

        def locator(self, _selector):
            return self

        def inner_text(self, timeout=None):
            return self._text

    def test_one_named_field(self):
        page = self._TextPage(
            "Your form needs corrections\n"
            "Missing entry for required field: LinkedIn Profile\n"
        )
        driver = F.BrowserDriver(page)
        assert driver.missing_field_labels() == ("LinkedIn Profile",)

    def test_several_named_fields_in_one_banner(self):
        page = self._TextPage(
            "Your form needs corrections\n"
            "Missing entry for required field: Email\n"
            "Missing entry for required field: Which best describes your "
            "experience with AI agents or agentic workflows?\n"
        )
        driver = F.BrowserDriver(page)
        assert driver.missing_field_labels() == (
            "Email",
            "Which best describes your experience with AI agents or agentic "
            "workflows?",
        )

    def test_no_named_fields_on_an_unrelated_refusal(self):
        page = self._TextPage("We're updating your application, please try "
                              "again when they're finished.")
        driver = F.BrowserDriver(page)
        assert driver.missing_field_labels() == ()


class TestAReverifyThatCannotWriteStillReachesTheClick:
    """`_reverify_fields` is best-effort by design: it runs after every field
    already read back correct once, and a widget that refuses a second write
    is not new information. Letting the `FillError` out would abort `submit()`
    *before* the click on a form that is, as far as every read-back goes,
    completely filled — turning a recoverable drift into a role that never
    applies. The click is what has to survive; an unresolved drift still
    surfaces as the board's own refusal.
    """

    def _driver(self, method):
        class Refuses(FakeDriver):
            pass

        def blow_up(*args, **kwargs):
            raise FillError(f"{method}: the widget would not take it")

        d = Refuses()
        setattr(d, method, blow_up)
        return d

    @pytest.mark.parametrize("kind,method,value,drift", [
        ("yesno", "set_yesno", "Yes", "No"),
        ("radio_group", "check_radio_group", "Yes", "No"),
        ("checkbox_group", "check_group_option", ("Python", "Go"), None),
        ("select", "select_native", "Yes", "No"),
        ("text", "fill_text", "right", "wrong"),
    ])
    def test_a_field_that_refuses_the_rewrite_does_not_block_the_click(
            self, kind, method, value, drift, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = self._driver(method)
        if kind == "checkbox_group":
            d._checked_groups["q1"] = {"Python"}   # "Go" drifted out
        elif kind == "text":
            d.values["q1"] = drift
        else:
            d._selected["q1"] = drift

        p = plan(fields=[field(id="q1", kind=kind, value=value)])
        result = FillResult(form_url="x")
        F.submit(p, result, d)

        assert ("click_submit", p.submit_selector) in d.calls
        assert result.submitted is True

    def test_the_refusing_driver_really_would_have_raised(self, tmp_path, monkeypatch):
        """Without the swallow this is what `submit()` would have propagated —
        so the tests above are not passing because nothing was attempted."""
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        d = self._driver("fill_text")
        with pytest.raises(FillError):
            d.fill_text("q1", "right")


class TestAReapplyThatFailsStillLetsTheRetryClick:
    """The post-refusal re-apply is a recovery attempt, not a precondition. A
    field that will not take a fresh write must not stop the retry click — the
    board named it, but the board is also the only thing that can tell us
    whether the retry lands."""

    def test_a_failed_reapply_does_not_abort_the_retry(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)

        class RefusesTheRewrite(FakeDriver):
            def fill_text(self, field_id, value):
                self.calls.append(("fill_attempted", field_id, value))
                raise FillError("the field would not take it")

        d = RefusesTheRewrite(values={"q1": "v"})
        d.refusals = ["missing entry for required field"]   # first click only
        d.named_missing = [["Only Q1"]]
        p = plan(fields=[field(id="q1", kind="text", label="Only Q1", value="v")])
        result = FillResult(form_url="x")

        F.submit(p, result, d)

        assert len([c for c in d.calls if c[0] == "click_submit"]) == 2
        assert result.submitted is True
        # The failed re-apply must not be reported as a field that was fixed.
        assert [o for o in result.outcomes
                if o.note == "reapplied after the board named it missing"] == []


class TestTeardownAndReportingNeverLoseALandedSubmit:
    """Two swallowed handlers in `run_one`, both downstream of the
    irreversible click. A submission the caller never sees leaves the role at
    `tailored`, and the next `--submit` run applies to the same board again —
    the duplicate-application failure this whole module is built to avoid.
    """

    def _playwright(self, ctx):
        class P:
            chromium = type("C", (), {
                "launch_persistent_context": staticmethod(lambda **kw: ctx)})()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return lambda: lambda: P()

    def _ctx(self, *, close_raises=False):
        class Ctx:
            pages = []

            def new_page(self):
                return object()

            def close(self):
                if close_raises:
                    raise RuntimeError("Target page has been closed")

        return Ctx()

    def _run(self, monkeypatch, ctx, *, after=None, sink=None):
        driver = FakeDriver()
        monkeypatch.setattr(F, "_require_playwright", self._playwright(ctx))
        monkeypatch.setattr(F, "fill_plan",
                            lambda plan, driver, answers, result=None: result)
        monkeypatch.setattr(F, "_driver_for", lambda ats, page: driver)
        result = F.run_one(plan(), submit_after=True, after=after, sink=sink)
        return driver, result

    def test_a_context_that_will_not_close_still_returns_the_submission(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        sink: list = []
        driver, result = self._run(monkeypatch, self._ctx(close_raises=True),
                                    sink=sink)
        assert ("click_submit", plan().submit_selector) in driver.calls
        assert result.submitted is True
        assert sink[0] is result

    def test_a_reporting_callback_that_blows_up_does_not_lose_the_submission(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "APPLICATIONS", tmp_path)
        called: list = []

        def explode(result):
            called.append(result.submitted)
            raise RuntimeError("the terminal went away")

        driver, result = self._run(monkeypatch, self._ctx(), after=explode)
        assert called == [True], "the callback must actually have run"
        assert ("click_submit", plan().submit_selector) in driver.calls
        assert result.submitted is True
