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
from pathlib import Path

import pandas as pd
import pytest
import yaml

from src import apply_cli
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
               unmapped=(), submit_selector="#application-form button[type=submit]"):
    from src.apply.plan import Plan
    return Plan(
        job_id=job_id, board="bushinggroup", token="1", form_url=GH_URL,
        company=company, title=title, out_dir=Path("/tmp"),
        fields=(), files=(), unmapped=tuple(unmapped), draftable=(), skipped=(),
        submit_selector=submit_selector, submit_disabled=False,
    )


def _fake_result(*, submitted=False, failures=(), recovered=(), submit_error=""):
    from src.apply.fill import FillResult
    return FillResult(
        form_url=GH_URL, failures=list(failures), recovered=list(recovered),
        submitted=submitted, submit_error=submit_error,
    )


class TestEligibleQueue:
    def test_tailored_with_both_dirs_and_letters_is_eligible(self, repo):
        repo.write_state()
        assert apply_cli.eligible_queue(repo.pipeline) == [JOB_ID]

    def test_missing_cover_letters_is_not_eligible(self, repo):
        repo.write_state(cover_letters=[])
        assert apply_cli.eligible_queue(repo.pipeline) == []

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

        assert outcomes[0].category == "submitted"
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
