import pytest
import pandas as pd
from pathlib import Path
import json

from src.discovery.orchestrator import main
from src.discovery import orchestrator

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
