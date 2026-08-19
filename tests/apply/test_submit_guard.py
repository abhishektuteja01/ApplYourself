"""The one invariant that matters: nothing clicks submit unless it is safe.

`fill_plan` never reaches the submit button at all (`test_fill.py`'s
`test_nothing_ever_clicks_submit` covers that). This file covers the second
half — the guard in front of the click that `run_one(submit_after=True)` and
a future `/apply run --submit` both go through.
"""
from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from src import paths
from src.apply import fill as F
from src.apply.fill import FillResult, SubmitGuardError, run_one, submit
from src.apply.plan import Plan, Unmapped

REPO_ROOT = paths.REPO_ROOT


def plan(*, unmapped=(), submit_selector="#application-form button[type=submit]",
         requires_captcha=False) -> Plan:
    return Plan(
        job_id="a1b2c3d4", board="gasketworks", token="1",
        form_url="https://boards.greenhouse.io/embed/job_app?token=1",
        company="Gasket Works", title="Widget Engineer", out_dir=Path("/tmp"),
        fields=(), files=(), unmapped=tuple(unmapped), draftable=(), skipped=(),
        submit_selector=submit_selector, submit_disabled=False,
        requires_captcha=requires_captcha,
    )


def unmapped(id="why_us", required=True) -> Unmapped:
    return Unmapped(id=id, label="Why us?", required=required, kind="textarea",
                     section="questions", tier="C", reason="no rule matches this question")


class FakeSubmitDriver:
    def __init__(self, *, disabled=False, confirms=False):
        self.disabled = disabled
        self.confirms = confirms
        self.clicked: list[str] = []
        self._ats = None
        self._sink: list[str] | None = None

    def submit_disabled_now(self, selector):
        return self.disabled

    def click_submit(self, selector):
        self.clicked.append(selector)
        # Plan defaults to greenhouse, which is now a measured board (§1).
        # This file tests the guard, not the marker feature, so a plain click
        # simulates the real board firing its own submit request — the same
        # way a click that actually lands behaves.
        markers = F.SUBMIT_REQUEST_MARKERS.get(self._ats)
        if markers and self._sink is not None:
            self._sink.append(f"200 https://example.test/{markers[0]}")

    def submission_confirmed(self):
        return self.confirms

    def settle(self):
        """Uploads must finish before the click — a board that is still
        processing one refuses it outright."""

    def watch_submit_requests(self, ats, sink):
        self._ats, self._sink = ats, sink

    def wait(self, ms):
        """The board is given time to answer before the page is judged."""

    def submission_refused(self):
        """No refusal. The default has to stay "it went out": absence of
        evidence must never become evidence of failure, or an unrecognised
        confirmation page turns into a duplicate application."""
        return ""


class TestBlockingQuestions:
    def test_required_unmapped_blocks(self):
        d = FakeSubmitDriver()
        result = FillResult(form_url="x")
        with pytest.raises(SubmitGuardError, match="why_us"):
            submit(plan(unmapped=[unmapped()]), result, d)
        assert d.clicked == []

    def test_recovered_at_fill_time_no_longer_blocks(self):
        # This is the case §9 exists for: hispanic_ethnicity and country park
        # in the plan but resolve once fill.py can read the widget's real
        # options. The guard must not re-park what fill_plan already fixed.
        d = FakeSubmitDriver()
        result = FillResult(form_url="x", recovered=["why_us"])
        submit(plan(unmapped=[unmapped()]), result, d)
        assert d.clicked == ["#application-form button[type=submit]"]

    def test_a_field_that_never_parked_is_not_a_blocker(self):
        # `plan.draftable[]` and `plan.skipped[]` are the optional-and-left-alone
        # cases — they never enter `plan.unmapped[]` (see answers.resolve /
        # plan.build_plan), so a role carrying only those still submits.
        d = FakeSubmitDriver()
        result = FillResult(form_url="x")
        submit(plan(unmapped=()), result, d)
        assert d.clicked == ["#application-form button[type=submit]"]


class TestFillFailureBlocks:
    def test_a_failed_react_select_assert_blocks(self):
        # `_select` raising FillError is exactly how a mis-stuck react-select
        # surfaces; fill_plan turns that into result.failures.
        d = FakeSubmitDriver()
        result = FillResult(form_url="x", failures=["country: no option matching 'USA'"])
        with pytest.raises(SubmitGuardError, match="country"):
            submit(plan(), result, d)
        assert d.clicked == []

    def test_no_failures_and_nothing_unmapped_submits(self):
        d = FakeSubmitDriver()
        result = FillResult(form_url="x")
        submit(plan(), result, d)
        assert len(d.clicked) == 1


class TestFormShape:
    def test_no_submit_selector_refuses(self):
        d = FakeSubmitDriver()
        result = FillResult(form_url="x")
        with pytest.raises(SubmitGuardError, match="no submit button"):
            submit(plan(submit_selector=None), result, d)
        assert d.clicked == []

    def test_a_disabled_button_refuses_even_if_everything_else_resolved(self):
        # aria-disabled at fill time, re-read live rather than trusted from the
        # scan snapshot — plan.submit_disabled can be stale by the time every
        # field has been written.
        d = FakeSubmitDriver(disabled=True)
        result = FillResult(form_url="x")
        with pytest.raises(SubmitGuardError, match="disabled"):
            submit(plan(), result, d)
        assert d.clicked == []


class TestRunOneNeverSubmitsByDefault:
    def test_the_default_path_does_not_reach_submit(self, monkeypatch):
        called = []
        monkeypatch.setattr(F, "submit", lambda *a, **k: called.append(1))

        class Ctx:
            pages = []
            def new_page(self):
                return object()
            def close(self):
                pass

        class P:
            chromium = type("C", (), {"launch_persistent_context": staticmethod(lambda **kw: Ctx())})()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        monkeypatch.setattr(F, "_require_playwright", lambda: lambda: P())
        monkeypatch.setattr(F, "fill_plan", lambda plan, driver, answers, result=None: FillResult(form_url="x"))

        run_one(plan(), submit_after=False)
        assert called == []

    def test_submit_after_true_reaches_submit_and_records_the_result(self, monkeypatch):
        class Ctx:
            pages = []
            def new_page(self):
                return object()
            def close(self):
                pass

        class P:
            chromium = type("C", (), {"launch_persistent_context": staticmethod(lambda **kw: Ctx())})()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        monkeypatch.setattr(F, "_require_playwright", lambda: lambda: P())
        monkeypatch.setattr(F, "fill_plan", lambda plan, driver, answers, result=None: FillResult(form_url="x"))
        monkeypatch.setattr(F, "BrowserDriver", lambda page: FakeSubmitDriver())

        result = run_one(plan(), submit_after=True)
        assert result.submitted is True
        assert result.submit_error == ""

    def test_a_guard_refusal_is_recorded_not_raised(self, monkeypatch):
        # One parked role in a queue must not kill the run (§13).
        class Ctx:
            pages = []
            def new_page(self):
                return object()
            def close(self):
                pass

        class P:
            chromium = type("C", (), {"launch_persistent_context": staticmethod(lambda **kw: Ctx())})()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def failing_fill(plan, driver, answers, result=None):
            # Mutates the caller's result, as the real fill_plan does — run_one
            # holds that object so a crash cannot lose `submitted`.
            result.failures.append("boom")
            return result

        monkeypatch.setattr(F, "_require_playwright", lambda: lambda: P())
        monkeypatch.setattr(F, "fill_plan", failing_fill)
        monkeypatch.setattr(F, "BrowserDriver", lambda page: FakeSubmitDriver())

        result = run_one(plan(), submit_after=True)
        assert result.submitted is False
        assert "boom" in result.submit_error


class TestTheFillEntryPointCannotSubmit:
    """`fill()` is the documented no-submit API surface — `apply fill` reaches
    it and has no `--submit` in reach at all. Every other test drives
    `fill_plan` or `run_one` directly, so the one function the narrow CLI
    actually calls was never checked against a driver that would have
    clicked.
    """

    def _playwright(self):
        class Ctx:
            pages = []

            def new_page(self):
                return object()

            def close(self):
                pass

        class P:
            chromium = type("C", (), {
                "launch_persistent_context": staticmethod(lambda **kw: Ctx())})()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return lambda: lambda: P()

    def test_a_driver_that_would_have_clicked_is_never_asked_to(self, monkeypatch):
        driver = FakeSubmitDriver()
        monkeypatch.setattr(F, "_require_playwright", self._playwright())
        monkeypatch.setattr(F, "fill_plan",
                            lambda plan, drv, answers, result=None: result)
        monkeypatch.setattr(F, "_driver_for", lambda ats, page: driver)

        result = F.fill(plan())

        assert driver.clicked == []
        assert result.submitted is False
        assert result.submit_error == ""

    def test_the_same_driver_does_click_from_the_submitting_entry_point(
        self, monkeypatch
    ):
        """Otherwise the test above passes because the fake never clicks."""
        driver = FakeSubmitDriver()
        monkeypatch.setattr(F, "_require_playwright", self._playwright())
        monkeypatch.setattr(F, "fill_plan",
                            lambda plan, drv, answers, result=None: result)
        monkeypatch.setattr(F, "_driver_for", lambda ats, page: driver)

        result = F.run_one(plan(), submit_after=True)

        assert driver.clicked == [plan().submit_selector]
        assert result.submitted is True

    def test_fill_publishes_its_result_into_the_sink_before_the_browser_opens(
        self, monkeypatch
    ):
        sink: list = []
        monkeypatch.setattr(F, "_require_playwright", self._playwright())
        monkeypatch.setattr(F, "fill_plan",
                            lambda plan, drv, answers, result=None: result)
        monkeypatch.setattr(F, "_driver_for", lambda ats, page: FakeSubmitDriver())

        result = F.fill(plan(), sink=sink)
        assert sink == [result]


class TestStaticSubmitSelectorBoundary:
    def test_submit_selector_is_touched_only_by_the_guarded_path(self):
        """`submit_selector` must not be read or clicked from any function
        other than the guard itself and the one driver method it calls — that
        is what makes `submit()` the single path to a real click.

        Scans every module under `src/apply/` plus `src/apply_cli.py`, not just
        `fill.py`: the guard was written when `fill.py` was the only driver,
        and `lever.py`/`ashby.py` were never examined.

        Matches on *call shape*, not on a substring of the source. The old
        form looked for the literal text `submit_selector`/`click_submit`, so
        `page.get_by_role("button", name="Submit application").click()` walked
        straight past it — and it only visited `ast.FunctionDef`, so an
        `async def` driver method was not even looked at.
        """
        allowed = {"submit", "click_submit", "submit_disabled_now",
                    "submission_confirmed", "wait_for_captcha"}
        # Anything that can activate a control. `press` covers Enter-in-input,
        # which submits a form without any click at all.
        activating = {"click", "press", "dblclick", "tap", "check", "submit"}

        offenders = []
        targets = sorted((REPO_ROOT / "src" / "apply").rglob("*.py"))
        targets.append(REPO_ROOT / "src" / "apply_cli.py")
        for path in targets:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name in allowed:
                    continue
                for inner in ast.walk(node):
                    if not isinstance(inner, ast.Call):
                        continue
                    func = inner.func
                    if not isinstance(func, ast.Attribute):
                        continue
                    if func.attr not in activating:
                        continue
                    seg = ast.get_source_segment(source, inner) or ""
                    if "submit" in seg.lower():
                        offenders.append(
                            f"{path.relative_to(REPO_ROOT)}:{inner.lineno} "
                            f"in {node.name}(): {seg[:60]}"
                        )
        assert offenders == []

    def test_the_guard_would_catch_a_differently_spelled_submit_click(self):
        """A guard that cannot fail is not a guard. The old substring form
        passed this exact line."""
        source = (
            "def rogue(page):\n"
            "    page.get_by_role('button', name='Submit application').click()\n"
        )
        activating = {"click", "press", "dblclick", "tap", "check", "submit"}
        hits = [
            n for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in activating
            and "submit" in (ast.get_source_segment(source, n) or "").lower()
        ]
        assert hits, "the call-shape predicate must match a real offender"

    def test_the_guard_visits_async_methods(self):
        """`ast.FunctionDef` does not match `async def`, so an async driver
        method was invisible to the old guard."""
        source = (
            "async def rogue(page):\n"
            "    await page.locator(submit_selector).click()\n"
        )
        seen = [n.name for n in ast.walk(ast.parse(source))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        assert seen == ["rogue"]



class TestACaptchaNeverSolvedIsNeverSubmitted:
    """On a captcha board the click only opens the challenge (lever.py: Lever's
    submit is a JS-driven `type="button"`, not a native form submit) — the
    real POST fires from the captcha's own success callback. So unlike a
    non-captcha board, the click alone is not the irreversible act: if the
    captcha is abandoned or times out, nothing was sent, and `submitted` must
    stay False (acea77ed: a timed-out captcha was recorded as `applied` when
    the board never received anything).
    """

    def test_a_captcha_timeout_after_the_click_leaves_it_unsubmitted(self):
        class CaptchaExplodes(FakeSubmitDriver):
            def wait_for_captcha(self):
                raise TimeoutError("hCaptcha never solved")

        d = CaptchaExplodes()
        result = FillResult(form_url="x")
        p = plan(requires_captcha=True)

        with pytest.raises(TimeoutError):
            submit(p, result, d)

        assert d.clicked == [p.submit_selector]
        assert result.submitted is False, "the captcha was never solved; nothing was sent"

    def test_run_one_reports_unsubmitted_when_the_captcha_wait_explodes(
        self, monkeypatch
    ):
        class CaptchaExplodes(FakeSubmitDriver):
            def wait_for_captcha(self):
                raise TimeoutError("hCaptcha never solved")

        class Ctx:
            pages = []
            def new_page(self):
                return object()
            def close(self):
                pass

        class P:
            chromium = type("C", (), {
                "launch_persistent_context": staticmethod(lambda **kw: Ctx())})()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        p = plan(requires_captcha=True)
        monkeypatch.setattr(F, "_require_playwright", lambda: lambda: P())
        monkeypatch.setattr(F, "fill_plan",
                            lambda plan, driver, answers, result=None: FillResult(form_url="x"))
        monkeypatch.setattr(F, "BrowserDriver", lambda page: CaptchaExplodes())

        result = run_one(p, submit_after=True)

        assert result.submitted is False
        assert "TimeoutError" in result.submit_error

    def test_a_guard_refusal_is_raised_before_the_click_so_submitted_stays_false(self):
        d = FakeSubmitDriver()
        result = FillResult(form_url="x")
        with pytest.raises(SubmitGuardError):
            submit(plan(unmapped=[unmapped()]), result, d)
        assert d.clicked == []
        assert result.submitted is False


class TestConfirmationIsPositiveEvidenceOnly:
    def test_no_confirmation_marker_is_unconfirmed_not_failed(self):
        # Lever carries no measured request marker either (§1), so this is
        # the genuine "nothing to go on but page text" case the test means to
        # cover — greenhouse's own marker would confirm it on its own now.
        d = FakeSubmitDriver(confirms=False)
        result = FillResult(form_url="x")
        submit(replace(plan(), ats="lever"), result, d)
        assert result.submitted is True
        assert result.confirmed is False

    def test_a_confirmation_marker_sets_confirmed(self):
        d = FakeSubmitDriver(confirms=True)
        result = FillResult(form_url="x")
        submit(plan(), result, d)
        assert result.submitted is True
        assert result.confirmed is True
