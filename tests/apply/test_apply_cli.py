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

        def write_state(self, job_id=JOB_ID, url=GH_URL, tailored_dirs=None):
            d = pipeline / job_id
            d.mkdir(parents=True, exist_ok=True)
            (d / "state.yaml").write_text(yaml.safe_dump({
                "job_id": job_id, "company": "Bushing Group", "title": "Widget Engineer",
                "state": "tailored", "url": url,
                "tailored_dirs": [tailored] if tailored_dirs is None else tailored_dirs,
                "cover_letters": [],
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

    def test_run_is_not_wired_up_yet(self):
        with pytest.raises(SystemExit) as exc:
            apply_cli.main(["run"])
        assert exc.value.code == 2


def _raise(exc):
    def raiser(*args, **kwargs):
        raise exc
    return raiser
