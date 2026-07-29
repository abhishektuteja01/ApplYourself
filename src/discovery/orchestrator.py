import logging
import argparse
import time
from datetime import datetime
from pathlib import Path
import pandas as pd

from src.discovery.config import load_config
from src.discovery import cleaning
from src.discovery.inbox import InboxSource
from src.discovery.sources.jobspy_source import LinkedinSource, IndeedSource, ZipRecruiterSource
from src.discovery.sources.ats.greenhouse import GreenhouseSource
from src.discovery.sources.ats.lever import LeverSource
from src.discovery.sources.ats.ashby import AshbySource
from src.discovery.schema import validate_frame, COLUMNS
from src import verticals

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
JOBS_RAW = REPO_ROOT / "jobs" / "raw"
JOBS_RUNS = REPO_ROOT / "jobs" / "runs"
JOBS_ROOT = REPO_ROOT / "jobs"
PIPELINE = REPO_ROOT / "pipeline"

def current_run_id(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d_%H%M")

class Context:
    def __init__(self, config, deadline_ts: float):
        self.config = config
        self.deadline_ts = deadline_ts
        self.verticals = verticals.get_config()
        
    def deadline_reached(self) -> bool:
        if self.deadline_ts == 0.0:
            return False
        return time.time() > self.deadline_ts

def get_sources():
    return [InboxSource(), LinkedinSource(), IndeedSource(), ZipRecruiterSource(), GreenhouseSource(), LeverSource(), AshbySource()]

def main(args=None):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", type=str, help="Resume run ID")
    parsed = parser.parse_args(args)
    
    config = load_config()
    
    start_time = datetime.now()
    run_id = parsed.resume or current_run_id(start_time)
    scraped_date = pd.Timestamp(start_time).normalize()
    
    deadline_ts = 0.0
    if config.deadline_hours > 0:
        deadline_ts = time.time() + config.deadline_hours * 3600
        
    ctx = Context(config=config, deadline_ts=deadline_ts)
    
    JOBS_RAW.mkdir(parents=True, exist_ok=True)
    JOBS_RUNS.mkdir(parents=True, exist_ok=True)
    
    report_lines = [
        f"# Run {run_id}",
        "",
        "## Discovery",
        "",
        f"Raw archive: `{JOBS_RAW.relative_to(REPO_ROOT) if JOBS_RAW.is_relative_to(REPO_ROOT) else JOBS_RAW}`",
        "",
    ]
    
    health_path = JOBS_ROOT / "universe_health.parquet"
    if health_path.exists():
        try:
            health_df = pd.read_parquet(health_path)
            pruned_count = health_df["pruned_at"].notna().sum()
            report_lines.append(f"Universe Health: {pruned_count} companies pruned (3x 404s)")
            report_lines.append("")
        except Exception as e:
            log.warning(f"Could not read universe health for report: {e}")
    
    sources = get_sources()
    fixed_order = ["manual", "linkedin", "indeed", "zip_recruiter", "greenhouse", "lever", "ashby"]
    
    enabled_sources = []
    source_map = {s.name: s for s in sources}
    for name in fixed_order:
        if name == "manual":
            if name in source_map:
                enabled_sources.append(source_map[name])
        else:
            if name in config.sources and config.sources[name].enabled:
                if name in source_map:
                    enabled_sources.append(source_map[name])

    try:
        if config.deadline_hours == 0:
            enabled_sources = []

        for source in enabled_sources:
            shard_file = JOBS_RAW / f"{run_id}_{source.name}.parquet"
            if parsed.resume and shard_file.exists():
                log.info(f"Skipping {source.name}, shard exists.")
                continue
                
            t0 = time.time()
            res = source.fetch(ctx)
            dur = time.time() - t0
            
            df = pd.DataFrame(res.rows) if res.rows else pd.DataFrame()
            if not df.empty:
                df["ingested_run_id"] = run_id
                df["scraped_date"] = scraped_date
                df = validate_frame(df)
            else:
                df = pd.DataFrame(columns=COLUMNS + ["ingested_run_id", "scraped_date"])
                df = validate_frame(df)
                
            df.to_parquet(shard_file, index=False)
            
            report_lines.append(f"### Source: {source.name}")
            report_lines.append(f"Time: {dur:.1f}s")
            if df.empty:
                report_lines.append("ZERO rows (likely rate-limited or no results)")
            else:
                report_lines.append(f"Rows: {len(df)}")
            if res.errors:
                report_lines.append("Errors:")
                for e in res.errors:
                    report_lines.append(f"- {e}")
            if res.report_lines:
                report_lines.extend(res.report_lines)
            report_lines.append("")
            
            if ctx.deadline_reached():
                report_lines.append(f"**DEADLINE REACHED** after {source.name}.")
                break
                
    finally:
        report_path = JOBS_RUNS / f"{run_id}.md"
        
        # Read existing report and append if resuming
        if parsed.resume and report_path.exists():
            report_path.write_text(report_path.read_text() + "\n" + "\n".join(report_lines[6:]) + "\n")
        else:
            report_path.write_text("\n".join(report_lines) + "\n")
            
        cleaning.run(
            run_id=run_id,
            raw_dir=JOBS_RAW,
            clean_dir=JOBS_ROOT,
            runs_dir=JOBS_RUNS,
            pipeline_dir=PIPELINE,
        )
    
    return 0
