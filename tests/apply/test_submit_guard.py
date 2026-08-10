"""The one invariant that matters: nothing clicks submit unless it is safe.

`fill_plan` never reaches the submit button at all (`test_fill.py`'s
`test_nothing_ever_clicks_submit` covers that). This file covers the second
half — the guard in front of the click that `run_one(submit_after=True)` and
a future `/apply run --submit` both go through.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.apply import fill as F
from src.apply.fill import FillResult, SubmitGuardError, run_one, submit
from src.apply.plan import Plan, Unmapped


def plan(*, unmapped=(), submit_selector="#application-form button[type=submit]") -> Plan:
    return Plan(
        job_id="a1b2c3d4", board="gasketworks", token="1",
        form_url="https://boards.greenhouse.io/embed/job_app?token=1",
        company="Gasket Works", title="Widget Engineer", out_dir=Path("/tmp"),
        fields=(), files=(), unmapped=tuple(unmapped), draftable=(), skipped=(),
        submit_selector=submit_selector, submit_disabled=False,
    )


def unmapped(id="why_us", required=True) -> Unmapped:
    return Unmapped(id=id, label="Why us?", required=required, kind="textarea",
                     section="questions", tier="C", reason="no rule matches this question")


class FakeSubmitDriver:
    def __init__(self, *, disabled=False):
        self.disabled = disabled
        self.clicked: list[str] = []

    def submit_disabled_now(self, selector):
        return self.disabled

    def click_submit(self, selector):
        self.clicked.append(selector)


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
        monkeypatch.setattr(F, "fill_plan", lambda plan, driver, answers: FillResult(form_url="x"))

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
        monkeypatch.setattr(F, "fill_plan", lambda plan, driver, answers: FillResult(form_url="x"))
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

        monkeypatch.setattr(F, "_require_playwright", lambda: lambda: P())
        monkeypatch.setattr(
            F, "fill_plan",
            lambda plan, driver, answers: FillResult(form_url="x", failures=["boom"]),
        )
        monkeypatch.setattr(F, "BrowserDriver", lambda page: FakeSubmitDriver())

        result = run_one(plan(), submit_after=True)
        assert result.submitted is False
        assert "boom" in result.submit_error


class TestStaticSubmitSelectorBoundary:
    def test_submit_selector_is_touched_only_by_the_guarded_path(self):
        """`submit_selector` must not be read or clicked from any function
        other than the guard itself and the one driver method it calls — that
        is what makes `submit()` the single path to a real click."""
        allowed = {"submit", "click_submit", "submit_disabled_now"}
        source = Path(F.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name not in allowed:
                body = ast.get_source_segment(source, node) or ""
                if "submit_selector" in body or "click_submit" in body:
                    offenders.append(node.name)
        assert offenders == []
