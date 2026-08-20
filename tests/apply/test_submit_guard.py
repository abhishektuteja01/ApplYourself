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
    def __init__(self, *, disabled=False, confirms=False, invalid=()):
        self.disabled = disabled
        self.invalid = tuple(invalid)
        self.confirms = confirms
        self.clicked: list[str] = []
        self._ats = None
        self._sink: list[str] | None = None
        #: Ordered record of the captcha handshake, so a test can assert the
        #: stale token is cleared *before* the wait rather than after.
        self.captcha_calls: list[str] = []

    def clear_captcha_token(self):
        self.captcha_calls.append("clear")

    def wait_for_captcha(self):
        self.captcha_calls.append("wait")

    def submit_disabled_now(self, selector):
        return self.disabled

    def invalid_fields(self):
        """A valid form by default. `invalid` names the controls the browser
        would refuse, which blocks the click before it happens."""
        return tuple(self.invalid)

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

    def settle(self, timeout=None, floor_ms=0):
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
        # A board with no measured request marker is the genuine "nothing to go
        # on but page text" case this covers — every measured board's marker
        # would confirm it on its own. Lever was this case until cae17aa7.
        d = FakeSubmitDriver(confirms=False)
        result = FillResult(form_url="x")
        submit(replace(plan(), ats="unmeasured_board"), result, d)
        assert result.submitted is True
        assert result.confirmed is False

    def test_a_confirmation_marker_sets_confirmed(self):
        d = FakeSubmitDriver(confirms=True)
        result = FillResult(form_url="x")
        submit(plan(), result, d)
        assert result.submitted is True
        assert result.confirmed is True


class TestTheBrowserItselfCanRefuseTheForm:
    """Constraint validation runs before any of the board's own script, so a
    form the browser refuses cannot submit: the click fires, nothing leaves the
    browser, the page does not navigate, and the only signal is a validation
    bubble no capture reads. That was recorded as "submitted, no confirmation
    seen" and transitioned to applied on a form the board never received
    (387c1801: the self-ID date was required-when-answered and left empty).
    """

    def test_an_invalid_form_is_never_clicked(self):
        d = FakeSubmitDriver(invalid=["eeo[disabilitySignatureDate]"])
        result = FillResult(form_url="x")
        with pytest.raises(F.SubmitGuardError, match="disabilitySignatureDate"):
            submit(plan(), result, d)
        assert d.clicked == [], "nothing may be clicked on a form the browser refuses"
        assert result.submitted is False

    def test_the_guard_names_every_offending_field(self):
        d = FakeSubmitDriver(invalid=["eeo[disabilitySignature]",
                                      "eeo[disabilitySignatureDate]"])
        with pytest.raises(F.SubmitGuardError) as exc:
            submit(plan(), FillResult(form_url="x"), d)
        assert "eeo[disabilitySignature]" in str(exc.value)
        assert "eeo[disabilitySignatureDate]" in str(exc.value)
        assert "are required" in str(exc.value)

    def test_a_single_offender_reads_as_singular(self):
        d = FakeSubmitDriver(invalid=["eeo[disabilitySignatureDate]"])
        with pytest.raises(F.SubmitGuardError, match="is required"):
            submit(plan(), FillResult(form_url="x"), d)

    def test_a_valid_form_still_clicks(self):
        d = FakeSubmitDriver()
        result = FillResult(form_url="x")
        submit(plan(), result, d)
        assert d.clicked == [plan().submit_selector]
        assert result.submitted is True

    def test_a_board_with_no_form_element_is_not_treated_as_invalid(self):
        """Ashby renders no <form> at all — nothing to check is not the same as
        something invalid."""
        d = FakeSubmitDriver(invalid=())
        result = FillResult(form_url="x")
        submit(plan(), result, d)
        assert result.submitted is True
        assert d.clicked == [plan().submit_selector]


class TestTheAllRequestsDiagnostic:
    """A board with no marker cannot be measured any other way, so this flag
    logs what a real submission actually does. It is how Lever's marker was
    measured: its form posts to the same URL the page was fetched from, so
    nothing short of a live capture could distinguish the submit from the page
    load. The flag must never become a confirmation signal — a marker matched
    too broadly is how a click that did nothing gets reported as a submission.
    """

    class FakePage:
        def __init__(self):
            self.handlers = []

        def on(self, event, handler):
            self.handlers.append((event, handler))

    class FakeResponse:
        def __init__(self, method, url, status=200):
            self.status, self.url = status, url
            self.request = type("R", (), {"method": method})()

    def _driver(self, monkeypatch, *, enabled):
        if enabled:
            monkeypatch.setenv(F.LOG_ALL_REQUESTS_ENV, "1")
        else:
            monkeypatch.delenv(F.LOG_ALL_REQUESTS_ENV, raising=False)
        page = self.FakePage()
        return F.BrowserDriver(page), page

    def test_off_by_default_on_an_unmarked_board(self, monkeypatch):
        driver, page = self._driver(monkeypatch, enabled=False)
        sink = []
        driver.watch_submit_requests("unmeasured_board", sink)
        assert page.handlers == [], "no watcher at all on an unmarked board"

    def test_the_flag_attaches_a_watcher_to_an_unmarked_board(self, monkeypatch):
        driver, page = self._driver(monkeypatch, enabled=True)
        driver.watch_submit_requests("unmeasured_board", [])
        assert [e for e, _ in page.handlers] == ["response"]

    def test_it_never_feeds_the_confirmation_sink(self, monkeypatch, caplog):
        """The whole point: an unmeasured board must not acquire a submission
        signal by being watched loosely."""
        driver, page = self._driver(monkeypatch, enabled=True)
        sink = []
        driver.watch_submit_requests("unmeasured_board", sink)
        _, handler = page.handlers[0]
        with caplog.at_level("INFO"):
            handler(self.FakeResponse("POST", "https://jobs.lever.co/x/y/apply"))
        assert sink == [], "the diagnostic must not populate submit_requests"
        assert "jobs.lever.co/x/y/apply" in caplog.text

    def test_gets_are_not_logged(self, monkeypatch, caplog):
        driver, page = self._driver(monkeypatch, enabled=True)
        driver.watch_submit_requests("unmeasured_board", [])
        _, handler = page.handlers[0]
        with caplog.at_level("INFO"):
            handler(self.FakeResponse("GET", "https://jobs.lever.co/x/y/apply"))
        assert "jobs.lever.co" not in caplog.text

    def test_a_marked_board_still_gets_its_real_watcher_too(self, monkeypatch):
        driver, page = self._driver(monkeypatch, enabled=True)
        sink = []
        driver.watch_submit_requests("greenhouse", sink)
        assert len(page.handlers) == 2, "the diagnostic must not replace the marker"


class TestTheLeverSubmitMarker:
    """Measured off a real submission (cae17aa7):

        REQUEST POST 302 https://jobs.lever.co/thinkahead/<id>/apply

    Two things that measurement settled. The form posts to the same URL the
    page was fetched from, so only the method separates the submit from the
    page load. And it answers **302**, not 2xx — the 2xx-only rule dropped the
    one response that proves the submission landed, which is why every Lever
    run reported `submit_requests: []` even when it succeeded.
    """

    class FakePage:
        def __init__(self):
            self.handler = None

        def on(self, event, handler):
            self.handler = handler

    def _response(self, method, url, status):
        return type("Resp", (), {
            "status": status, "url": url,
            "request": type("R", (), {"method": method})(),
        })()

    def _sink_for(self, method, url, status, ats="lever"):
        page = self.FakePage()
        driver, sink = F.BrowserDriver(page), []
        driver.watch_submit_requests(ats, sink)
        page.handler(self._response(method, url, status))
        return sink

    FORM = "https://jobs.lever.co/thinkahead/cb488cff/apply"

    def test_the_measured_redirect_counts_as_the_submission(self):
        assert self._sink_for("POST", self.FORM, 302) == [f"302 {self.FORM}"]

    def test_a_2xx_on_the_same_endpoint_also_counts(self):
        assert self._sink_for("POST", self.FORM, 200) == [f"200 {self.FORM}"]

    def test_the_page_load_is_not_mistaken_for_a_submission(self):
        """Same URL, different method — the only thing telling them apart."""
        assert self._sink_for("GET", self.FORM, 200) == []

    def test_a_refusal_is_not_recorded_as_a_submission(self):
        assert self._sink_for("POST", self.FORM, 400) == []
        assert self._sink_for("POST", self.FORM, 500) == []

    def test_the_captcha_and_third_party_posts_are_not_the_submission(self):
        """Measured alongside the submit: these fire on their own hosts."""
        for url in ("https://api.hcaptcha.com/getcaptcha/e33f87f8",
                    "https://www.linkedin.com/li/track",
                    "https://www.linkedin.com/talentwidgets/apply-with-linkedin"):
            assert self._sink_for("POST", url, 200) == [], url

    def test_the_redirect_allowance_is_not_global(self):
        """For the XHR boards a 3xx is a shape nothing has measured, so
        treating one as success there would be a guess."""
        assert "ashby" not in F.SUBMIT_REQUEST_REDIRECT_OK
        assert "greenhouse" not in F.SUBMIT_REQUEST_REDIRECT_OK
        assert self._sink_for(
            "POST", "https://jobs.ashbyhq.com/api/non-user-graphql", 302,
            ats="ashby",
        ) == []
