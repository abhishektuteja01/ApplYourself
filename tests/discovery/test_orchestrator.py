import pytest
import pandas as pd
from pathlib import Path
import json

from src.discovery.orchestrator import main
from src.discovery import orchestrator
from src.discovery.schema import make_row
from src.discovery.sources.base import SourceResult

def test_resume_skips_existing_shard(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "JOBS_RAW", tmp_path / "jobs" / "raw")
    monkeypatch.setattr(orchestrator, "JOBS_RUNS", tmp_path / "jobs" / "runs")
    monkeypatch.setattr(orchestrator, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(orchestrator, "PIPELINE", tmp_path / "pipeline")

    (tmp_path / "jobs" / "raw").mkdir(parents=True)
    today_str = pd.Timestamp.today().strftime("%Y-%m-%d_0000")
    # mock a shard for 'manual'
    pd.DataFrame([{"site": "manual"}]).to_parquet(tmp_path / "jobs" / "raw" / f"{today_str}_manual.parquet")

    class MockConfig:
        deadline_hours = 6.0
        sources = {}
        location_allowlist = None
        raw_retention_days = 30

    monkeypatch.setattr("src.discovery.orchestrator.load_config", lambda: MockConfig())

    # Run with resume
    main(["--resume", today_str])

    # Assert manual shard wasn't overwritten
    df = pd.read_parquet(tmp_path / "jobs" / "raw" / f"{today_str}_manual.parquet")
    assert len(df) == 1
    assert "ingested_run_id" not in df.columns # Mock was untouched

def test_deadline_hours_zero_no_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "JOBS_RAW", tmp_path / "jobs" / "raw")
    monkeypatch.setattr(orchestrator, "JOBS_RUNS", tmp_path / "jobs" / "runs")
    monkeypatch.setattr(orchestrator, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(orchestrator, "PIPELINE", tmp_path / "pipeline")

    class MockConfig:
        deadline_hours = 0.0
        sources = {}
        location_allowlist = None
        raw_retention_days = 30

    monkeypatch.setattr("src.discovery.orchestrator.load_config", lambda: MockConfig())

    main([])

    raw_dir = tmp_path / "jobs" / "raw"
    assert len(list(raw_dir.glob("*.parquet"))) == 0 # no shard generated

class _CrashingSource:
    name = "manual"
    def fetch(self, ctx):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

class _WorkingSource:
    name = "linkedin"
    def fetch(self, ctx):
        row = make_row(site="linkedin", company="Acme AI", title="Widget Assembly Consultant",
                       job_url="https://x/1", description="a" * 250)
        row["vertical"] = "example_primary"
        return SourceResult([row], [], [])

def test_crashing_source_does_not_stop_later_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "JOBS_RAW", tmp_path / "jobs" / "raw")
    monkeypatch.setattr(orchestrator, "JOBS_RUNS", tmp_path / "jobs" / "runs")
    monkeypatch.setattr(orchestrator, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(orchestrator, "PIPELINE", tmp_path / "pipeline")

    class MockSourceConfig:
        enabled = True
        pacing_seconds = 0

    class MockConfig:
        deadline_hours = 6.0
        sources = {"linkedin": MockSourceConfig()}
        location_allowlist = None
        raw_retention_days = 30

    monkeypatch.setattr("src.discovery.orchestrator.load_config", lambda: MockConfig())
    monkeypatch.setattr(orchestrator, "get_sources",
                        lambda: [_CrashingSource(), _WorkingSource()])

    main([])

    raw_dir = tmp_path / "jobs" / "raw"
    # No shard for the crashed source, so --resume retries it.
    assert list(raw_dir.glob("*_manual.parquet")) == []
    linkedin_shards = list(raw_dir.glob("*_linkedin.parquet"))
    assert len(linkedin_shards) == 1
    assert len(pd.read_parquet(linkedin_shards[0])) == 1

    report = next((tmp_path / "jobs" / "runs").glob("*.md")).read_text(encoding="utf-8")
    assert "**CRASHED**" in report
    assert "ValueError" in report          # full traceback, not just the message
    assert "in fetch" in report
    # The finally still rebuilt clean.parquet from the surviving shards.
    assert (tmp_path / "jobs" / "clean.parquet").exists()

def test_zero_rows_writes_audit_parquet(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "JOBS_RAW", tmp_path / "jobs" / "raw")
    monkeypatch.setattr(orchestrator, "JOBS_RUNS", tmp_path / "jobs" / "runs")
    monkeypatch.setattr(orchestrator, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(orchestrator, "PIPELINE", tmp_path / "pipeline")

    # Empty inbox, will return 0 rows for manual
    monkeypatch.setattr("src.discovery.inbox.INBOX", tmp_path / "inbox")

    class MockConfig:
        deadline_hours = 6.0
        sources = {}
        location_allowlist = None
        raw_retention_days = 30

    monkeypatch.setattr("src.discovery.orchestrator.load_config", lambda: MockConfig())

    main([])

    raw_dir = tmp_path / "jobs" / "raw"
    shards = list(raw_dir.glob("*_manual.parquet"))
    assert len(shards) == 1
    df = pd.read_parquet(shards[0])
    assert len(df) == 0
    assert "site" in df.columns
    assert "title" in df.columns
    assert "scraped_date" in df.columns

# ---------------------------------------------------------------------
# The finally-block cleaning guarantee + the mid-loop deadline break
# ---------------------------------------------------------------------

@pytest.fixture
def paths(tmp_path, monkeypatch):
    """Redirect every path the orchestrator writes to."""
    monkeypatch.setattr(orchestrator, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "JOBS_RAW", tmp_path / "jobs" / "raw")
    monkeypatch.setattr(orchestrator, "JOBS_RUNS", tmp_path / "jobs" / "runs")
    monkeypatch.setattr(orchestrator, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(orchestrator, "PIPELINE", tmp_path / "pipeline")
    return tmp_path


def _mock_config(deadline_hours=6.0, **sources):
    class MockSourceConfig:
        enabled = True
        pacing_seconds = 0

    class MockConfig:
        location_allowlist = None
        raw_retention_days = 30

    MockConfig.deadline_hours = deadline_hours
    MockConfig.sources = {name: MockSourceConfig() for name in sources or {"linkedin": 1}}
    return MockConfig()


class _NamedSource:
    """A source that records its calls, with a configurable name/behaviour."""

    def __init__(self, name, rows=1, raises=None):
        self.name = name
        self.rows = rows
        self.raises = raises
        self.calls = 0

    def fetch(self, ctx):
        self.calls += 1
        if self.raises:
            raise self.raises
        out = []
        for i in range(self.rows):
            row = make_row(site=self.name, company=f"Acme {i}",
                           title="Widget Assembly Consultant", job_url=f"https://x/{i}",
                           description="a" * 250)
            row["vertical"] = "example_primary"
            out.append(row)
        return SourceResult(out, [f"{self.name}: ok"], [])


def _spy_cleaning(monkeypatch):
    calls = []
    monkeypatch.setattr(orchestrator.cleaning, "run",
                        lambda **kw: calls.append(kw))
    return calls


def test_cleaning_runs_even_when_the_loop_raises(paths, monkeypatch):
    """The try/finally guarantee: an exception outside the per-source
    except Exception (validate_frame, to_parquet) must still leave a run
    report and a rebuilt clean.parquet behind, and must propagate."""
    monkeypatch.setattr("src.discovery.orchestrator.load_config", _mock_config)
    monkeypatch.setattr(orchestrator, "get_sources",
                        lambda: [_NamedSource("manual")])
    monkeypatch.setattr(orchestrator, "validate_frame",
                        lambda df: (_ for _ in ()).throw(RuntimeError("boom")))
    calls = _spy_cleaning(monkeypatch)

    with pytest.raises(RuntimeError, match="boom"):
        main([])

    assert len(calls) == 1
    report = next((paths / "jobs" / "runs").glob("*.md")).read_text(encoding="utf-8")
    assert "# Run " in report


def test_cleaning_runs_when_to_parquet_raises(paths, monkeypatch):
    monkeypatch.setattr("src.discovery.orchestrator.load_config", _mock_config)
    monkeypatch.setattr(orchestrator, "get_sources",
                        lambda: [_NamedSource("manual")])
    monkeypatch.setattr(pd.DataFrame, "to_parquet",
                        lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    calls = _spy_cleaning(monkeypatch)

    with pytest.raises(OSError, match="disk full"):
        main([])

    assert len(calls) == 1


def test_cleaning_runs_when_no_source_is_enabled(paths, monkeypatch):
    """deadline_hours == 0 empties the source list; the finally must still
    rebuild clean.parquet from whatever is already in the window."""
    monkeypatch.setattr("src.discovery.orchestrator.load_config",
                        lambda: _mock_config(deadline_hours=0.0))
    src = _NamedSource("manual")
    monkeypatch.setattr(orchestrator, "get_sources", lambda: [src])
    calls = _spy_cleaning(monkeypatch)

    assert main([]) == 0
    assert src.calls == 0
    assert len(calls) == 1


def test_cleaning_receives_the_run_id_and_redirected_dirs(paths, monkeypatch):
    monkeypatch.setattr("src.discovery.orchestrator.load_config", _mock_config)
    monkeypatch.setattr(orchestrator, "get_sources",
                        lambda: [_NamedSource("manual")])
    calls = _spy_cleaning(monkeypatch)

    main(["--resume", "2026-01-01_0000"])

    assert calls[0]["run_id"] == "2026-01-01_0000"
    assert calls[0]["raw_dir"] == paths / "jobs" / "raw"
    assert calls[0]["clean_dir"] == paths / "jobs"
    assert calls[0]["runs_dir"] == paths / "jobs" / "runs"
    assert calls[0]["pipeline_dir"] == paths / "pipeline"


def test_a_raising_cleaning_run_propagates(paths, monkeypatch):
    """Cleaning is the last thing standing between a scrape and
    clean.parquet — a failure there must not be swallowed."""
    monkeypatch.setattr("src.discovery.orchestrator.load_config", _mock_config)
    monkeypatch.setattr(orchestrator, "get_sources",
                        lambda: [_NamedSource("manual")])
    monkeypatch.setattr(orchestrator.cleaning, "run",
                        lambda **kw: (_ for _ in ()).throw(ValueError("bad shard")))

    with pytest.raises(ValueError, match="bad shard"):
        main([])


def test_deadline_reached_breaks_before_the_next_source(paths, monkeypatch):
    """The mid-loop break: source 1 runs to completion, then the budget is
    spent, so source 2 is never fetched."""
    monkeypatch.setattr("src.discovery.orchestrator.load_config",
                        lambda: _mock_config(deadline_hours=1e-9, linkedin=1))
    first, second = _NamedSource("manual"), _NamedSource("linkedin")
    monkeypatch.setattr(orchestrator, "get_sources", lambda: [first, second])
    _spy_cleaning(monkeypatch)

    main([])

    assert first.calls == 1
    assert second.calls == 0
    report = next((paths / "jobs" / "runs").glob("*.md")).read_text(encoding="utf-8")
    assert "**DEADLINE REACHED** after manual." in report
    # source 1's shard is still banked
    assert len(list((paths / "jobs" / "raw").glob("*_manual.parquet"))) == 1
    assert list((paths / "jobs" / "raw").glob("*_linkedin.parquet")) == []


def test_deadline_reached_breaks_on_the_crash_path_too(paths, monkeypatch):
    """A crashing source must not outrun the budget: the deadline check is
    repeated inside the except handler."""
    monkeypatch.setattr("src.discovery.orchestrator.load_config",
                        lambda: _mock_config(deadline_hours=1e-9, linkedin=1))
    first = _NamedSource("manual", raises=ValueError("kaboom"))
    second = _NamedSource("linkedin")
    monkeypatch.setattr(orchestrator, "get_sources", lambda: [first, second])
    _spy_cleaning(monkeypatch)

    main([])

    assert first.calls == 1
    assert second.calls == 0
    report = next((paths / "jobs" / "runs").glob("*.md")).read_text(encoding="utf-8")
    assert "**CRASHED**" in report
    assert "**DEADLINE REACHED** after manual." in report


def test_no_deadline_line_when_the_budget_holds(paths, monkeypatch):
    monkeypatch.setattr("src.discovery.orchestrator.load_config",
                        lambda: _mock_config(deadline_hours=6.0, linkedin=1))
    first, second = _NamedSource("manual"), _NamedSource("linkedin")
    monkeypatch.setattr(orchestrator, "get_sources", lambda: [first, second])
    _spy_cleaning(monkeypatch)

    main([])

    assert (first.calls, second.calls) == (1, 1)
    report = next((paths / "jobs" / "runs").glob("*.md")).read_text(encoding="utf-8")
    assert "DEADLINE REACHED" not in report
    assert "manual: ok" in report and "linkedin: ok" in report


def test_resume_appends_to_an_existing_report(paths, monkeypatch):
    """The finally's resume branch appends rather than overwriting, so the
    first run's source lines survive a --resume."""
    monkeypatch.setattr("src.discovery.orchestrator.load_config", _mock_config)
    monkeypatch.setattr(orchestrator, "get_sources",
                        lambda: [_NamedSource("manual")])
    _spy_cleaning(monkeypatch)

    main([])
    run_id = next((paths / "jobs" / "runs").glob("*.md")).stem
    first_text = (paths / "jobs" / "runs" / f"{run_id}.md").read_text(encoding="utf-8")

    # Second pass: the manual shard exists, so resume skips it.
    main(["--resume", run_id])
    text = (paths / "jobs" / "runs" / f"{run_id}.md").read_text(encoding="utf-8")
    assert text.startswith(first_text)
    assert text.count("# Run ") == 1  # header not duplicated
