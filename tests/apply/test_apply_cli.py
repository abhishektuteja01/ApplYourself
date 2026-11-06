"""`uv run apply plan <job_id>` — the read-only entry point.

The queue, the rate limiter and the run report land with fill.py; until then the
CLI's whole job is picking the right posting URL and the right /tailor dir, and
failing legibly when it cannot. Both of those are grounded in what the live
pipeline actually holds: a role can carry a different URL in state.yaml than in
clean.parquet, and can be missing from clean.parquet entirely once it ages out
of the 14-day window.

No network: load_board is stubbed everywhere.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
import yaml

from src import app_cli, apply_cli
from src.apply import ashby, confirmation, ledger
from src.apply.answers import AnswersError
from src.apply.greenhouse import BoardForm, Posting, PostingExpired
from src.apply.plan import PlanError
from src.apply.schema import parse_schema
from tests import factories

from .conftest import FIXTURES, load_fixture, load_html

GH_URL = "https://job-boards.greenhouse.io/bushinggroup/jobs/1000003"
LINKEDIN_URL = "https://www.linkedin.com/jobs/view/4422512798"
JOB_ID = "a1b2c3d4"


@pytest.fixture
def repo(tmp_path, monkeypatch, tailor_dir):
    """A miniature repo: clean.parquet, pipeline/, applications/."""
    clean = tmp_path / "clean.parquet"
    pipeline = tmp_path / "pipeline"
    pipeline.mkdir()
    monkeypatch.setattr(apply_cli, "CLEAN", clean)
    monkeypatch.setattr(apply_cli, "APPLICATIONS", tailor_dir.parent.parent)
    # The role's folder comes from app_cli's resolver.
    monkeypatch.setattr(app_cli, "APPLICATIONS", tailor_dir.parent.parent)
    monkeypatch.setattr(app_cli, "_identity", lambda: ("Alex Example", "Alex_Example"))

    tailored = f"{tailor_dir.parent.name}/{tailor_dir.name}"

    class Repo:
        def __init__(self):
            self.clean = clean
            self.pipeline = pipeline
            self.tailored = tailored
            self.out_dir = tailor_dir

        def write_clean(self, **rows):
            factories.write_clean(clean, *({"job_id": j, "url": u} for j, u in rows.items()))

        def write_state(self, job_id=JOB_ID, url=GH_URL, tailored_dirs=None, state="tailored"):
            factories.write_state(
                pipeline,
                job_id,
                state,
                company="Bushing Group",
                title="Widget Engineer",
                url=url,
                tailored_dirs=[tailored] if tailored_dirs is None else tailored_dirs,
            )

    return Repo()


@pytest.fixture
def stub_board(monkeypatch):
    """Serve a fixture board without touching the network."""
    calls = []

    def fake_load_board(url, timeout=30):
        calls.append(url)
        name = "form_education"
        from src.apply.domscan import scan_form
        from src.apply.reconcile import reconcile

        scan = scan_form(load_html(name))
        schema = parse_schema(load_fixture(name))
        return BoardForm(
            posting=Posting(token="1000003", url_slug=None),
            slug="bushinggroup",
            html="",
            scan=scan,
            schema=schema,
            reconciled=reconcile(scan, schema),
        )

    monkeypatch.setattr(apply_cli, "load_board", fake_load_board)
    return calls


@pytest.fixture(autouse=True)
def stub_answers(monkeypatch):
    """The synthetic widget-world config, never the user's real one."""
    from src.apply.answers import load_answers

    loaded = load_answers(FIXTURES / "application_answers.yaml")
    monkeypatch.setattr(apply_cli, "load_answers", lambda *a, **k: loaded)


LEVER_URL = "https://jobs.lever.co/widgetco/00000001-0000-0000-0000-000000000001"
ASHBY_URL = "https://jobs.ashbyhq.com/widgetco/00000001-0000-0000-0000-000000000001"


class TestDetectAts:
    def test_greenhouse(self):
        assert apply_cli.detect_ats(GH_URL) == "greenhouse"

    def test_lever(self):
        assert apply_cli.detect_ats(LEVER_URL) == "lever"

    def test_ashby(self):
        assert apply_cli.detect_ats(ASHBY_URL) == "ashby"

    def test_eu_lever_is_detected_as_plain_lever(self):
        assert (
            apply_cli.detect_ats(LEVER_URL.replace("jobs.lever.co", "jobs.eu.lever.co")) == "lever"
        )

    def test_eu_greenhouse(self):
        assert (
            apply_cli.detect_ats("https://boards.eu.greenhouse.io/widgetco/jobs/1000001")
            == "greenhouse"
        )

    def test_a_gh_jid_careers_page(self):
        assert apply_cli.detect_ats("https://widgetco.com/jobs?gh_jid=1000001") == "greenhouse"

    def test_workday_is_recognised_but_not_submittable(self):
        url = "https://widgetco.wd5.myworkdayjobs.com/Careers/job/US/Eng_JR1"
        assert apply_cli.detect_ats(url) == "workday"
        assert not apply_cli.is_auto_submittable(url)

    def test_neither(self):
        assert apply_cli.detect_ats(LINKEDIN_URL) is None


class TestBuildDispatchesToLever:
    def test_a_lever_url_builds_a_lever_plan(self, repo, monkeypatch, tailor_dir):
        repo.write_clean(**{JOB_ID: LEVER_URL})
        repo.write_state(url=LEVER_URL)
        from .conftest import load_html

        monkeypatch.setattr(
            "src.apply.lever.fetch_text", lambda *a, **k: load_html("form_lever_minimal")
        )

        plan, _ = apply_cli.build(JOB_ID)
        assert plan.ats == "lever"
        assert plan.requires_captcha is True
        assert plan.board == "widgetco"


class TestBuildDispatchesToAshby:
    def _stub_api(self, monkeypatch):
        from .conftest import load_api

        monkeypatch.setattr(
            "src.apply.ashby.fetch_json_post", lambda *a, **k: load_api("api_ashby_form")
        )
        # Otherwise `load_board` would open a real headless browser to read
        # DOM-only descriptions — no network in this test module.
        monkeypatch.setattr("src.apply.ashby.fetch_dom_enrichment", lambda *a, **k: {})

    def test_an_ashby_url_builds_an_ashby_plan(self, repo, monkeypatch, tailor_dir):
        repo.write_clean(**{JOB_ID: ASHBY_URL})
        repo.write_state(url=ASHBY_URL)
        self._stub_api(monkeypatch)

        plan, _ = apply_cli.build(JOB_ID)
        assert plan.ats == "ashby"
        assert plan.board == "widgetco"

    def test_an_ashby_plan_carries_a_submit_selector(self, repo, monkeypatch, tailor_dir):
        """The API path states the selector without the DOM in hand: the class
        is unhashed and resolves to one button on every live board."""
        repo.write_clean(**{JOB_ID: ASHBY_URL})
        repo.write_state(url=ASHBY_URL)
        self._stub_api(monkeypatch)

        plan, _ = apply_cli.build(JOB_ID)
        assert plan.submit_selector == ashby.SUBMIT_SELECTOR

    def test_ashby_now_reaches_the_browser_rather_than_the_manual_bucket(
        self, repo, monkeypatch, tailor_dir
    ):
        """Registering the driver is what moves these roles out of `manual`.
        The shortlist reads the same flag, so the two cannot disagree."""
        repo.write_clean(**{JOB_ID: ASHBY_URL})
        repo.write_state(url=ASHBY_URL)
        self._stub_api(monkeypatch)
        opened = []
        monkeypatch.setattr(
            "src.apply_cli.run_one", lambda plan, *a, **k: opened.append(plan.ats) or _fake_result()
        )

        outcome = apply_cli._run_role(JOB_ID, submit=False, headless=False)
        assert opened == ["ashby"]
        assert outcome.category != "manual"


class TestResolveUrl:
    def test_clean_parquet_wins_when_it_holds_a_board_url(self, repo, stub_board):
        repo.write_clean(**{JOB_ID: GH_URL})
        repo.write_state(url="https://www.linkedin.com/jobs/view/1")
        assert apply_cli.resolve_url(JOB_ID, {"url": LINKEDIN_URL}) == GH_URL

    def test_state_is_used_when_clean_holds_an_unapplyable_url(self, repo):
        # A real role carries an Avature url in state and a LinkedIn one in
        # clean; another carries two different LinkedIn urls.
        repo.write_clean(**{JOB_ID: LINKEDIN_URL})
        assert apply_cli.resolve_url(JOB_ID, {"url": GH_URL}) == GH_URL

    def test_state_is_used_when_the_role_aged_out_of_clean(self, repo):
        repo.write_clean(other="https://example.com")
        assert apply_cli.resolve_url(JOB_ID, {"url": GH_URL}) == GH_URL

    def test_works_with_no_clean_parquet_at_all(self, repo):
        assert apply_cli.resolve_url(JOB_ID, {"url": GH_URL}) == GH_URL

    def test_neither_source_applyable_names_both(self, repo):
        repo.write_clean(**{JOB_ID: LINKEDIN_URL})
        with pytest.raises(apply_cli.ApplyCliError) as exc:
            apply_cli.resolve_url(JOB_ID, {"url": "https://jobs.lever.co/x/y"})
        assert "clean.parquet" in str(exc.value) and "state.yaml" in str(exc.value)
        assert "by hand" in str(exc.value)

    def test_an_empty_url_is_reported_not_silently_skipped(self, repo):
        repo.write_clean(**{JOB_ID: ""})
        with pytest.raises(apply_cli.ApplyCliError, match=r"\(empty\)"):
            apply_cli.resolve_url(JOB_ID, {"url": ""})

    def test_unknown_job_id_anywhere(self, repo):
        repo.write_clean(other=GH_URL)
        with pytest.raises(apply_cli.ApplyCliError, match=r"neither clean.parquet nor"):
            apply_cli.resolve_url(JOB_ID, None)


class TestResolveOutDir:
    def test_the_most_recent_tailor_dir_wins(self, repo):
        # tailored_dirs[] is append-only, so a re-tailor puts _v2 at the end.
        repo.write_state(tailored_dirs=["old/dir", repo.tailored])
        assert apply_cli.resolve_out_dir(JOB_ID) == repo.out_dir

    def test_no_tailored_dirs_points_at_tailor(self, repo):
        repo.write_state(tailored_dirs=[])
        with pytest.raises(apply_cli.ApplyCliError, match="run /tailor"):
            apply_cli.resolve_out_dir(JOB_ID)

    def test_no_state_at_all_points_at_tailor(self, repo):
        with pytest.raises(apply_cli.ApplyCliError, match="run /tailor"):
            apply_cli.resolve_out_dir(JOB_ID)

    def test_a_tailored_dir_that_is_gone_from_disk_is_named(self, repo):
        repo.write_state(tailored_dirs=["example_primary/deleted"])
        with pytest.raises(apply_cli.ApplyCliError, match="not on disk"):
            apply_cli.resolve_out_dir(JOB_ID)


class TestSalaryFor:
    """`salary_for()` — the figure a salary question asks for, from this
    posting's parsed compensation and the lane's `salary_expectation`."""

    def _write_clean(self, path, job_id, salary_min, salary_currency):
        factories.write_clean(
            path,
            {
                "job_id": job_id,
                "url": GH_URL,
                "salary_min": salary_min,
                "salary_currency": salary_currency,
            },
        )

    def _verticals(self, tmp_path, monkeypatch, expectation):
        path = tmp_path / "verticals.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "verticals": {"example_primary": {"salary_expectation": expectation}},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(apply_cli.paths, "PROFILE", tmp_path)

    def test_a_us_role_marks_up_its_posted_minimum(self, tmp_path, monkeypatch):
        clean = tmp_path / "clean.parquet"
        self._write_clean(clean, JOB_ID, 120000, "USD")
        monkeypatch.setattr(apply_cli, "CLEAN", clean)
        self._verticals(tmp_path, monkeypatch, {"markup_pct": 10, "fallback": 110000})
        assert apply_cli.salary_for(JOB_ID, "example_primary", "United States") == 132000.0

    def test_no_posted_minimum_uses_the_lane_fallback(self, tmp_path, monkeypatch):
        clean = tmp_path / "clean.parquet"
        self._write_clean(clean, JOB_ID, None, "USD")
        monkeypatch.setattr(apply_cli, "CLEAN", clean)
        self._verticals(tmp_path, monkeypatch, {"markup_pct": 10, "fallback": 110000})
        assert apply_cli.salary_for(JOB_ID, "example_primary", "United States") == 110000.0

    def test_a_plain_fallback_is_a_us_figure_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr(apply_cli, "CLEAN", tmp_path / "missing.parquet")
        self._verticals(tmp_path, monkeypatch, {"markup_pct": 10, "fallback": 110000})
        assert apply_cli.salary_for(JOB_ID, "example_primary", "Germany") is None

    def test_a_per_country_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setattr(apply_cli, "CLEAN", tmp_path / "missing.parquet")
        self._verticals(
            tmp_path, monkeypatch, {"fallback": {"United States": 110000, "Germany": 70000}}
        )
        assert apply_cli.salary_for(JOB_ID, "example_primary", "Germany") == 70000.0

    def test_no_salary_expectation_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(apply_cli, "CLEAN", tmp_path / "missing.parquet")
        (tmp_path / "verticals.yaml").write_text(
            yaml.safe_dump({"verticals": {"example_primary": {}}}), encoding="utf-8"
        )
        monkeypatch.setattr(apply_cli.paths, "PROFILE", tmp_path)
        assert apply_cli.salary_for(JOB_ID, "example_primary", "United States") is None


class TestPlanCommand:
    def test_happy_path_prints_a_ready_plan(self, repo, stub_board, capsys):
        repo.write_clean(**{JOB_ID: GH_URL})
        repo.write_state()
        assert apply_cli.main(["plan", JOB_ID]) == 0
        out = capsys.readouterr().out
        assert "READY" in out
        assert "Alex_Example_Resume.pdf" in out
        assert "alex@example.com" in out
        assert stub_board == [GH_URL]

    def test_json_output_is_machine_readable(self, repo, stub_board, capsys):
        repo.write_clean(**{JOB_ID: GH_URL})
        repo.write_state()
        assert apply_cli.main(["plan", JOB_ID, "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["job_id"] == JOB_ID
        assert data["board"] == "bushinggroup"
        assert data["parked"] is False
        assert data["unmapped"] == []
        assert any(f["id"] == "email" for f in data["fields"])
        assert any(f["id"] == "resume" for f in data["files"])
        # react-selects are marked for the post-selection assert fill.py owes.
        assert all(f["assert_selected"] == (f["kind"] == "react_select") for f in data["fields"])

    def test_a_filled_field_carries_what_was_offered_and_what_answered_it(
        self, repo, stub_board, capsys
    ):
        """An answer can only be audited against the alternatives it beat.
        `options` used to reach the JSON for parked fields only, so a keyword
        guess that picked the wrong one of nine options was indistinguishable
        from one that picked right."""
        repo.write_clean(**{JOB_ID: GH_URL})
        repo.write_state()
        assert apply_cli.main(["plan", JOB_ID, "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert all("options" in f and "source" in f for f in data["fields"])
        bank_answered = [f for f in data["fields"] if f["tier"] == "B" and f["source"]]
        assert bank_answered, "no Tier B field in this fixture to check"
        # The source names the bank question, and the keyword when it guessed.
        assert all(f["source"].startswith("bank:") for f in bank_answered)
        # Ashby's real selector, dropped until now.
        assert all("name" in f for f in data["fields"])

    def test_draftable_says_whether_it_blocks(self, repo, stub_board, capsys):
        """`draftable` dropped `required` and `tier`, so the session had to
        infer from the list it came in which fate an unanswered entry had."""
        repo.write_clean(**{JOB_ID: GH_URL})
        repo.write_state()
        assert apply_cli.main(["plan", JOB_ID, "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert all({"required", "tier", "reason"} <= set(d) for d in data["draftable"])

    def test_the_questions_view_groups_by_who_answers(self, repo, stub_board, capsys):
        repo.write_clean(**{JOB_ID: GH_URL})
        repo.write_state()
        assert apply_cli.main(["plan", JOB_ID, "--questions"]) == 0
        out = capsys.readouterr().out
        assert "KNOWN — answered from the bank or your facts" in out
        assert "GUESSED BY KEYWORD — confirm or correct" in out
        assert "NEW — not in the bank yet" in out
        assert "BLOCKING — required and unanswered" in out
        assert "alex@example.com" in out

    def test_url_and_out_dir_overrides_skip_lookup(self, repo, stub_board, capsys):
        # No clean.parquet, no state.yaml: both overrides supplied.
        assert (
            apply_cli.main(["plan", JOB_ID, "--url", GH_URL, "--out-dir", str(repo.out_dir)]) == 0
        )
        assert "READY" in capsys.readouterr().out

    def test_an_expired_posting_exits_2_not_1(self, repo, monkeypatch, capsys):
        repo.write_clean(**{JOB_ID: GH_URL})
        repo.write_state()
        monkeypatch.setattr(apply_cli, "load_board", _raise(PostingExpired("gone (404)")))
        assert apply_cli.main(["plan", JOB_ID]) == 2
        assert "EXPIRED" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "exc",
        [
            AnswersError("profile/application_answers.yaml missing"),
            PlanError("out_dir is not a directory"),
        ],
    )
    def test_config_failures_exit_1_with_the_message(self, repo, monkeypatch, capsys, exc):
        repo.write_clean(**{JOB_ID: GH_URL})
        repo.write_state()
        monkeypatch.setattr(apply_cli, "load_board", _raise(exc))
        assert apply_cli.main(["plan", JOB_ID]) == 1
        assert str(exc) in capsys.readouterr().err

    def test_a_non_greenhouse_role_exits_1_and_says_apply_by_hand(self, repo, stub_board, capsys):
        repo.write_clean(**{JOB_ID: LINKEDIN_URL})
        repo.write_state(url=LINKEDIN_URL)
        assert apply_cli.main(["plan", JOB_ID]) == 1
        assert "by hand" in capsys.readouterr().err
        assert stub_board == []

    def test_nothing_is_written_anywhere(self, repo, stub_board):
        repo.write_clean(**{JOB_ID: GH_URL})
        repo.write_state()
        before = {p: p.read_bytes() for p in repo.pipeline.rglob("*") if p.is_file()}
        listing = sorted(p.name for p in repo.out_dir.iterdir())
        apply_cli.main(["plan", JOB_ID])
        assert {p: p.read_bytes() for p in repo.pipeline.rglob("*") if p.is_file()} == before
        assert sorted(p.name for p in repo.out_dir.iterdir()) == listing

    def test_a_parked_board_still_prints_and_exits_0(self, repo, monkeypatch, capsys):
        """`plan` reports; it does not judge. A parked role is a successful
        report of a park, not a CLI failure."""
        repo.write_clean(**{JOB_ID: GH_URL})
        repo.write_state()
        name = "form_minimal"
        from src.apply.domscan import scan_form
        from src.apply.reconcile import reconcile

        scan = scan_form(load_html(name))
        schema = parse_schema(load_fixture(name))
        monkeypatch.setattr(
            apply_cli,
            "load_board",
            lambda url, timeout=30: BoardForm(
                posting=Posting(token="1000001", url_slug=None),
                slug="gasketworks",
                html="",
                scan=scan,
                schema=schema,
                reconciled=reconcile(scan, schema),
            ),
        )
        assert apply_cli.main(["plan", JOB_ID]) == 0
        out = capsys.readouterr().out
        assert "PARKED" in out and "Nothing would be submitted" in out


class TestPrepareCommand:
    """`/apply` Step 1, consolidated into the CLI: validate prerequisites and
    derive OUT_DIR/VERTICAL/COMPANY_ANSWERS from state.yaml, printing
    shell-quoted `KEY=value` lines for the command session to read."""

    @pytest.fixture(autouse=True)
    def _answers_yaml_exists(self, monkeypatch, tmp_path):
        answers_path = tmp_path / "application_answers.yaml"
        answers_path.write_text("schema_version: 2\n", encoding="utf-8")
        monkeypatch.setattr(apply_cli, "DEFAULT_PATH", answers_path)
        monkeypatch.setattr(
            apply_cli, "EXAMPLE_PATH", tmp_path / "application_answers.example.yaml"
        )
        monkeypatch.setattr(apply_cli, "_playwright_available", lambda: True)

    def test_happy_path_prints_every_field(self, repo, capsys):
        repo.write_state()
        (repo.out_dir / "company_answers.yaml").write_text("why_company: x\n", encoding="utf-8")
        assert apply_cli.main(["prepare", JOB_ID]) == 0
        out = capsys.readouterr().out
        lines = dict(line.split("=", 1) for line in out.strip().splitlines())
        assert lines["JOB_ID"] == JOB_ID
        assert shlex.split(lines["STATE"])[0] == "tailored"
        assert shlex.split(lines["OUT_DIR"])[0] == str(repo.out_dir)
        assert shlex.split(lines["VERTICAL"])[0] == "example_primary"
        assert shlex.split(lines["COMPANY_ANSWERS"])[0] == str(
            repo.out_dir / "company_answers.yaml"
        )
        assert shlex.split(lines["OVERRIDES_FILE"])[0] == str(
            repo.out_dir / "answers_override.json"
        )

    def test_resets_the_overrides_file_to_just_job_id(self, repo, capsys):
        repo.write_state()
        stale = repo.out_dir / "answers_override.json"
        stale.write_text(json.dumps({"job_id": JOB_ID, "leftover_field": "old"}), encoding="utf-8")
        assert apply_cli.main(["prepare", JOB_ID]) == 0
        assert json.loads(stale.read_text(encoding="utf-8")) == {"job_id": JOB_ID}

    def test_no_answers_file_leaves_company_answers_blank(self, repo, capsys):
        repo.write_state()
        assert apply_cli.main(["prepare", JOB_ID]) == 0
        out = capsys.readouterr().out
        lines = dict(line.split("=", 1) for line in out.strip().splitlines())
        assert shlex.split(lines["COMPANY_ANSWERS"])[0] == ""

    def test_missing_state_file_exits_1(self, repo, capsys):
        assert apply_cli.main(["prepare", JOB_ID]) == 1
        assert "state.yaml" in capsys.readouterr().err

    def test_wrong_state_exits_1(self, repo, capsys):
        repo.write_state(state="applied")
        assert apply_cli.main(["prepare", JOB_ID]) == 1
        assert "not 'saved' or 'tailored'" in capsys.readouterr().err

    def test_saved_is_a_valid_entry_state(self, repo, capsys):
        repo.write_state(state="saved")
        assert apply_cli.main(["prepare", JOB_ID]) == 0
        assert "STATE=saved" in capsys.readouterr().out

    def test_no_tailored_dirs_exits_1(self, repo, capsys):
        repo.write_state(tailored_dirs=[])
        assert apply_cli.main(["prepare", JOB_ID]) == 1
        assert "run /tailor" in capsys.readouterr().err

    def test_missing_application_answers_yaml_exits_1(self, repo, monkeypatch, tmp_path, capsys):
        missing = tmp_path / "does_not_exist.yaml"
        monkeypatch.setattr(apply_cli, "DEFAULT_PATH", missing)
        repo.write_state()
        assert apply_cli.main(["prepare", JOB_ID]) == 1
        assert str(missing) in capsys.readouterr().err

    def test_missing_playwright_exits_1(self, repo, monkeypatch, capsys):
        monkeypatch.setattr(apply_cli, "_playwright_available", lambda: False)
        repo.write_state()
        assert apply_cli.main(["prepare", JOB_ID]) == 1
        assert "playwright not installed" in capsys.readouterr().err

    def test_nothing_is_checked_before_state_file_existence(self, repo, monkeypatch, capsys):
        """A missing role should not depend on load order surfacing the wrong
        error first."""
        assert apply_cli.main(["prepare", "no-such-job"]) == 1
        assert "state.yaml" in capsys.readouterr().err


class TestCliShape:
    def test_no_subcommand_is_a_usage_error(self):
        with pytest.raises(SystemExit) as exc:
            apply_cli.main([])
        assert exc.value.code == 2


def _raise(exc):
    def raiser(*args, **kwargs):
        raise exc

    return raiser


# ------------------------------------------------------------------ run: queue


def _fake_plan(
    job_id=JOB_ID,
    company="Bushing Group",
    title="Widget Engineer",
    unmapped=(),
    submit_selector="#application-form button[type=submit]",
    ats="greenhouse",
    requires_captcha=False,
):
    from src.apply.plan import Plan

    return Plan(
        job_id=job_id,
        board="bushinggroup",
        token="1",
        form_url=GH_URL if job_id == JOB_ID else f"{GH_URL}?role={job_id}",
        company=company,
        title=title,
        out_dir=Path("/tmp"),
        fields=(),
        files=(),
        unmapped=tuple(unmapped),
        draftable=(),
        skipped=(),
        submit_selector=submit_selector,
        submit_disabled=False,
        ats=ats,
        requires_captcha=requires_captcha,
    )


def _fake_result(
    *, submitted=False, failures=(), recovered=(), submit_error="", confirmed=False, manifest=None
):
    from src.apply.fill import FillResult

    return FillResult(
        form_url=GH_URL,
        failures=list(failures),
        recovered=list(recovered),
        submitted=submitted,
        submit_error=submit_error,
        confirmed=confirmed,
        manifest=list(manifest or []),
    )


class TestManifest:
    """`--manifest` writes the post-fill read-back. Written only when asked
    for, and only by a run that opened a browser — an unasked-for artifact
    beside the run report would make "no run was started, so no report
    exists" untrue."""

    ROWS = [
        {
            "id": "q1",
            "label": "Start date?",
            "kind": "text",
            "group": "filled",
            "planned": "Immediately.",
            "actual": "September 2026",
            "matches": False,
            "prefilled": True,
            "before": "September 2026",
            "invalid": False,
            "options": [],
        }
    ]

    def test_a_queue_walk_refuses_one_manifest_path(self, repo, tmp_path):
        """Every role would overwrite the last one's read-back."""
        repo.write_state()
        assert apply_cli.main(["run", "--manifest", str(tmp_path / "m.json")]) == 1

    def test_the_run_path_writes_it_for_one_role(self, repo, monkeypatch, tmp_path):
        repo.write_state()
        monkeypatch.setattr(apply_cli, "build", lambda job_id, **kw: (_fake_plan(job_id), None))
        monkeypatch.setattr(
            apply_cli, "run_one", lambda plan, answers, **kw: _fake_result(manifest=self.ROWS)
        )
        monkeypatch.setattr(apply_cli, "APPLY_RUNS", tmp_path / "runs")
        out_path = tmp_path / "m.json"
        assert (
            apply_cli.main(["run", "--job-id", JOB_ID, "--headless", "--manifest", str(out_path)])
            == 0
        )
        assert json.loads(out_path.read_text(encoding="utf-8"))["job_id"] == JOB_ID


class TestEligibleQueue:
    def test_tailored_with_a_dir_is_eligible(self, repo):
        repo.write_state()
        assert apply_cli.eligible_queue(repo.pipeline) == [JOB_ID]

    def test_missing_tailored_dirs_is_not_eligible(self, repo):
        repo.write_state(tailored_dirs=[])
        assert apply_cli.eligible_queue(repo.pipeline) == []

    def test_terminal_states_are_excluded(self, repo):
        for job_id, state in [
            ("aaaa0001", "rejected"),
            ("aaaa0002", "withdrawn"),
            ("aaaa0003", "offer"),
            ("aaaa0004", "ghosted"),
        ]:
            repo.write_state(job_id=job_id, state=state)
        assert apply_cli.eligible_queue(repo.pipeline) == []

    def test_non_tailored_active_states_are_excluded(self, repo):
        for job_id, state in [
            ("bbbb0001", "saved"),
            ("bbbb0002", "applied"),
            ("bbbb0003", "screen"),
        ]:
            repo.write_state(job_id=job_id, state=state)
        assert apply_cli.eligible_queue(repo.pipeline) == []

    def test_a_retitled_role_whose_form_was_sent_is_not_eligible(self, repo):
        """A new id for the same form must not re-enter the queue."""
        repo.write_state(job_id="eeee5555")
        form = "https://boards.greenhouse.io/embed/job_app?token=1000003"
        ledger.append(apply_cli.ledger_path(), JOB_ID, "accepted", form_url=form)
        assert apply_cli.eligible_queue(repo.pipeline) == []

    def test_ordering_is_deterministic_regardless_of_write_order(self, repo):
        for job_id in ("zzzz9999", "aaaa1111", "mmmm5555"):
            repo.write_state(job_id=job_id)
        assert apply_cli.eligible_queue(repo.pipeline) == ["aaaa1111", "mmmm5555", "zzzz9999"]


class TestParseDuration:
    @pytest.mark.parametrize(
        "spec,seconds",
        [
            ("4m", 240.0),
            ("60s", 60.0),
            ("90", 90.0),
            ("1.5h", 5400.0),
            ("0m", 0.0),
        ],
    )
    def test_units(self, spec, seconds):
        assert apply_cli.parse_duration(spec) == seconds

    def test_garbage_raises(self):
        with pytest.raises(apply_cli.ApplyCliError, match="not a duration"):
            apply_cli.parse_duration("soon")


class TestRunQueue:
    def test_the_rate_limiter_is_injected_and_never_sleeps(self, monkeypatch):
        monkeypatch.setattr(apply_cli, "build", _raise(apply_cli.ApplyCliError("nope")))
        sleeps = []
        apply_cli.run_queue(
            ["a1", "a2", "a3"],
            submit=False,
            rate=999,
            jitter=0,
            sleeper=sleeps.append,
            jitter_fn=lambda a, b: 0,
        )
        # 3 roles, 2 gaps between them — never on the first.
        assert len(sleeps) == 2
        assert all(s == 999 for s in sleeps)

    def test_a_parked_role_does_not_stop_the_run(self, monkeypatch):
        monkeypatch.setattr(apply_cli, "build", lambda job_id, **kw: (_fake_plan(job_id), None))
        monkeypatch.setattr(apply_cli, "run_one", lambda plan, answers, **kw: _fake_result())
        outcomes = apply_cli.run_queue(
            ["parked1", "parked2"],
            submit=False,
            sleeper=lambda s: None,
        )
        assert [o.category for o in outcomes] == ["ready", "ready"]

        # Now make the first one park (unmapped, nothing recovered) and confirm
        # the second role still runs.
        def build_with_park(job_id, **kw):
            unmapped = () if job_id == "ok" else [_unmapped()]
            return _fake_plan(job_id, unmapped=unmapped), None

        monkeypatch.setattr(apply_cli, "build", build_with_park)
        outcomes = apply_cli.run_queue(["parked1", "ok"], submit=False, sleeper=lambda s: None)
        assert outcomes[0].category == "parked"
        assert outcomes[1].category == "ready"

    def test_expired_postings_are_their_own_category(self, monkeypatch):
        monkeypatch.setattr(apply_cli, "build", _raise(PostingExpired("gone (404)")))
        outcomes = apply_cli.run_queue(["x"], submit=False, sleeper=lambda s: None)
        assert outcomes[0].category == "expired"

    def test_a_successful_submit_calls_track_cli_not_state_io(self, monkeypatch):
        track_calls = []
        monkeypatch.setattr(apply_cli, "build", lambda job_id, **kw: (_fake_plan(job_id), None))
        monkeypatch.setattr(
            apply_cli, "run_one", lambda plan, answers, **kw: _fake_result(submitted=True)
        )
        monkeypatch.setattr(apply_cli.track_cli, "main", track_calls.append)
        # state_io.transition must never be reached from this path.
        monkeypatch.setattr(
            apply_cli.state_io,
            "transition",
            _raise(AssertionError("state_io.transition must not be called")),
        )

        outcomes = apply_cli.run_queue([JOB_ID], submit=True, sleeper=lambda s: None)

        # No confirmation marker was seen, so the submission is reported as
        # unconfirmed — but it still transitions, because a duplicate
        # application is worse than a state row that needs an eyeball.
        assert outcomes[0].category == "submitted_unconfirmed"
        assert track_calls == [
            [JOB_ID, "applied", "--note", "auto-submitted via /apply (bushinggroup)"]
        ]

    def test_a_recovered_field_is_no_longer_blocking(self, monkeypatch):
        u = type("U", (), {"id": "hispanic_ethnicity"})()
        monkeypatch.setattr(
            apply_cli, "build", lambda job_id, **kw: (_fake_plan(job_id, unmapped=[u]), None)
        )
        monkeypatch.setattr(
            apply_cli,
            "run_one",
            lambda plan, answers, **kw: _fake_result(recovered=["hispanic_ethnicity"]),
        )
        outcomes = apply_cli.run_queue([JOB_ID], submit=False, sleeper=lambda s: None)
        assert outcomes[0].category == "ready"

    def test_a_fill_failure_is_a_failed_category(self, monkeypatch):
        monkeypatch.setattr(apply_cli, "build", lambda job_id, **kw: (_fake_plan(job_id), None))
        monkeypatch.setattr(
            apply_cli,
            "run_one",
            lambda plan, answers, **kw: _fake_result(failures=["country: no option matching"]),
        )
        outcomes = apply_cli.run_queue([JOB_ID], submit=False, sleeper=lambda s: None)
        assert outcomes[0].category == "failed"
        assert "country" in outcomes[0].detail


class TestRunReport:
    def test_render_groups_by_category(self):
        from src.apply_cli import RunOutcome

        outcomes = [
            RunOutcome("a1", "A Co", "Eng", "submitted"),
            RunOutcome(
                "a2",
                "B Co",
                "Eng",
                "parked",
                detail="required unresolved",
                unmapped=(
                    {
                        "id": "why_us",
                        "tier": "C",
                        "label": "Why us?",
                        "reason": "no question in the bank asks this",
                        "source": "",
                        "options": ["Yes", "No"],
                        "description": "",
                    },
                ),
            ),
            RunOutcome("a3", "C Co", "Eng", "failed", detail="boom"),
        ]
        text = apply_cli.render_report(outcomes, apply_cli.datetime(2026, 1, 1))
        assert "## Submitted (1)" in text
        assert "## Parked (1)" in text
        assert "why_us" in text
        # The label, the reason and the offered options, not just the field id:
        # a bare id names nothing the next pass can act on.
        assert "Why us?" in text
        assert "no question in the bank asks this" in text
        assert "offers: Yes, No" in text
        assert "## Failed (1)" in text
        assert "boom" in text

    def test_write_report_creates_a_dated_file(self, tmp_path):
        from src.apply_cli import RunOutcome

        out_dir = tmp_path / "apply_runs"
        path = apply_cli.write_report(
            [RunOutcome("a1", "A Co", "Eng", "submitted")],
            apply_cli.datetime(2026, 1, 1, 9, 30, 0),
            out_dir=out_dir,
        )
        assert path.parent == out_dir
        assert path.read_text(encoding="utf-8").startswith("# apply run")


class TestRunCommand:
    def test_nothing_eligible_exits_0(self, repo, capsys):
        assert apply_cli.main(["run"]) == 0
        assert "Nothing eligible" in capsys.readouterr().out

    def test_default_path_never_submits(self, repo, monkeypatch, tmp_path):
        repo.write_state()
        monkeypatch.setattr(apply_cli, "APPLY_RUNS", tmp_path / "apply_runs")
        monkeypatch.setattr(apply_cli, "build", lambda job_id, **kw: (_fake_plan(job_id), None))
        submit_after_seen = []
        monkeypatch.setattr(
            apply_cli,
            "run_one",
            lambda plan, answers, **kw: (
                submit_after_seen.append(kw.get("submit_after")),
                _fake_result(),
            )[1],
        )
        assert apply_cli.main(["run", "--rate", "0s", "--jitter", "0s"]) == 0
        assert submit_after_seen == [False]

    def test_job_id_override_runs_one_role_even_off_queue_order(self, repo, monkeypatch, tmp_path):
        repo.write_state()
        monkeypatch.setattr(apply_cli, "APPLY_RUNS", tmp_path / "apply_runs")
        monkeypatch.setattr(apply_cli, "build", lambda job_id, **kw: (_fake_plan(job_id), None))
        monkeypatch.setattr(apply_cli, "run_one", lambda plan, answers, **kw: _fake_result())
        assert apply_cli.main(["run", "--job-id", JOB_ID, "--rate", "0s"]) == 0

    def test_unknown_job_id_is_an_error(self, repo, capsys):
        assert apply_cli.main(["run", "--job-id", "nope"]) == 1
        assert "not eligible" in capsys.readouterr().err

    def test_limit_caps_the_queue(self, repo, monkeypatch, tmp_path):
        for job_id in ("aaaa1111", "bbbb2222"):
            repo.write_state(job_id=job_id)
        monkeypatch.setattr(apply_cli, "APPLY_RUNS", tmp_path / "apply_runs")
        seen = []

        def build_and_record(job_id, **kw):
            seen.append(job_id)
            return _fake_plan(job_id), None

        monkeypatch.setattr(apply_cli, "build", build_and_record)
        monkeypatch.setattr(apply_cli, "run_one", lambda plan, answers, **kw: _fake_result())
        assert apply_cli.main(["run", "--limit", "1", "--rate", "0s"]) == 0
        assert len(seen) == 1

    def test_a_failed_role_exits_1_and_a_report_is_written(self, repo, monkeypatch, tmp_path):
        repo.write_state()
        out_dir = tmp_path / "apply_runs"
        monkeypatch.setattr(apply_cli, "APPLY_RUNS", out_dir)
        monkeypatch.setattr(apply_cli, "build", lambda job_id, **kw: (_fake_plan(job_id), None))
        monkeypatch.setattr(
            apply_cli,
            "run_one",
            lambda plan, answers, **kw: _fake_result(failures=["boom"]),
        )
        assert apply_cli.main(["run", "--rate", "0s"]) == 1
        assert list(out_dir.glob("*.md"))


class TestASubmissionIsNeverSilentlyLost:
    """The three ways a run used to submit without leaving a usable trace."""

    def test_a_confirmed_submit_is_plain_submitted(self, monkeypatch):
        monkeypatch.setattr(apply_cli, "build", lambda job_id, **kw: (_fake_plan(job_id), None))
        monkeypatch.setattr(
            apply_cli,
            "run_one",
            lambda plan, answers, **kw: _fake_result(submitted=True, confirmed=True),
        )
        monkeypatch.setattr(apply_cli.track_cli, "main", lambda argv: 0)
        out = apply_cli.run_queue([JOB_ID], submit=True, sleeper=lambda s: None)
        assert out[0].category == "submitted"

    def test_track_cli_returning_nonzero_is_not_reported_as_a_clean_submit(self, monkeypatch):
        # track_cli.main RETURNS 1 on a rejected transition, it does not raise.
        # Reporting "submitted" here leaves the role at `tailored`, so the next
        # --submit run applies to the same board a second time.
        monkeypatch.setattr(apply_cli, "build", lambda job_id, **kw: (_fake_plan(job_id), None))
        monkeypatch.setattr(
            apply_cli, "run_one", lambda plan, answers, **kw: _fake_result(submitted=True)
        )
        monkeypatch.setattr(apply_cli.track_cli, "main", lambda argv: 1)
        out = apply_cli.run_queue([JOB_ID], submit=True, sleeper=lambda s: None)
        assert out[0].category == "submitted_untracked"
        assert "still says tailored" in out[0].detail

    def test_track_cli_raising_does_not_abort_the_run_or_lose_the_submission(self, monkeypatch):
        monkeypatch.setattr(apply_cli, "build", lambda job_id, **kw: (_fake_plan(job_id), None))
        monkeypatch.setattr(
            apply_cli, "run_one", lambda plan, answers, **kw: _fake_result(submitted=True)
        )
        monkeypatch.setattr(apply_cli.track_cli, "main", _raise(OSError("disk full")))
        out = apply_cli.run_queue([JOB_ID, "second"], submit=True, sleeper=lambda s: None)
        assert out[0].category == "submitted_untracked"
        assert "OSError" in out[0].detail
        assert len(out) == 2, "the second role must still run"

    def test_a_driver_error_is_isolated_to_its_role(self, monkeypatch):
        monkeypatch.setattr(apply_cli, "build", lambda job_id, **kw: (_fake_plan(job_id), None))

        def explode(plan, answers, **kw):
            if plan.job_id == "boom":
                raise TimeoutError("element not found")
            return _fake_result()

        monkeypatch.setattr(apply_cli, "run_one", explode)
        out = apply_cli.run_queue(["boom", "fine"], submit=False, sleeper=lambda s: None)
        assert out[0].category == "failed"
        assert "TimeoutError" in out[0].detail
        assert out[1].category == "ready"


class TestManualApplyIsItsOwnCategory:
    def test_a_board_with_no_driver_is_manual_not_failed(self, monkeypatch):
        """Every registered board has a driver now, so this needs an ats that
        never will — the category still has to exist for LinkedIn, Indeed and
        company careers pages."""
        monkeypatch.setattr(
            apply_cli, "build", lambda job_id, **kw: (_fake_plan(job_id, ats="workday"), None)
        )
        monkeypatch.setattr(apply_cli, "run_one", _raise(AssertionError("must not open a browser")))
        out = apply_cli.run_queue([JOB_ID], submit=True, sleeper=lambda s: None)
        assert out[0].category == "manual"
        assert "apply by hand" in out[0].detail

    def test_a_manual_apply_url_is_manual_not_failed(self, monkeypatch):
        monkeypatch.setattr(
            apply_cli,
            "build",
            _raise(apply_cli.ManualApplyOnly("workday — apply by hand")),
        )
        out = apply_cli.run_queue([JOB_ID], submit=False, sleeper=lambda s: None)
        assert out[0].category == "manual"


class TestTheReportSurvivesACrash:
    def test_the_report_holds_every_completed_role_when_the_walk_dies(self, monkeypatch, tmp_path):
        # The report is the only record of what went out. A crash on role 3
        # must not erase roles 1 and 2.
        monkeypatch.setattr(apply_cli, "build", lambda job_id, **kw: (_fake_plan(job_id), None))

        def explode(plan, answers, **kw):
            if plan.job_id == "third":
                raise RuntimeError("browser died")
            return _fake_result(submitted=True, confirmed=True)

        monkeypatch.setattr(apply_cli, "run_one", explode)
        monkeypatch.setattr(apply_cli.track_cli, "main", lambda argv: 0)

        collected: list = []
        apply_cli.run_queue(
            ["first", "second", "third"],
            submit=True,
            sleeper=lambda s: None,
            collect_into=collected,
        )
        assert [o.category for o in collected] == [
            "submitted",
            "submitted",
            "failed",
        ]
        started = apply_cli.datetime(2026, 8, 10, 12, 0, 0)
        text = apply_cli.write_report(collected, started, tmp_path).read_text(encoding="utf-8")
        assert "first" in text and "second" in text and "third" in text


class TestOverridesAreBoundToOneRole:
    def _write(self, tmp_path, data):
        p = tmp_path / "answers.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_a_file_drafted_for_another_role_is_refused(self, tmp_path):
        p = self._write(
            tmp_path,
            {"job_id": "other123", "question_1": {"value": "Because I admire their work."}},
        )
        with pytest.raises(apply_cli.ApplyCliError, match="drafted for job_id"):
            apply_cli.load_overrides(p, JOB_ID)

    def test_a_matching_job_id_loads(self, tmp_path):
        p = self._write(
            tmp_path,
            {
                "job_id": JOB_ID,
                "question_1": {
                    "value": "An answer.",
                    "source": "bullets:B-1",
                    "question": "project_proud",
                },
            },
        )
        loaded = apply_cli.load_overrides(p, JOB_ID)
        assert loaded["question_1"].value == "An answer."
        assert loaded["question_1"].question == "project_proud"

    def test_a_file_without_a_job_id_still_loads(self, tmp_path):
        p = self._write(tmp_path, {"question_1": {"value": "x"}})
        assert apply_cli.load_overrides(p, JOB_ID)["question_1"].value == "x"

    def test_a_list_value_is_a_tuple(self, tmp_path):
        p = self._write(tmp_path, {"q": {"value": ["a", "b"]}})
        assert apply_cli.load_overrides(p, JOB_ID)["q"].value == ("a", "b")

    @pytest.mark.parametrize("value", ["", "   ", [], [""], [1], 3, None])
    def test_a_bad_value_is_refused_by_name(self, tmp_path, value):
        p = self._write(tmp_path, {"q": {"value": value}})
        with pytest.raises(apply_cli.ApplyCliError, match="'q'"):
            apply_cli.load_overrides(p, JOB_ID)

    def test_an_unknown_key_is_refused(self, tmp_path):
        p = self._write(tmp_path, {"q": {"value": "x", "tier": "C1"}})
        with pytest.raises(apply_cli.ApplyCliError, match="unknown keys"):
            apply_cli.load_overrides(p, JOB_ID)

    def test_a_claim_must_state_booleans(self, tmp_path):
        p = self._write(tmp_path, {"q": {"value": "x", "claim": {"authorized": "yes"}}})
        with pytest.raises(apply_cli.ApplyCliError, match="true or false"):
            apply_cli.load_overrides(p, JOB_ID)


class TestTheReportNeverOverwritesAnotherRun:
    def test_two_runs_in_the_same_second_get_separate_files(self, tmp_path):
        started = apply_cli.datetime(2026, 8, 10, 12, 0, 0)
        first = apply_cli.reserve_report_path(started, tmp_path)
        apply_cli.write_report([], started, path=first)
        second = apply_cli.reserve_report_path(started, tmp_path)
        assert first != second
        assert first.exists()

    def test_per_role_writes_reuse_the_reserved_path(self, tmp_path):
        started = apply_cli.datetime(2026, 8, 10, 12, 0, 0)
        path = apply_cli.reserve_report_path(started, tmp_path)
        for _ in range(3):
            apply_cli.write_report([], started, path=path)
        assert len(list(tmp_path.glob("*.md"))) == 1


class TestHeadlessAndCaptchaAreRefusedTogether:
    def test_a_captcha_board_headless_with_submit_is_refused_before_the_browser(self, monkeypatch):
        monkeypatch.setattr(
            apply_cli,
            "build",
            lambda job_id, **kw: (_fake_plan(job_id, ats="lever", requires_captcha=True), None),
        )
        monkeypatch.setattr(apply_cli, "run_one", _raise(AssertionError("must not open a browser")))
        out = apply_cli.run_queue([JOB_ID], submit=True, headless=True, sleeper=lambda s: None)
        assert out[0].category == "failed"
        assert "nobody to" in out[0].detail

    def test_headed_is_fine(self, monkeypatch):
        monkeypatch.setattr(
            apply_cli,
            "build",
            lambda job_id, **kw: (_fake_plan(job_id, ats="lever", requires_captcha=True), None),
        )
        monkeypatch.setattr(apply_cli, "run_one", lambda plan, answers, **kw: _fake_result())
        out = apply_cli.run_queue([JOB_ID], submit=True, headless=False, sleeper=lambda s: None)
        assert out[0].category == "ready"


class TestACrashAfterTheClickStillRecordsTheSubmission:
    """The click is the irreversible act. `run_one` publishes its FillResult
    into `sink` before the browser opens, so an exception anywhere after the
    click — including in browser teardown, or a Ctrl-C — cannot erase the fact
    that an application went out. Losing it leaves the role `tailored` and the
    next run applies to the same board again.
    """

    def _plan_only(self, monkeypatch):
        monkeypatch.setattr(apply_cli, "build", lambda job_id, **kw: (_fake_plan(job_id), None))
        monkeypatch.setattr(apply_cli.track_cli, "main", lambda argv: 0)

    def test_teardown_blowing_up_after_a_submit_is_still_a_submission(self, monkeypatch):
        self._plan_only(monkeypatch)

        def submit_then_die(plan, answers, sink=None, **kw):
            sink.append(_fake_result(submitted=True))
            raise RuntimeError("Target page has been closed")

        monkeypatch.setattr(apply_cli, "run_one", submit_then_die)
        out = apply_cli.run_queue([JOB_ID], submit=True, sleeper=lambda s: None)
        assert out[0].category == "submitted_unconfirmed"
        assert "SUBMITTED" in out[0].detail

    def test_a_keyboard_interrupt_after_a_submit_is_recorded_then_reraised(self, monkeypatch):
        self._plan_only(monkeypatch)

        def submit_then_interrupt(plan, answers, sink=None, **kw):
            sink.append(_fake_result(submitted=True))
            raise KeyboardInterrupt

        monkeypatch.setattr(apply_cli, "run_one", submit_then_interrupt)
        out = apply_cli._run_role(JOB_ID, submit=True, headless=False)
        assert out.category == "submitted_unconfirmed"

    def test_a_failed_state_write_on_the_crash_path_is_submitted_untracked(self, monkeypatch):
        # Same bar as the non-crash path: a refused transition leaves the role
        # `tailored`, so the next --submit run applies to the same board again.
        monkeypatch.setattr(apply_cli, "build", lambda job_id, **kw: (_fake_plan(job_id), None))
        monkeypatch.setattr(apply_cli.track_cli, "main", lambda argv: 1)

        def submit_then_die(plan, answers, sink=None, **kw):
            sink.append(_fake_result(submitted=True))
            raise RuntimeError("Target page has been closed")

        monkeypatch.setattr(apply_cli, "run_one", submit_then_die)
        out = apply_cli.run_queue([JOB_ID], submit=True, sleeper=lambda s: None)
        assert out[0].category == "submitted_untracked"
        assert "still says tailored" in out[0].detail

    def test_a_crash_with_nothing_submitted_is_an_ordinary_failure(self, monkeypatch):
        self._plan_only(monkeypatch)

        def die_early(plan, answers, sink=None, **kw):
            sink.append(_fake_result(submitted=False))
            raise TimeoutError("element not found")

        monkeypatch.setattr(apply_cli, "run_one", die_early)
        out = apply_cli.run_queue([JOB_ID], submit=True, sleeper=lambda s: None)
        assert out[0].category == "failed"

    def test_a_keyboard_interrupt_with_nothing_submitted_propagates(self, monkeypatch):
        self._plan_only(monkeypatch)

        def interrupt(plan, answers, sink=None, **kw):
            sink.append(_fake_result(submitted=False))
            raise KeyboardInterrupt

        monkeypatch.setattr(apply_cli, "run_one", interrupt)
        with pytest.raises(KeyboardInterrupt):
            apply_cli._run_role(JOB_ID, submit=True, headless=False)


def _unmapped(field_id="why_us"):
    from src.apply.plan import Unmapped

    return Unmapped(
        id=field_id,
        label="Why us?",
        required=True,
        kind="textarea",
        section="questions",
        tier="C",
        reason="no question in the bank asks this",
    )


class TestSubmitRequiresAnExplicitBound:
    """`--submit` with no bound walks the whole eligible queue and applies to
    every role in it. The check runs before anything is loaded, so a mistyped
    command cannot send a single application."""

    def test_submit_without_limit_exits_1_and_opens_no_browser(
        self, repo, monkeypatch, tmp_path, capsys
    ):
        repo.write_state()
        runs = tmp_path / "apply_runs"
        monkeypatch.setattr(apply_cli, "APPLY_RUNS", runs)
        planned: list[str] = []
        monkeypatch.setattr(
            apply_cli,
            "build",
            lambda job_id, **kw: (planned.append(job_id), (_fake_plan(job_id), None))[1],
        )
        monkeypatch.setattr(apply_cli, "run_one", _raise(AssertionError("must not open a browser")))

        assert apply_cli.main(["run", "--submit"]) == 1
        assert "--limit" in capsys.readouterr().err
        assert planned == [], "nothing may even be fetched"
        assert not runs.exists(), "no run was started, so no report belongs to one"

    def test_unattended_refuses_submit_and_bank_writes(self, repo, monkeypatch, tmp_path, capsys):
        import shutil

        repo.write_state()
        monkeypatch.setenv("APPLY_UNATTENDED", "1")
        monkeypatch.setattr(apply_cli, "run_one", _raise(AssertionError("must not open a browser")))
        assert apply_cli.main(["run", "--job-id", JOB_ID, "--submit", "--yes"]) == 1
        assert "APPLY_UNATTENDED=1" in capsys.readouterr().err

        bank_path = tmp_path / "bank.yaml"
        shutil.copy(FIXTURES / "application_answers.yaml", bank_path)
        before = bank_path.read_text(encoding="utf-8")
        assert apply_cli.main(["learn", "--path", str(bank_path), "--undo", "pronouns"]) == 1
        assert "APPLY_UNATTENDED=1" in capsys.readouterr().err
        assert bank_path.read_text(encoding="utf-8") == before

    def test_without_submit_no_limit_is_needed(self, repo, monkeypatch, tmp_path):
        # The bound is on *submitting*, not on walking. A fill-and-stop run
        # sends nothing, so it stays unbounded.
        repo.write_state()
        monkeypatch.setattr(apply_cli, "APPLY_RUNS", tmp_path / "apply_runs")
        monkeypatch.setattr(apply_cli, "build", lambda job_id, **kw: (_fake_plan(job_id), None))
        monkeypatch.setattr(apply_cli, "run_one", lambda plan, answers, **kw: _fake_result())
        assert apply_cli.main(["run", "--rate", "0s", "--jitter", "0s"]) == 0

    def test_job_id_is_exempt_because_one_named_role_is_already_a_bound(
        self, repo, monkeypatch, tmp_path
    ):
        repo.write_state()
        monkeypatch.setattr(apply_cli, "APPLY_RUNS", tmp_path / "apply_runs")
        monkeypatch.setattr(apply_cli, "build", lambda job_id, **kw: (_fake_plan(job_id), None))
        seen: list = []
        monkeypatch.setattr(
            apply_cli,
            "run_one",
            lambda plan, answers, **kw: (
                seen.append((plan.job_id, kw.get("submit_after"))),
                _fake_result(),
            )[1],
        )
        assert apply_cli.main(["run", "--submit", "--yes", "--job-id", JOB_ID, "--rate", "0s"]) == 0
        assert seen == [(JOB_ID, True)]


class TestTheRateFloor:
    """A hard floor, never a config key: `--rate` paces requests at other
    people's employer boards, so it is not the operator's to turn down."""

    def test_the_floor_is_thirty_seconds(self):
        assert apply_cli.MIN_RATE_SECONDS == 30.0

    def test_zero_is_raised_to_the_floor_and_says_so(self, capsys):
        assert apply_cli.clamp_rate(0) == 30.0
        err = capsys.readouterr().err
        assert "below the 30s floor" in err

    def test_a_value_just_under_is_raised_too(self, capsys):
        assert apply_cli.clamp_rate(29.9) == 30.0
        assert "WARNING" in capsys.readouterr().err

    def test_a_value_above_the_floor_is_untouched_and_silent(self, capsys):
        assert apply_cli.clamp_rate(240.0) == 240.0
        assert capsys.readouterr().err == ""

    def test_the_floor_is_what_the_queue_walk_actually_paces_with(
        self, repo, monkeypatch, tmp_path
    ):
        """Defining the clamp is not the same as wiring it in — `--rate 0s`
        has to arrive at `run_queue` as 30, not 0."""
        repo.write_state()
        monkeypatch.setattr(apply_cli, "APPLY_RUNS", tmp_path / "apply_runs")
        recorded: dict = {}

        def record(job_ids, **kw):
            recorded.update(kw)
            return []

        monkeypatch.setattr(apply_cli, "run_queue", record)
        assert apply_cli.main(["run", "--rate", "0s", "--jitter", "0s"]) == 0
        assert recorded["rate"] == 30.0


class TestOneSubmissionPerCompanyPerRun:
    """Two roles at one company in one run reads to the company as a script,
    not a candidate. The cap is on *submitting*, so the second role never
    opens a browser at all and lands in its own report category — not
    `failed`, which is for breakage."""

    def _stub(self, monkeypatch, plan_companies):
        opened: list[str] = []
        monkeypatch.setattr(
            apply_cli,
            "build",
            lambda job_id, **kw: (_fake_plan(job_id, company=plan_companies[job_id]), None),
        )
        monkeypatch.setattr(
            apply_cli,
            "run_one",
            lambda plan, answers, **kw: (
                opened.append(plan.job_id),
                _fake_result(submitted=True, confirmed=True),
            )[1],
        )
        monkeypatch.setattr(apply_cli.track_cli, "main", lambda argv: 0)
        return opened

    def test_the_second_role_at_one_company_is_skipped_and_never_opened(self, monkeypatch):
        opened = self._stub(
            monkeypatch, {"a1": "Bushing Group", "a2": "Bushing Group", "b1": "Gasket Works"}
        )
        sleeps: list[float] = []
        out = apply_cli.run_queue(
            ["a1", "a2", "b1"],
            submit=True,
            rate=30,
            jitter=0,
            sleeper=sleeps.append,
            jitter_fn=lambda a, b: 0,
            companies={"a1": "Bushing Group", "a2": "Bushing Group", "b1": "Gasket Works"},
        )
        assert [o.category for o in out] == ["submitted", "skipped", "submitted"]
        assert opened == ["a1", "b1"]
        # Only one gap: before b1. A role nothing was attempted for must not
        # buy the next one a four-minute wait.
        assert sleeps == [30]

    def test_the_skipped_role_says_which_company_capped_it(self, monkeypatch):
        self._stub(monkeypatch, {"a1": "Bushing Group", "a2": "Bushing Group"})
        out = apply_cli.run_queue(
            ["a1", "a2"],
            submit=True,
            sleeper=lambda s: None,
            companies={"a1": "Bushing Group", "a2": "Bushing Group"},
        )
        assert out[1].company == "Bushing Group"
        assert "already submitted to this company" in out[1].detail

    def test_a_role_that_did_not_submit_does_not_cap_its_company(self, monkeypatch):
        # Only a submission spends the company's one slot. A parked or failed
        # first role must leave the second one applyable.
        monkeypatch.setattr(apply_cli, "build", lambda job_id, **kw: (_fake_plan(job_id), None))
        opened: list[str] = []
        monkeypatch.setattr(
            apply_cli,
            "run_one",
            lambda plan, answers, **kw: (
                opened.append(plan.job_id),
                _fake_result(failures=["boom"]),
            )[1],
        )
        out = apply_cli.run_queue(
            ["a1", "a2"],
            submit=True,
            sleeper=lambda s: None,
            companies={"a1": "Bushing Group", "a2": "Bushing Group"},
        )
        assert opened == ["a1", "a2"]
        assert [o.category for o in out] == ["failed", "failed"]

    def test_a_fill_and_stop_walk_is_not_capped(self, monkeypatch):
        # Nothing is sent without --submit, so there is nothing to cap.
        opened = self._stub(monkeypatch, {"a1": "Bushing Group", "a2": "Bushing Group"})
        apply_cli.run_queue(
            ["a1", "a2"],
            submit=False,
            sleeper=lambda s: None,
            companies={"a1": "Bushing Group", "a2": "Bushing Group"},
        )
        assert opened == ["a1", "a2"]

    def test_a_differently_spelled_company_is_still_the_same_company(self, monkeypatch):
        # state.yaml carries whatever each board called itself. Comparing the
        # raw strings would let "Bushing Group, Inc." and "The Bushing Group
        # LLC" both submit.
        opened = self._stub(
            monkeypatch, {"a1": "Bushing Group, Inc.", "a2": "The Bushing Group LLC"}
        )
        out = apply_cli.run_queue(
            ["a1", "a2"],
            submit=True,
            sleeper=lambda s: None,
            companies={"a1": "Bushing Group, Inc.", "a2": "The Bushing Group LLC"},
        )
        assert [o.category for o in out] == ["submitted", "skipped"]
        assert opened == ["a1"]

    def test_the_plan_company_counts_even_when_the_index_carried_none(self, monkeypatch):
        # state.yaml can be missing `company:` entirely; the board's own name
        # is only known once the plan is built. Both spellings are counted, so
        # a blank index entry does not hand the company a second submission.
        opened = self._stub(monkeypatch, {"a1": "Bushing Group, Inc.", "a2": "Bushing Group"})
        out = apply_cli.run_queue(
            ["a1", "a2"],
            submit=True,
            sleeper=lambda s: None,
            companies={"a1": "", "a2": "The Bushing Group LLC"},
        )
        assert [o.category for o in out] == ["submitted", "skipped"]
        assert opened == ["a1"]

    def test_the_report_gives_skipped_its_own_section(self):
        from src.apply_cli import RunOutcome

        text = apply_cli.render_report(
            [
                RunOutcome(
                    "a2",
                    "Bushing Group",
                    "Eng",
                    "skipped",
                    detail="already submitted to this company in this run",
                )
            ],
            apply_cli.datetime(2026, 1, 1),
        )
        assert "one submission per company per run" in text
        assert "## Failed" not in text

    def test_a_skipped_role_does_not_make_the_run_exit_nonzero(self):
        assert "skipped" not in apply_cli._EXIT_NONZERO


class TestQueueCompanies:
    def test_the_company_comes_from_state_yaml(self, repo):
        repo.write_state(job_id="aaaa1111")
        repo.write_state(job_id="bbbb2222")
        got = apply_cli.queue_companies(["aaaa1111", "bbbb2222", "nosuchrole"], repo.pipeline)
        assert got == {"aaaa1111": "Bushing Group", "bbbb2222": "Bushing Group", "nosuchrole": ""}


class TestLearnCommand:
    """`apply learn` reviews and undoes; the run itself does the learning."""

    def _bank(self, tmp_path):
        import shutil

        path = tmp_path / "bank.yaml"
        shutil.copy(FIXTURES / "application_answers.yaml", path)
        return path

    def test_review_lists_every_question(self, tmp_path, capsys):
        assert apply_cli.main(["learn", "--path", str(self._bank(tmp_path))]) == 0
        out = capsys.readouterr().out
        assert "linkedin:" in out and "~ linkedin" in out

    def test_undo_removes_a_question(self, tmp_path, capsys):
        path = self._bank(tmp_path)
        assert apply_cli.main(["learn", "--path", str(path), "--undo", "pronouns"]) == 0
        assert "pronouns" not in yaml.safe_load(path.read_text(encoding="utf-8"))["questions"]

    def test_undo_of_an_unknown_question_fails(self, tmp_path, capsys):
        assert apply_cli.main(["learn", "--path", str(self._bank(tmp_path)), "--undo", "nope"]) == 1
        assert "no question" in capsys.readouterr().err


class TestApplyConfirm:
    """Only `apply confirm`'s stored hash confirms a file, and only while it matches."""

    @pytest.fixture(autouse=True)
    def _no_clean(self, tmp_path, monkeypatch):
        # confirm builds the role's plan to list its guesses; never from real data.
        monkeypatch.setattr(apply_cli, "CLEAN", tmp_path / "missing.parquet")

    def _file(self, tmp_path, data):
        path = tmp_path / "answers_override.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_a_typed_yes_confirms_and_a_later_edit_does_not(self, tmp_path, monkeypatch):
        data = {"job_id": JOB_ID, "q": {"value": "x", "question": "q1"}}
        path = self._file(tmp_path, data)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
        assert apply_cli.main(["confirm", str(path)]) == 0
        assert apply_cli.answers_confirmed(path)
        self._file(tmp_path, {**data, "q": {"value": "y", "question": "q1"}})
        assert not apply_cli.answers_confirmed(path)

    def test_a_session_flag_or_a_non_tty_never_confirms(self, tmp_path, monkeypatch):
        path = self._file(tmp_path, {"job_id": JOB_ID, "confirmed": True, "q": {"value": "x"}})
        assert not apply_cli.answers_confirmed(path)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr("builtins.input", _raise(AssertionError("must not prompt")))
        assert apply_cli.main(["confirm", str(path)]) == 1
        assert not confirmation.sidecar(path).exists()

    def test_the_banks_standing_keyword_guesses_are_shown(self, tmp_path, monkeypatch, capsys):
        """Confirming lets them through a submit, so the user must see them."""
        from dataclasses import replace

        from src.apply.plan import FieldPlan

        guess = FieldPlan(
            id="q",
            name="q",
            label="What's your LinkedIn?",
            kind="text",
            section="questions",
            required=True,
            multi=False,
            value="https://x",
            tier="B",
            source="bank:linkedin~linkedin",
        )
        monkeypatch.setattr(
            apply_cli,
            "build",
            lambda job_id, **kw: (replace(_fake_plan(job_id), fields=(guess,)), None),
        )
        path = self._file(tmp_path, {"job_id": JOB_ID})
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        assert apply_cli.main(["confirm", str(path)]) == 1
        out = capsys.readouterr().out
        assert "What's your LinkedIn?" in out and "bank:linkedin~linkedin" in out


class TestARunLearnsWhatWasConfirmed:
    """`run --answers` saves every mapping the session made and every keyword
    guess that went unchallenged, before the browser opens."""

    def _bank(self, tmp_path, monkeypatch):
        import shutil

        from src.apply import bank

        path = tmp_path / "bank.yaml"
        shutil.copy(FIXTURES / "application_answers.yaml", path)
        monkeypatch.setattr(bank, "DEFAULT_PATH", path)
        return path

    def _plan(self, fields):
        from dataclasses import replace

        return replace(_fake_plan(JOB_ID), fields=tuple(fields))

    def _field(self, fid, label, value, source="", options=(), replaced=""):
        from src.apply.plan import FieldPlan

        return FieldPlan(
            id=fid,
            name=fid,
            label=label,
            kind="select" if options else "text",
            section="questions",
            required=True,
            multi=False,
            value=value,
            tier="C",
            options=tuple(options),
            source=source,
            replaced=replaced,
        )

    def _run(self, tmp_path, monkeypatch, plan, overrides):
        answers_path = tmp_path / "answers.json"
        data = {"job_id": JOB_ID, **overrides}
        answers_path.write_text(json.dumps(data), encoding="utf-8")
        confirmation.record(answers_path, data)
        monkeypatch.setattr(apply_cli, "build", lambda job_id, **kw: (plan, None))
        monkeypatch.setattr(apply_cli, "run_one", lambda plan, answers, **kw: _fake_result())
        return apply_cli.run_queue(
            [JOB_ID], submit=False, sleeper=lambda s: None, answers_path=answers_path
        )[0]

    def test_a_mapped_wording_joins_its_question(self, tmp_path, monkeypatch):
        path = self._bank(tmp_path, monkeypatch)
        plan = self._plan(
            [self._field("q", "Your LinkedIn URL please", "https://x", source="override")]
        )
        out = self._run(
            tmp_path, monkeypatch, plan, {"q": {"value": "https://x", "question": "linkedin"}}
        )
        bank_data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert "Your LinkedIn URL please" in bank_data["questions"]["linkedin"]["wordings"]
        assert out.learned

    def test_a_new_question_is_created_with_its_answer(self, tmp_path, monkeypatch):
        path = self._bank(tmp_path, monkeypatch)
        label = "Tell us about a project you're proud of"
        plan = self._plan([self._field("q", label, "I built a widget.", source="override")])
        self._run(
            tmp_path,
            monkeypatch,
            plan,
            {
                "q": {
                    "value": "I built a widget.",
                    "question": "project_proud",
                    "source": "bullets:B-1",
                }
            },
        )
        q = yaml.safe_load(path.read_text(encoding="utf-8"))["questions"]["project_proud"]
        assert q["answer"] == "I built a widget." and q["wordings"] == [label]
        assert q["source"] == "bullets:B-1"

    def test_a_why_us_answer_never_becomes_a_bank_answer(self, tmp_path, monkeypatch):
        path = self._bank(tmp_path, monkeypatch)
        plan = self._plan([self._field("q", "Why Acme?", "Because Acme.", source="override")])
        self._run(
            tmp_path,
            monkeypatch,
            plan,
            {"q": {"value": "Because Acme.", "question": "why_acme", "source": "company_answers"}},
        )
        assert "why_acme" not in yaml.safe_load(path.read_text(encoding="utf-8"))["questions"]

    def test_an_unchallenged_keyword_guess_becomes_exact(self, tmp_path, monkeypatch):
        path = self._bank(tmp_path, monkeypatch)
        plan = self._plan(
            [
                self._field(
                    "q",
                    "What's your LinkedIn?",
                    "https://linkedin.com/in/example",
                    source="bank:linkedin~linkedin",
                )
            ]
        )
        self._run(tmp_path, monkeypatch, plan, {})
        q = yaml.safe_load(path.read_text(encoding="utf-8"))["questions"]["linkedin"]
        assert "What's your LinkedIn?" in q["wordings"]

    def test_a_corrected_keyword_guess_is_never_guessed_again(self, tmp_path, monkeypatch):
        path = self._bank(tmp_path, monkeypatch)
        plan = self._plan(
            [
                self._field(
                    "q",
                    "Is LinkedIn required?",
                    "No",
                    source="override",
                    replaced="bank:linkedin~linkedin",
                )
            ]
        )
        self._run(
            tmp_path,
            monkeypatch,
            plan,
            {"q": {"value": "No", "source": "user", "question": "linkedin_required"}},
        )
        q = yaml.safe_load(path.read_text(encoding="utf-8"))["questions"]
        assert "is linkedin required" in q["linkedin"]["not"]
        assert q["linkedin_required"]["answer"] == "No"

    def test_a_confirmed_claim_is_saved(self, tmp_path, monkeypatch):
        path = self._bank(tmp_path, monkeypatch)
        option = "Yes, I will require sponsorship in the future."
        plan = self._plan(
            [self._field("q", "Sponsorship?", option, source="override", options=[option, "No"])]
        )
        self._run(
            tmp_path,
            monkeypatch,
            plan,
            {"q": {"value": option, "claim": {"requires_sponsorship": True}}},
        )
        claims = yaml.safe_load(path.read_text(encoding="utf-8"))["claims"]
        assert claims[option] == {"requires_sponsorship": True}

    def test_answers_that_cannot_apply_fail_the_role(self, tmp_path, monkeypatch):
        self._bank(tmp_path, monkeypatch)
        from dataclasses import replace

        plan = replace(_fake_plan(JOB_ID), ignored_overrides=("q: not a field on this form",))
        out = self._run(tmp_path, monkeypatch, plan, {})
        assert out.category == "failed" and "cannot apply" in out.detail


class TestWorkdayIsRecognisedButNeverSubmitted:
    """`detect_ats` now names Workday, so the submission paths must gate on
    submittability rather than on "it parsed"."""

    WD_URL = "https://widgetco.wd5.myworkdayjobs.com/Careers/job/US/Eng_JR1"

    def test_resolve_url_refuses_a_workday_posting(self, tmp_path, monkeypatch):
        monkeypatch.setattr(apply_cli, "CLEAN", tmp_path / "clean.parquet")
        with pytest.raises(apply_cli.ManualApplyOnly):
            apply_cli.resolve_url("a1b2c3d4", {"url": self.WD_URL})


class TestTheSubmitLedger:
    """A role already sent never gets clicked again, even when its state.yaml
    was re-created (68 roles were applied to twice that way)."""

    def _stub(self, monkeypatch, result):
        monkeypatch.setattr(apply_cli, "build", lambda job_id, **kw: (_fake_plan(job_id), None))
        calls = []

        def run_one(plan, answers, **kw):
            calls.append(plan.job_id)
            if kw.get("before_click"):
                kw["before_click"]()
            return result

        monkeypatch.setattr(apply_cli, "run_one", run_one)
        monkeypatch.setattr(apply_cli.track_cli, "main", lambda argv: 0)
        return calls

    def test_a_submit_writes_clicking_then_the_outcome(self, monkeypatch):
        r = _fake_result(submitted=True, confirmed=True)
        r.outcome = "accepted"
        self._stub(monkeypatch, r)
        apply_cli.run_queue([JOB_ID], submit=True, sleeper=lambda s: None)
        from src.apply import ledger

        statuses = [x["status"] for x in ledger.records(apply_cli.ledger_path())]
        assert statuses == ["clicking", "accepted"]

    def test_a_role_in_the_ledger_is_skipped_without_opening_a_browser(self, monkeypatch):
        from src.apply import ledger

        ledger.append(apply_cli.ledger_path(), JOB_ID, "accepted", form_url=GH_URL)
        calls = self._stub(monkeypatch, _fake_result())
        out = apply_cli.run_queue([JOB_ID], submit=True, sleeper=lambda s: None)
        assert out[0].category == "skipped"
        assert "already sent" in out[0].detail
        assert calls == []

    def test_rejected_is_reported_and_never_tracked(self, monkeypatch):
        r = _fake_result(submit_error="the board rejected the application: spam")
        r.outcome = "rejected"
        self._stub(monkeypatch, r)
        tracked = []
        monkeypatch.setattr(apply_cli.track_cli, "main", lambda argv: tracked.append(argv) or 0)
        out = apply_cli.run_queue([JOB_ID], submit=True, sleeper=lambda s: None)
        assert out[0].category == "rejected"
        assert tracked == []

    def test_unsent_is_reported_and_never_tracked(self, monkeypatch):
        r = _fake_result(submitted=True)
        r.outcome = "unsent"
        self._stub(monkeypatch, r)
        tracked = []
        monkeypatch.setattr(apply_cli.track_cli, "main", lambda argv: tracked.append(argv) or 0)
        out = apply_cli.run_queue([JOB_ID], submit=True, sleeper=lambda s: None)
        assert out[0].category == "unsent"
        assert tracked == []

    def test_the_queue_leaves_out_sent_roles(self, repo):
        from src.apply import ledger

        repo.write_state()
        assert apply_cli.eligible_queue() == [JOB_ID]
        ledger.append(apply_cli.ledger_path(), JOB_ID, "backfill")
        assert apply_cli.eligible_queue() == []


def test_tests_never_touch_the_real_applications_folder():
    from src.shared import paths

    assert paths.REPO_ROOT / "applications" != paths.APPLICATIONS
    assert apply_cli.APPLY_RUNS.parent != paths.REPO_ROOT / "applications"


class TestKeywordsGeneralize:
    def _bank(self, tmp_path, monkeypatch):
        import shutil

        from src.apply import bank

        path = tmp_path / "bank.yaml"
        shutil.copy(FIXTURES / "application_answers.yaml", path)
        monkeypatch.setattr(bank, "DEFAULT_PATH", path)
        return path

    def test_a_keyword_lets_the_next_wording_be_guessed(self, tmp_path, monkeypatch):
        from src.apply import bank
        from src.apply.answers import find_question, load_answers

        path = self._bank(tmp_path, monkeypatch)
        bank.learn(
            [
                bank.Lesson(
                    label="Tell us about a project you're proud of",
                    question="project_proud",
                    value="I built a widget.",
                    keyword="proud of",
                    source="bullets:B-1",
                )
            ]
        )
        found = find_question("What are you most proud of building?", load_answers(path))
        assert found and found[0].id == "project_proud" and "~" in found[1]

    def test_a_keyword_that_shadows_another_question_is_refused_alone(self, tmp_path, monkeypatch):
        from src.apply import bank

        path = self._bank(tmp_path, monkeypatch)
        changes = bank.learn(
            [
                bank.Lesson(
                    label="Your personal LinkedIn page",
                    question="linkedin_page",
                    value="x",
                    keyword="linkedin page",
                    source="user",
                )
            ]
        )
        assert any("keyword refused" in c for c in changes)
        assert "linkedin_page" in yaml.safe_load(path.read_text(encoding="utf-8"))["questions"]


def test_a_shortened_question_id_is_not_a_new_question(tmp_path, monkeypatch):
    import shutil

    from src.apply import bank

    path = tmp_path / "bank.yaml"
    shutil.copy(FIXTURES / "application_answers.yaml", path)
    monkeypatch.setattr(bank, "DEFAULT_PATH", path)
    data = bank.read()
    data["questions"]["years_of_professional_experience_total"] = {
        "answer": "2",
        "wordings": ["Years of professional experience"],
    }
    bank.write(data)
    changes = bank.learn(
        [
            bank.Lesson(
                label="How many years have you worked?",
                question="years_of_professional_exp",
                value="2",
                source="user",
            )
        ]
    )
    assert changes == [
        "not saved: years_of_professional_exp: did you mean "
        "'years_of_professional_experience_total'?"
    ]


class TestEachLessonMustHold:
    """A lesson is saved only if the bank, with it in, still answers what was
    confirmed; a refused lesson never costs the others."""

    def _bank(self, tmp_path, monkeypatch):
        import shutil

        from src.apply import bank

        path = tmp_path / "bank.yaml"
        shutil.copy(FIXTURES / "application_answers.yaml", path)
        monkeypatch.setattr(bank, "DEFAULT_PATH", path)
        return path

    def test_a_different_answer_under_the_same_id_is_refused(self, tmp_path, monkeypatch):
        from src.apply import bank

        path = self._bank(tmp_path, monkeypatch)
        changes = bank.learn(
            [
                bank.Lesson(
                    label="Pronouns, please",
                    question="pronouns",
                    value="He/Him",
                    offered=("They/Them", "He/Him"),
                )
            ]
        )
        assert changes[0].startswith("not saved: pronouns answers 'They/Them'")
        q = yaml.safe_load(path.read_text(encoding="utf-8"))["questions"]["pronouns"]
        assert q["answer"] == ["They/Them"]

    def test_a_new_spelling_is_learned_when_the_board_offered_none_of_the_bank_s(
        self, tmp_path, monkeypatch
    ):
        from src.apply import bank

        path = self._bank(tmp_path, monkeypatch)
        bank.learn(
            [
                bank.Lesson(
                    label="Pronouns",
                    question="pronouns",
                    value="they / them",
                    offered=("they / them", "he / him"),
                )
            ]
        )
        q = yaml.safe_load(path.read_text(encoding="utf-8"))["questions"]["pronouns"]
        assert q["answer"] == ["They/Them", "they / them"]

    def test_a_one_employer_answer_never_becomes_a_question(self, tmp_path, monkeypatch):
        from src.apply import bank

        path = self._bank(tmp_path, monkeypatch)
        changes = bank.learn(
            [
                bank.Lesson(
                    label="What excites you about this role?",
                    question="excites_role",
                    source="jd",
                    value="Your fraud roadmap at Acme.",
                )
            ]
        )
        assert "stays with this role" in changes[0]
        assert "excites_role" not in yaml.safe_load(path.read_text(encoding="utf-8"))["questions"]

    def test_one_bad_lesson_does_not_lose_the_rest(self, tmp_path, monkeypatch):
        from src.apply import bank

        path = self._bank(tmp_path, monkeypatch)
        changes = bank.learn(
            [
                bank.Lesson(
                    label="Will you require visa sponsorship?",
                    question="clash",
                    value="No",
                    source="user",
                ),
                bank.Lesson(label="Your pronouns?", question="pronouns", value="They/Them"),
            ]
        )
        q = yaml.safe_load(path.read_text(encoding="utf-8"))["questions"]
        assert "clash" not in q and "Your pronouns?" in q["pronouns"]["wordings"]
        assert any(c.startswith("not saved") for c in changes)

    def test_undo_of_the_last_wording_removes_the_question(self, tmp_path, monkeypatch):
        from src.apply import bank

        path = self._bank(tmp_path, monkeypatch)
        bank.learn(
            [
                bank.Lesson(
                    label="Why widgets?",
                    question="why_widgets",
                    value="I like them.",
                    source="user",
                )
            ]
        )
        out = bank.undo("why_widgets", wording="Why widgets?")
        assert "removed" in out
        assert "why_widgets" not in yaml.safe_load(path.read_text(encoding="utf-8"))["questions"]

    def test_every_write_keeps_a_history(self, tmp_path, monkeypatch):
        from src.apply import bank

        self._bank(tmp_path, monkeypatch)
        bank.learn([bank.Lesson(label="Your pronouns?", question="pronouns", value="They/Them")])
        assert list((tmp_path / ".bank_history").glob("bank.*.yaml"))


def test_an_hourly_posted_minimum_is_not_a_salary(tmp_path, monkeypatch):
    clean = tmp_path / "clean.parquet"
    factories.write_clean(
        clean, {"job_id": JOB_ID, "url": GH_URL, "salary_min": 40.0, "salary_currency": "USD"}
    )
    monkeypatch.setattr(apply_cli, "CLEAN", clean)
    (tmp_path / "verticals.yaml").write_text(
        yaml.safe_dump(
            {
                "verticals": {
                    "example_primary": {
                        "salary_expectation": {"markup_pct": 10, "fallback": 110000}
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(apply_cli.paths, "PROFILE", tmp_path)
    assert apply_cli.salary_for(JOB_ID, "example_primary", "United States") == 110000.0


def test_an_optional_export_control_question_takes_no_override(answers, tailor_dir):
    from src.apply.plan import Override, build_plan
    from src.apply.reconcile import MergedField, MergedOption, Reconciled

    f = MergedField(
        id="q",
        name="q",
        label="Does the deemed export rule affect you?",
        required=False,
        kind="select",
        section="questions",
        multi=False,
        options=(MergedOption(label="Yes"), MergedOption(label="No")),
    )
    plan = build_plan(
        Reconciled(fields=(f,), api_only=()),
        answers,
        tailor_dir,
        overrides={"q": Override(value="No")},
    )
    assert not plan.fields and "export control" in plan.ignored_overrides[0]


def test_the_jd_country_comes_from_where_the_hire_works(tmp_path, monkeypatch):
    clean = tmp_path / "clean.parquet"
    factories.write_clean(
        clean,
        {
            "job_id": "a",
            "url": "",
            "jd_text": "Our team in India builds tools. "
            "This role is remote and you must be based in the United States.",
        },
        {"job_id": "b", "url": "", "jd_text": "We have offices in India and Poland."},
    )
    monkeypatch.setattr(apply_cli, "CLEAN", clean)
    assert apply_cli._jd_country("a") == "United States"
    assert apply_cli._jd_country("b") == ""


def _has_libpostal() -> bool:
    try:
        import postal.parser  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not _has_libpostal(), reason="needs `uv sync --group discovery`")
class TestTheJobsCountry:
    """Where a role is, read from discovery's location in every shape seen live."""

    @pytest.mark.parametrize(
        "location,country",
        [
            ("VA - Tysons Corner", "United States"),
            ("USA-CA San Jose Innovation Drive", "United States"),
            ("US, CA, Santa Clara", "United States"),
            ("IN Pune", "India"),
            ("MH, IN", "India"),
            ("Wrocław, DO, PL", "Poland"),
            ("München, BY, DE", "Germany"),
            ("SGP Work-at-Home", "Singapore"),
            ("Northern Virginia - On-site", "United States"),
            ("Hybrid", ""),
            ("Remote", ""),
        ],
    )
    def test_a_location_names_its_country(self, answers, location, country):
        assert apply_cli.job_country({"job_id": "", "location": location}, answers) == country

    def test_a_location_nothing_reads_is_unknown(self, answers):
        from src.apply.answers import UNKNOWN_COUNTRY

        assert (
            apply_cli.job_country({"job_id": "", "location": "HLT-Hong Leong Tower"}, answers)
            == UNKNOWN_COUNTRY
        )


def test_an_unconfirmed_answers_file_teaches_nothing_and_blocks_guesses(tmp_path, monkeypatch):
    import shutil
    from dataclasses import replace

    from src.apply import bank
    from src.apply.plan import FieldPlan

    path = tmp_path / "bank.yaml"
    shutil.copy(FIXTURES / "application_answers.yaml", path)
    monkeypatch.setattr(bank, "DEFAULT_PATH", path)
    guess = FieldPlan(
        id="q",
        name="q",
        label="What's your LinkedIn?",
        kind="text",
        section="questions",
        required=True,
        multi=False,
        value="https://x",
        tier="B",
        source="bank:linkedin~linkedin",
    )
    monkeypatch.setattr(
        apply_cli,
        "build",
        lambda job_id, **kw: (replace(_fake_plan(JOB_ID), fields=(guess,)), None),
    )
    monkeypatch.setattr(apply_cli, "run_one", _raise(AssertionError("must not fill")))
    stale = tmp_path / "answers.json"
    stale.write_text(json.dumps({"job_id": JOB_ID}), encoding="utf-8")
    out = apply_cli.run_queue([JOB_ID], submit=True, sleeper=lambda s: None, answers_path=stale)
    assert out[0].category == "parked" and not out[0].learned
    assert "What's your LinkedIn?" not in (
        yaml.safe_load(path.read_text(encoding="utf-8"))["questions"]["linkedin"].get("wordings")
        or []
    )


def test_a_refused_lesson_leaves_no_not_on_the_guessing_question(tmp_path, monkeypatch):
    import shutil

    from src.apply import bank

    path = tmp_path / "bank.yaml"
    shutil.copy(FIXTURES / "application_answers.yaml", path)
    monkeypatch.setattr(bank, "DEFAULT_PATH", path)
    bank.learn(
        [
            bank.Lesson(
                label="Is LinkedIn required?",
                question="linkedin_required",
                value="No",
                source="jd",
                misfired="linkedin",
            )
        ]
    )
    assert not yaml.safe_load(path.read_text(encoding="utf-8"))["questions"]["linkedin"].get("not")


class TestAConfirmedFactJudgmentIsLearned:
    LABEL = "Does the deemed export rule apply to you?"

    def test_it_is_saved_answered_next_time_and_undone(self, tmp_path, monkeypatch):
        import shutil

        from src.apply import bank
        from src.apply.answers import load_answers, resolve
        from src.apply.reconcile import MergedField, MergedOption

        path = tmp_path / "bank.yaml"
        shutil.copy(FIXTURES / "application_answers.yaml", path)
        changes = bank.learn(
            [
                bank.Lesson(
                    label=self.LABEL, value="Yes", fact={"fact": "us_person", "yes_when": False}
                )
            ],
            path=path,
        )
        assert any("fact wording" in c for c in changes)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["work_authorization"]["us_person"] = "no"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        f = MergedField(
            id="q",
            name="q",
            label=self.LABEL,
            required=True,
            kind="select",
            section="questions",
            multi=False,
            options=(MergedOption(label="Yes"), MergedOption(label="No")),
        )
        assert resolve(f, load_answers(path)).value == "Yes"
        refused = bank.learn(
            [
                bank.Lesson(
                    label=self.LABEL, value="No", fact={"fact": "us_person", "yes_when": True}
                )
            ],
            path=path,
        )
        assert refused and refused[0].startswith("not saved")
        assert "removed" in bank.undo(fact=self.LABEL, path=path)
        assert resolve(f, load_answers(path)).parked

    def test_the_answers_file_takes_a_fact_without_a_value(self, tmp_path):
        p = tmp_path / "a.json"
        p.write_text(
            json.dumps(
                {
                    "job_id": JOB_ID,
                    "q": {"fact": {"fact": "us_person", "yes_when": False}, "source": "work_auth"},
                }
            ),
            encoding="utf-8",
        )
        ov = apply_cli.load_overrides(p, JOB_ID)["q"]
        assert ov.fact == {"fact": "us_person", "yes_when": False}
        p.write_text(json.dumps({"q": {"fact": {"fact": "us_person"}}}), encoding="utf-8")
        with pytest.raises(apply_cli.ApplyCliError, match="yes_when"):
            apply_cli.load_overrides(p, JOB_ID)


def test_teach_saves_one_answer_and_a_keyword_finds_the_next_wording(tmp_path, capsys):
    import shutil

    from src.apply.answers import find_question, load_answers

    path = tmp_path / "bank.yaml"
    shutil.copy(FIXTURES / "application_answers.yaml", path)
    argv = [
        "teach",
        "--question-id",
        "background_check",
        "--path",
        str(path),
        "--label",
        "Do you consent to a background investigation and drug test?",
        "--value",
        "Yes",
        "--offered",
        "Yes",
        "--offered",
        "No",
        "--keyword",
        "background investigation",
        "--job",
        "aaaa1111",
    ]
    assert apply_cli.main(argv) == 0
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))["questions"]["background_check"]
    assert saved["answer"] == "Yes"
    found = find_question(
        "Will you undergo a background investigation before you start?", load_answers(path)
    )
    assert found and found[0].id == "background_check" and "~" in found[1]
    assert apply_cli.main(argv) == 0
    assert "nothing to change" in capsys.readouterr().out
