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

import pandas as pd
import pytest
import yaml

from src import apply_cli
from src.apply import ashby
from src.apply.answers import AnswersError
from src.apply.greenhouse import BoardForm, Posting, PostingExpired
from src.apply.plan import PlanError
from src.apply.schema import parse_schema

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
    monkeypatch.setattr(apply_cli, "PIPELINE", pipeline)
    monkeypatch.setattr(apply_cli, "APPLICATIONS", tailor_dir.parent.parent)

    tailored = f"{tailor_dir.parent.name}/{tailor_dir.name}"

    class Repo:
        def __init__(self):
            self.clean = clean
            self.pipeline = pipeline
            self.tailored = tailored
            self.out_dir = tailor_dir

        def write_clean(self, **rows):
            pd.DataFrame(
                [{"job_id": j, "url": u} for j, u in rows.items()]
            ).to_parquet(clean)

        def write_state(self, job_id=JOB_ID, url=GH_URL, tailored_dirs=None,
                        cover_letters=None, state="tailored"):
            d = pipeline / job_id
            d.mkdir(parents=True, exist_ok=True)
            (d / "state.yaml").write_text(yaml.safe_dump({
                "job_id": job_id, "company": "Bushing Group", "title": "Widget Engineer",
                "state": state, "url": url,
                "tailored_dirs": [tailored] if tailored_dirs is None else tailored_dirs,
                "cover_letters": [tailored] if cover_letters is None else cover_letters,
            }), encoding="utf-8")

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
            slug="bushinggroup", html="", scan=scan, schema=schema,
            reconciled=reconcile(scan, schema),
        )

    monkeypatch.setattr(apply_cli, "load_board", fake_load_board)
    return calls


@pytest.fixture(autouse=True)
def stub_answers(monkeypatch):
    """The synthetic widget-world config, never the user's real one."""
    from src.apply.answers import load_answers

    loaded = load_answers(
        FIXTURES / "application_answers.yaml", FIXTURES / "preferences_time_limited.md"
    )
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

    def test_neither(self):
        assert apply_cli.detect_ats(LINKEDIN_URL) is None


class TestBuildDispatchesToLever:
    def test_a_lever_url_builds_a_lever_plan(self, repo, monkeypatch, tailor_dir):
        repo.write_clean(**{JOB_ID: LEVER_URL})
        repo.write_state(url=LEVER_URL)
        from .conftest import load_html
        monkeypatch.setattr("src.apply.lever.fetch_text",
                            lambda *a, **k: load_html("form_lever_minimal"))

        plan, _ = apply_cli.build(JOB_ID)
        assert plan.ats == "lever"
        assert plan.requires_captcha is True
        assert plan.board == "widgetco"


class TestBuildDispatchesToAshby:
    def _stub_api(self, monkeypatch):
        from .conftest import load_api
        monkeypatch.setattr("src.apply.ashby.fetch_json_post",
                            lambda *a, **k: load_api("api_ashby_form"))
        # Otherwise `load_board` would open a real headless browser to read
        # DOM-only descriptions — no network in this test module.
        monkeypatch.setattr("src.apply.ashby.fetch_dom_enrichment",
                            lambda *a, **k: {})

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
            self, repo, monkeypatch, tailor_dir):
        """Registering the driver is what moves these roles out of `manual`.
        The shortlist reads the same flag, so the two cannot disagree."""
        repo.write_clean(**{JOB_ID: ASHBY_URL})
        repo.write_state(url=ASHBY_URL)
        self._stub_api(monkeypatch)
        opened = []
        monkeypatch.setattr("src.apply_cli.run_one",
                            lambda plan, *a, **k: opened.append(plan.ats) or _fake_result())

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
        with pytest.raises(apply_cli.ApplyCliError, match="neither clean.parquet nor"):
            apply_cli.resolve_url(JOB_ID, None)


class TestResolveOutDir:
    def test_the_most_recent_tailor_dir_wins(self, repo):
        # tailored_dirs[] is append-only, so a re-tailor puts _v2 at the end.
        got = apply_cli.resolve_out_dir(JOB_ID, {"tailored_dirs": ["old/dir", repo.tailored]})
        assert got == repo.out_dir

    def test_no_tailored_dirs_points_at_tailor(self, repo):
        with pytest.raises(apply_cli.ApplyCliError, match="run /tailor"):
            apply_cli.resolve_out_dir(JOB_ID, {"tailored_dirs": []})

    def test_no_state_at_all_points_at_tailor(self, repo):
        with pytest.raises(apply_cli.ApplyCliError, match="run /tailor"):
            apply_cli.resolve_out_dir(JOB_ID, None)

    def test_a_tailored_dir_that_is_gone_from_disk_is_named(self, repo):
        with pytest.raises(apply_cli.ApplyCliError, match="not a"):
            apply_cli.resolve_out_dir(JOB_ID, {"tailored_dirs": ["example_primary/deleted"]})


class TestParsedSalaryFor:
    """`_parsed_salary_for()` — the deterministic bridge between a job's
    parsed compensation columns and `Answers.parsed_salary` (§ discussion:
    no JD text, no LLM, just clean.parquet + verticals.yaml)."""

    def _write_clean(self, path, job_id, salary_min, salary_currency):
        pd.DataFrame([{
            "job_id": job_id, "url": GH_URL,
            "salary_min": salary_min, "salary_currency": salary_currency,
        }]).to_parquet(path)

    def _write_verticals(self, tmp_path, vertical, markup_pct):
        monkeypatch_target = tmp_path / "verticals.yaml"
        monkeypatch_target.write_text(yaml.safe_dump({
            "verticals": {vertical: {"salary_expectation": {"markup_pct": markup_pct}}},
        }), encoding="utf-8")
        return monkeypatch_target

    def test_usd_salary_min_computes_the_marked_up_figure(self, tmp_path, monkeypatch):
        clean = tmp_path / "clean.parquet"
        self._write_clean(clean, JOB_ID, 120000, "USD")
        monkeypatch.setattr(apply_cli, "CLEAN", clean)
        monkeypatch.setattr(apply_cli.paths, "PROFILE",
                             self._write_verticals(tmp_path, "risk_ai", 10).parent)
        assert apply_cli._parsed_salary_for(JOB_ID, "risk_ai") == 132000.0

    def test_non_usd_currency_returns_none(self, tmp_path, monkeypatch):
        clean = tmp_path / "clean.parquet"
        self._write_clean(clean, JOB_ID, 120000, "GBP")
        monkeypatch.setattr(apply_cli, "CLEAN", clean)
        monkeypatch.setattr(apply_cli.paths, "PROFILE",
                             self._write_verticals(tmp_path, "risk_ai", 10).parent)
        assert apply_cli._parsed_salary_for(JOB_ID, "risk_ai") is None

    def test_no_salary_expectation_configured_returns_none(self, tmp_path, monkeypatch):
        clean = tmp_path / "clean.parquet"
        self._write_clean(clean, JOB_ID, 120000, "USD")
        monkeypatch.setattr(apply_cli, "CLEAN", clean)
        verticals = tmp_path / "verticals.yaml"
        verticals.write_text(yaml.safe_dump({"verticals": {"risk_ai": {}}}), encoding="utf-8")
        monkeypatch.setattr(apply_cli.paths, "PROFILE", verticals.parent)
        assert apply_cli._parsed_salary_for(JOB_ID, "risk_ai") is None

    def test_missing_salary_min_returns_none(self, tmp_path, monkeypatch):
        clean = tmp_path / "clean.parquet"
        self._write_clean(clean, JOB_ID, None, "USD")
        monkeypatch.setattr(apply_cli, "CLEAN", clean)
        monkeypatch.setattr(apply_cli.paths, "PROFILE",
                             self._write_verticals(tmp_path, "risk_ai", 10).parent)
        assert apply_cli._parsed_salary_for(JOB_ID, "risk_ai") is None

    def test_no_clean_parquet_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(apply_cli, "CLEAN", tmp_path / "missing.parquet")
        assert apply_cli._parsed_salary_for(JOB_ID, "risk_ai") is None


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
        assert all(f["assert_selected"] == (f["kind"] == "react_select")
                   for f in data["fields"])

    def test_url_and_out_dir_overrides_skip_lookup(self, repo, stub_board, capsys):
        # No clean.parquet, no state.yaml: both overrides supplied.
        assert apply_cli.main(
            ["plan", JOB_ID, "--url", GH_URL, "--out-dir", str(repo.out_dir)]
        ) == 0
        assert "READY" in capsys.readouterr().out

    def test_an_expired_posting_exits_2_not_1(self, repo, monkeypatch, capsys):
        repo.write_clean(**{JOB_ID: GH_URL})
        repo.write_state()
        monkeypatch.setattr(apply_cli, "load_board", _raise(PostingExpired("gone (404)")))
        assert apply_cli.main(["plan", JOB_ID]) == 2
        assert "EXPIRED" in capsys.readouterr().err

    @pytest.mark.parametrize("exc", [
        AnswersError("profile/application_answers.yaml missing"),
        PlanError("out_dir is not a directory"),
    ])
    def test_config_failures_exit_1_with_the_message(self, repo, monkeypatch, capsys, exc):
        repo.write_clean(**{JOB_ID: GH_URL})
        repo.write_state()
        monkeypatch.setattr(apply_cli, "load_board", _raise(exc))
        assert apply_cli.main(["plan", JOB_ID]) == 1
        assert str(exc) in capsys.readouterr().err

    def test_a_non_greenhouse_role_exits_1_and_says_apply_by_hand(
        self, repo, stub_board, capsys
    ):
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
        monkeypatch.setattr(apply_cli, "load_board", lambda url, timeout=30: BoardForm(
            posting=Posting(token="1000001", url_slug=None), slug="gasketworks",
            html="", scan=scan, schema=schema, reconciled=reconcile(scan, schema),
        ))
        assert apply_cli.main(["plan", JOB_ID]) == 0
        out = capsys.readouterr().out
        assert "PARKED" in out and "Nothing would be submitted" in out


class TestPrepareCommand:
    """`/apply` Step 1, consolidated into the CLI: validate prerequisites and
    derive OUT_DIR/VERTICAL/COMPANY_ANSWERS from state.yaml, printing
    shell-quoted `KEY=value` lines the command session `eval`s."""

    @pytest.fixture(autouse=True)
    def _answers_yaml_exists(self, monkeypatch, tmp_path):
        answers_path = tmp_path / "application_answers.yaml"
        answers_path.write_text("schema_version: 1\n", encoding="utf-8")
        monkeypatch.setattr(apply_cli, "DEFAULT_PATH", answers_path)
        monkeypatch.setattr(apply_cli, "EXAMPLE_PATH", tmp_path / "application_answers.example.yaml")
        monkeypatch.setattr(apply_cli, "_playwright_available", lambda: True)

    def test_happy_path_prints_every_field(self, repo, capsys):
        repo.write_state()
        (repo.out_dir / "company_answers.md").write_text("why_company\n", encoding="utf-8")
        assert apply_cli.main(["prepare", JOB_ID]) == 0
        out = capsys.readouterr().out
        lines = dict(line.split("=", 1) for line in out.strip().splitlines())
        assert lines["JOB_ID"] == JOB_ID
        assert shlex.split(lines["STATE"])[0] == "tailored"
        assert shlex.split(lines["OUT_DIR"])[0] == str(repo.out_dir)
        assert shlex.split(lines["VERTICAL"])[0] == "example_primary"
        assert shlex.split(lines["COMPANY_ANSWERS"])[0] == str(repo.out_dir / "company_answers.md")
        assert shlex.split(lines["OVERRIDES_FILE"])[0] == str(repo.out_dir / "answers_override.json")

    def test_resets_the_overrides_file_to_just_job_id(self, repo, capsys):
        repo.write_state()
        stale = repo.out_dir / "answers_override.json"
        stale.write_text(json.dumps({"job_id": JOB_ID, "leftover_field": "old"}),
                          encoding="utf-8")
        assert apply_cli.main(["prepare", JOB_ID]) == 0
        assert json.loads(stale.read_text(encoding="utf-8")) == {"job_id": JOB_ID}

    def test_no_cover_letter_leaves_company_answers_blank(self, repo, capsys):
        repo.write_state(cover_letters=[])
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
        assert "tailored_dirs" in capsys.readouterr().err

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


def _fake_plan(job_id=JOB_ID, company="Bushing Group", title="Widget Engineer",
               unmapped=(), submit_selector="#application-form button[type=submit]",
               ats="greenhouse", requires_captcha=False):
    from src.apply.plan import Plan
    return Plan(
        job_id=job_id, board="bushinggroup", token="1", form_url=GH_URL,
        company=company, title=title, out_dir=Path("/tmp"),
        fields=(), files=(), unmapped=tuple(unmapped), draftable=(), skipped=(),
        submit_selector=submit_selector, submit_disabled=False, ats=ats,
        requires_captcha=requires_captcha,
    )


def _fake_result(*, submitted=False, failures=(), recovered=(), submit_error="",
                  confirmed=False):
    from src.apply.fill import FillResult
    return FillResult(
        form_url=GH_URL, failures=list(failures), recovered=list(recovered),
        submitted=submitted, submit_error=submit_error, confirmed=confirmed,
    )


class TestEligibleQueue:
    def test_tailored_with_both_dirs_and_letters_is_eligible(self, repo):
        repo.write_state()
        assert apply_cli.eligible_queue(repo.pipeline) == [JOB_ID]

    def test_missing_cover_letters_is_still_eligible(self, repo):
        repo.write_state(cover_letters=[])
        assert apply_cli.eligible_queue(repo.pipeline) == [JOB_ID]

    def test_missing_tailored_dirs_is_not_eligible(self, repo):
        repo.write_state(tailored_dirs=[])
        assert apply_cli.eligible_queue(repo.pipeline) == []

    def test_terminal_states_are_excluded(self, repo):
        for job_id, state in [("aaaa0001", "rejected"), ("aaaa0002", "withdrawn"),
                               ("aaaa0003", "offer"), ("aaaa0004", "ghosted")]:
            repo.write_state(job_id=job_id, state=state)
        assert apply_cli.eligible_queue(repo.pipeline) == []

    def test_non_tailored_active_states_are_excluded(self, repo):
        for job_id, state in [("bbbb0001", "saved"), ("bbbb0002", "applied"),
                               ("bbbb0003", "screen")]:
            repo.write_state(job_id=job_id, state=state)
        assert apply_cli.eligible_queue(repo.pipeline) == []

    def test_ordering_is_deterministic_regardless_of_write_order(self, repo):
        for job_id in ("zzzz9999", "aaaa1111", "mmmm5555"):
            repo.write_state(job_id=job_id)
        assert apply_cli.eligible_queue(repo.pipeline) == ["aaaa1111", "mmmm5555", "zzzz9999"]


class TestParseDuration:
    @pytest.mark.parametrize("spec,seconds", [
        ("4m", 240.0), ("60s", 60.0), ("90", 90.0), ("1.5h", 5400.0), ("0m", 0.0),
    ])
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
            ["a1", "a2", "a3"], submit=False,
            rate=999, jitter=0, sleeper=sleeps.append, jitter_fn=lambda a, b: 0,
        )
        # 3 roles, 2 gaps between them — never on the first.
        assert len(sleeps) == 2
        assert all(s == 999 for s in sleeps)

    def test_a_parked_role_does_not_stop_the_run(self, monkeypatch):
        monkeypatch.setattr(apply_cli, "build",
                             lambda job_id, **kw: (_fake_plan(job_id), None))
        monkeypatch.setattr(apply_cli, "run_one",
                             lambda plan, answers, **kw: _fake_result())
        outcomes = apply_cli.run_queue(
            ["parked1", "parked2"], submit=False, sleeper=lambda s: None,
        )
        assert [o.category for o in outcomes] == ["ready", "ready"]

        # Now make the first one park (unmapped, nothing recovered) and confirm
        # the second role still runs.
        def build_with_park(job_id, **kw):
            unmapped = () if job_id == "ok" else [
                type("U", (), {"id": "why_us"})(),
            ]
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
        monkeypatch.setattr(apply_cli, "build",
                             lambda job_id, **kw: (_fake_plan(job_id), None))
        monkeypatch.setattr(apply_cli, "run_one",
                             lambda plan, answers, **kw: _fake_result(submitted=True))
        monkeypatch.setattr(apply_cli.track_cli, "main", track_calls.append)
        # state_io.transition must never be reached from this path.
        monkeypatch.setattr(apply_cli.state_io, "transition",
                             _raise(AssertionError("state_io.transition must not be called")))

        outcomes = apply_cli.run_queue([JOB_ID], submit=True, sleeper=lambda s: None)

        # No confirmation marker was seen, so the submission is reported as
        # unconfirmed — but it still transitions, because a duplicate
        # application is worse than a state row that needs an eyeball.
        assert outcomes[0].category == "submitted_unconfirmed"
        assert track_calls == [[JOB_ID, "applied", "--note",
                                 "auto-submitted via /apply (bushinggroup)"]]

    def test_a_recovered_field_is_no_longer_blocking(self, monkeypatch):
        u = type("U", (), {"id": "hispanic_ethnicity"})()
        monkeypatch.setattr(apply_cli, "build",
                             lambda job_id, **kw: (_fake_plan(job_id, unmapped=[u]), None))
        monkeypatch.setattr(
            apply_cli, "run_one",
            lambda plan, answers, **kw: _fake_result(recovered=["hispanic_ethnicity"]),
        )
        outcomes = apply_cli.run_queue([JOB_ID], submit=False, sleeper=lambda s: None)
        assert outcomes[0].category == "ready"

    def test_a_fill_failure_is_a_failed_category(self, monkeypatch):
        monkeypatch.setattr(apply_cli, "build",
                             lambda job_id, **kw: (_fake_plan(job_id), None))
        monkeypatch.setattr(
            apply_cli, "run_one",
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
            RunOutcome("a2", "B Co", "Eng", "parked", detail="required unresolved",
                       unmapped=("why_us",)),
            RunOutcome("a3", "C Co", "Eng", "failed", detail="boom"),
        ]
        text = apply_cli.render_report(outcomes, apply_cli.datetime(2026, 1, 1))
        assert "## Submitted (1)" in text
        assert "## Parked (1)" in text
        assert "why_us" in text
        assert "## Failed (1)" in text
        assert "boom" in text

    def test_write_report_creates_a_dated_file(self, tmp_path):
        from src.apply_cli import RunOutcome
        out_dir = tmp_path / "apply_runs"
        path = apply_cli.write_report(
            [RunOutcome("a1", "A Co", "Eng", "submitted")],
            apply_cli.datetime(2026, 1, 1, 9, 30, 0), out_dir=out_dir,
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
        monkeypatch.setattr(apply_cli, "build",
                             lambda job_id, **kw: (_fake_plan(job_id), None))
        submit_after_seen = []
        monkeypatch.setattr(
            apply_cli, "run_one",
            lambda plan, answers, **kw: (
                submit_after_seen.append(kw.get("submit_after")), _fake_result()
            )[1],
        )
        assert apply_cli.main(["run", "--rate", "0s", "--jitter", "0s"]) == 0
        assert submit_after_seen == [False]

    def test_job_id_override_runs_one_role_even_off_queue_order(
        self, repo, monkeypatch, tmp_path
    ):
        repo.write_state()
        monkeypatch.setattr(apply_cli, "APPLY_RUNS", tmp_path / "apply_runs")
        monkeypatch.setattr(apply_cli, "build",
                             lambda job_id, **kw: (_fake_plan(job_id), None))
        monkeypatch.setattr(apply_cli, "run_one",
                             lambda plan, answers, **kw: _fake_result())
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
        monkeypatch.setattr(apply_cli, "run_one",
                             lambda plan, answers, **kw: _fake_result())
        assert apply_cli.main(["run", "--limit", "1", "--rate", "0s"]) == 0
        assert len(seen) == 1

    def test_a_failed_role_exits_1_and_a_report_is_written(self, repo, monkeypatch, tmp_path):
        repo.write_state()
        out_dir = tmp_path / "apply_runs"
        monkeypatch.setattr(apply_cli, "APPLY_RUNS", out_dir)
        monkeypatch.setattr(apply_cli, "build",
                             lambda job_id, **kw: (_fake_plan(job_id), None))
        monkeypatch.setattr(
            apply_cli, "run_one",
            lambda plan, answers, **kw: _fake_result(failures=["boom"]),
        )
        assert apply_cli.main(["run", "--rate", "0s"]) == 1
        assert list(out_dir.glob("*.md"))


class TestASubmissionIsNeverSilentlyLost:
    """The three ways a run used to submit without leaving a usable trace."""

    def test_a_confirmed_submit_is_plain_submitted(self, monkeypatch):
        monkeypatch.setattr(apply_cli, "build",
                             lambda job_id, **kw: (_fake_plan(job_id), None))
        monkeypatch.setattr(apply_cli, "run_one",
                             lambda plan, answers, **kw: _fake_result(
                                 submitted=True, confirmed=True))
        monkeypatch.setattr(apply_cli.track_cli, "main", lambda argv: 0)
        out = apply_cli.run_queue([JOB_ID], submit=True, sleeper=lambda s: None)
        assert out[0].category == "submitted"

    def test_track_cli_returning_nonzero_is_not_reported_as_a_clean_submit(
        self, monkeypatch
    ):
        # track_cli.main RETURNS 1 on a rejected transition, it does not raise.
        # Reporting "submitted" here leaves the role at `tailored`, so the next
        # --submit run applies to the same board a second time.
        monkeypatch.setattr(apply_cli, "build",
                             lambda job_id, **kw: (_fake_plan(job_id), None))
        monkeypatch.setattr(apply_cli, "run_one",
                             lambda plan, answers, **kw: _fake_result(submitted=True))
        monkeypatch.setattr(apply_cli.track_cli, "main", lambda argv: 1)
        out = apply_cli.run_queue([JOB_ID], submit=True, sleeper=lambda s: None)
        assert out[0].category == "submitted_untracked"
        assert "still says tailored" in out[0].detail

    def test_track_cli_raising_does_not_abort_the_run_or_lose_the_submission(
        self, monkeypatch
    ):
        monkeypatch.setattr(apply_cli, "build",
                             lambda job_id, **kw: (_fake_plan(job_id), None))
        monkeypatch.setattr(apply_cli, "run_one",
                             lambda plan, answers, **kw: _fake_result(submitted=True))
        monkeypatch.setattr(apply_cli.track_cli, "main",
                             _raise(OSError("disk full")))
        out = apply_cli.run_queue([JOB_ID, "second"], submit=True, sleeper=lambda s: None)
        assert out[0].category == "submitted_untracked"
        assert "OSError" in out[0].detail
        assert len(out) == 2, "the second role must still run"

    def test_a_driver_error_is_isolated_to_its_role(self, monkeypatch):
        monkeypatch.setattr(apply_cli, "build",
                             lambda job_id, **kw: (_fake_plan(job_id), None))

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
        monkeypatch.setattr(apply_cli, "build",
                             lambda job_id, **kw: (_fake_plan(job_id, ats="workday"), None))
        monkeypatch.setattr(apply_cli, "run_one",
                             _raise(AssertionError("must not open a browser")))
        out = apply_cli.run_queue([JOB_ID], submit=True, sleeper=lambda s: None)
        assert out[0].category == "manual"
        assert "apply by hand" in out[0].detail

    def test_a_manual_apply_url_is_manual_not_failed(self, monkeypatch):
        monkeypatch.setattr(
            apply_cli, "build",
            _raise(apply_cli.ManualApplyOnly("workday — apply by hand")),
        )
        out = apply_cli.run_queue([JOB_ID], submit=False, sleeper=lambda s: None)
        assert out[0].category == "manual"


class TestTheReportSurvivesACrash:
    def test_the_report_holds_every_completed_role_when_the_walk_dies(
        self, monkeypatch, tmp_path
    ):
        # The report is the only record of what went out. A crash on role 3
        # must not erase roles 1 and 2.
        monkeypatch.setattr(apply_cli, "build",
                             lambda job_id, **kw: (_fake_plan(job_id), None))

        def explode(plan, answers, **kw):
            if plan.job_id == "third":
                raise RuntimeError("browser died")
            return _fake_result(submitted=True, confirmed=True)

        monkeypatch.setattr(apply_cli, "run_one", explode)
        monkeypatch.setattr(apply_cli.track_cli, "main", lambda argv: 0)

        collected: list = []
        apply_cli.run_queue(
            ["first", "second", "third"], submit=True, sleeper=lambda s: None,
            collect_into=collected,
        )
        assert [o.category for o in collected] == [
            "submitted", "submitted", "failed",
        ]
        started = apply_cli.datetime(2026, 8, 10, 12, 0, 0)
        text = apply_cli.write_report(collected, started, tmp_path).read_text(encoding="utf-8")
        assert "first" in text and "second" in text and "third" in text


class TestOverridesAreBoundToOneRole:
    def test_a_file_drafted_for_another_role_is_refused(self, tmp_path):
        # Tier C2 answers are company-specific by construction, so reusing a
        # file sends the wrong company's "why us" under the user's name.
        p = tmp_path / "answers.json"
        p.write_text(json.dumps({
            "job_id": "other123",
            "question_1": {"value": "Because I admire their work.", "tier": "C2"},
        }), encoding="utf-8")
        with pytest.raises(apply_cli.ApplyCliError, match="drafted for job_id"):
            apply_cli.load_overrides(p, JOB_ID)

    def test_a_matching_job_id_loads(self, tmp_path):
        p = tmp_path / "answers.json"
        p.write_text(json.dumps({
            "job_id": JOB_ID,
            "question_1": {"value": "An answer.", "tier": "C1"},
        }), encoding="utf-8")
        assert apply_cli.load_overrides(p, JOB_ID) == {
            "question_1": ("An answer.", "C1")}

    def test_a_file_without_a_job_id_still_loads(self, tmp_path):
        # Hand-written override files stay valid; the key is a guard, not a
        # new required field.
        p = tmp_path / "answers.json"
        p.write_text(json.dumps({"question_1": {"value": "x", "tier": "C1"}}),
                      encoding="utf-8")
        assert apply_cli.load_overrides(p, JOB_ID) == {"question_1": ("x", "C1")}

    def test_a_jd_tagged_entry_loads(self, tmp_path):
        # "JD" is the one tier build_plan() lets supersede a static Tier B
        # rules[] match -- a salary figure read from the JD itself.
        p = tmp_path / "answers.json"
        p.write_text(json.dumps({
            "job_id": JOB_ID,
            "salary_expectation": {"value": "145000", "tier": "JD"},
        }), encoding="utf-8")
        assert apply_cli.load_overrides(p, JOB_ID) == {
            "salary_expectation": ("145000", "JD")}

    def test_an_unrecognized_tier_still_raises(self, tmp_path):
        p = tmp_path / "answers.json"
        p.write_text(json.dumps({
            "job_id": JOB_ID,
            "question_1": {"value": "x", "tier": "B0"},
        }), encoding="utf-8")
        with pytest.raises(apply_cli.ApplyCliError, match="want C1, C2, JD, B0-LLM or AUDIT"):
            apply_cli.load_overrides(p, JOB_ID)

    def test_a_b0_llm_tagged_entry_loads(self, tmp_path):
        # "B0-LLM" is the one tier build_plan() lets supersede a Tier B0
        # work-authorization park -- a full-sentence option judged true
        # against plan["work_authorization"], not a static candidate match.
        p = tmp_path / "answers.json"
        p.write_text(json.dumps({
            "job_id": JOB_ID,
            "visa_status": {"value": "Yes, I will need sponsorship in the future.",
                             "tier": "B0-LLM"},
        }), encoding="utf-8")
        assert apply_cli.load_overrides(p, JOB_ID) == {
            "visa_status": ("Yes, I will need sponsorship in the future.", "B0-LLM")}

    def test_an_audit_tagged_entry_loads(self, tmp_path):
        # "AUDIT" supersedes an already-resolved Tier B/B0 field the audit
        # step judged wrong or incomplete -- not tied to one specific
        # category the way "JD" (money) and "B0-LLM" (work-auth wording) are.
        p = tmp_path / "answers.json"
        p.write_text(json.dumps({
            "job_id": JOB_ID,
            "how_did_you_hear": {
                "value": "Careers Page. I'm drawn to the team's focus on...",
                "tier": "AUDIT",
            },
        }), encoding="utf-8")
        assert apply_cli.load_overrides(p, JOB_ID) == {
            "how_did_you_hear": (
                "Careers Page. I'm drawn to the team's focus on...", "AUDIT")}


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
    def test_a_captcha_board_headless_with_submit_is_refused_before_the_browser(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            apply_cli, "build",
            lambda job_id, **kw: (_fake_plan(job_id, ats="lever",
                                              requires_captcha=True), None))
        monkeypatch.setattr(apply_cli, "run_one",
                             _raise(AssertionError("must not open a browser")))
        out = apply_cli.run_queue([JOB_ID], submit=True, headless=True,
                                   sleeper=lambda s: None)
        assert out[0].category == "failed"
        assert "nobody to" in out[0].detail

    def test_headed_is_fine(self, monkeypatch):
        monkeypatch.setattr(
            apply_cli, "build",
            lambda job_id, **kw: (_fake_plan(job_id, ats="lever",
                                              requires_captcha=True), None))
        monkeypatch.setattr(apply_cli, "run_one",
                             lambda plan, answers, **kw: _fake_result())
        out = apply_cli.run_queue([JOB_ID], submit=True, headless=False,
                                   sleeper=lambda s: None)
        assert out[0].category == "ready"


class TestACrashAfterTheClickStillRecordsTheSubmission:
    """The click is the irreversible act. `run_one` publishes its FillResult
    into `sink` before the browser opens, so an exception anywhere after the
    click — including in browser teardown, or a Ctrl-C — cannot erase the fact
    that an application went out. Losing it leaves the role `tailored` and the
    next run applies to the same board again.
    """

    def _plan_only(self, monkeypatch):
        monkeypatch.setattr(apply_cli, "build",
                             lambda job_id, **kw: (_fake_plan(job_id), None))
        monkeypatch.setattr(apply_cli.track_cli, "main", lambda argv: 0)

    def test_teardown_blowing_up_after_a_submit_is_still_a_submission(
        self, monkeypatch
    ):
        self._plan_only(monkeypatch)

        def submit_then_die(plan, answers, sink=None, **kw):
            sink.append(_fake_result(submitted=True))
            raise RuntimeError("Target page has been closed")

        monkeypatch.setattr(apply_cli, "run_one", submit_then_die)
        out = apply_cli.run_queue([JOB_ID], submit=True, sleeper=lambda s: None)
        assert out[0].category == "submitted_unconfirmed"
        assert "SUBMITTED" in out[0].detail

    def test_a_keyboard_interrupt_after_a_submit_is_recorded_then_reraised(
        self, monkeypatch
    ):
        self._plan_only(monkeypatch)
        seen: list = []

        def submit_then_interrupt(plan, answers, sink=None, **kw):
            sink.append(_fake_result(submitted=True))
            raise KeyboardInterrupt

        monkeypatch.setattr(apply_cli, "run_one", submit_then_interrupt)
        out = apply_cli._run_role(JOB_ID, submit=True, headless=False)
        assert out.category == "submitted_unconfirmed"

    def test_a_crash_with_nothing_submitted_is_an_ordinary_failure(
        self, monkeypatch
    ):
        self._plan_only(monkeypatch)

        def die_early(plan, answers, sink=None, **kw):
            sink.append(_fake_result(submitted=False))
            raise TimeoutError("element not found")

        monkeypatch.setattr(apply_cli, "run_one", die_early)
        out = apply_cli.run_queue([JOB_ID], submit=True, sleeper=lambda s: None)
        assert out[0].category == "failed"

    def test_a_keyboard_interrupt_with_nothing_submitted_propagates(
        self, monkeypatch
    ):
        self._plan_only(monkeypatch)

        def interrupt(plan, answers, sink=None, **kw):
            sink.append(_fake_result(submitted=False))
            raise KeyboardInterrupt

        monkeypatch.setattr(apply_cli, "run_one", interrupt)
        with pytest.raises(KeyboardInterrupt):
            apply_cli._run_role(JOB_ID, submit=True, headless=False)
