from __future__ import annotations

import logging
import argparse
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
import pandas as pd

from src.discovery.config import load_config
from src.discovery import cleaning
from src.discovery.inbox import InboxSource
from src.discovery.sources.jobspy_source import LinkedinSource, IndeedSource
from src.discovery.sources.ats.greenhouse import GreenhouseSource
from src.discovery.sources.ats.lever import LeverSource
from src.discovery.sources.ats.ashby import AshbySource
from src.discovery.schema import validate_frame, COLUMNS
from src import verticals
from src.parquet_io import write_parquet
from src import paths

log = logging.getLogger(__name__)

REPO_ROOT = paths.REPO_ROOT
JOBS_RAW = paths.JOBS_RAW
JOBS_RUNS = paths.JOBS_RUNS
JOBS_ROOT = paths.JOBS
PIPELINE = paths.PIPELINE

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
    return [InboxSource(), LinkedinSource(), IndeedSource(), GreenhouseSource(), LeverSource(), AshbySource()]

def main(args=None):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", type=str, help="Resume run ID")
    parsed = parser.parse_args(args)
    
    try:
        config = load_config()
    except (FileNotFoundError, ValueError) as e:
        # load_config is a library function that raises; the CLI is where a bad
        # config becomes a one-line message and a nonzero exit.
        sys.exit(f"ERROR: {e}")
    
    start_time = datetime.now()
    run_id = parsed.resume or current_run_id(start_time)
    scraped_date = pd.Timestamp(start_time).normalize()
    
    # deadline_hours == 0 is the scrape-nothing kill switch: no source runs, and
    # the finally below still rebuilds clean.parquet from the existing window.
    # deadline_ts is only meaningful when it is positive, and deadline_reached()
    # reads 0.0 as "no deadline set" for exactly the case that never scrapes.
    scrape_enabled = config.deadline_hours > 0
    deadline_ts = time.time() + config.deadline_hours * 3600 if scrape_enabled else 0.0
        
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
            report_lines.append(f"Universe Health: {pruned_count} companies pruned (3x dead board)")
            report_lines.append("")
        except (OSError, ValueError, KeyError, yaml.YAMLError) as e:
            log.warning("Could not read universe health for report: %s", e)
    
    sources = get_sources()
    fixed_order = ["manual", "linkedin", "indeed", "greenhouse", "lever", "ashby"]
    
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

    if not scrape_enabled:
        log.info("deadline_hours == 0 — skipping every source, cleaning only.")
        report_lines.append("**deadline_hours == 0** — no source polled this run.")
        enabled_sources = []

    try:
        for source in enabled_sources:
            shard_file = JOBS_RAW / f"{run_id}_{source.name}.parquet"
            if parsed.resume and shard_file.exists():
                log.info("Skipping %s, shard exists.", source.name)
                continue
                
            t0 = time.time()
            try:
                res = source.fetch(ctx)
            except Exception:  # noqa: BLE001 — deliberate per-source containment
                # Broad on purpose: one source's crash must not cost the other
                # five their shards. No shard is written, so --resume retries it.
                log.exception("Source %s crashed; continuing.", source.name)
                report_lines.extend([
                    f"### Source: {source.name}",
                    f"Time: {time.time() - t0:.1f}s",
                    "**CRASHED** — no shard written, source skipped this run",
                    "```",
                    traceback.format_exc().rstrip(),
                    "```",
                    "",
                ])
                if ctx.deadline_reached():
                    report_lines.append(f"**DEADLINE REACHED** after {source.name}.")
                    break
                continue
            dur = time.time() - t0

            df = pd.DataFrame(res.rows) if res.rows else pd.DataFrame()
            if not df.empty:
                df["ingested_run_id"] = run_id
                df["scraped_date"] = scraped_date
                df = validate_frame(df)
            else:
                df = pd.DataFrame(columns=COLUMNS + ["ingested_run_id", "scraped_date"])
                df = validate_frame(df)
                
            write_parquet(df, shard_file)
            
            report_lines.append(f"### Source: {source.name}")
            report_lines.append(f"Time: {dur:.1f}s")
            if df.empty:
                if source.name == "manual":
                    report_lines.append("ZERO rows (inbox empty)")
                else:
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
            report_path.write_text(
                report_path.read_text(encoding="utf-8") + "\n" + "\n".join(report_lines[6:]) + "\n",
                encoding="utf-8",
            )
        else:
            report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
            
        cleaning.run(
            run_id=run_id,
            raw_dir=JOBS_RAW,
            clean_dir=JOBS_ROOT,
            runs_dir=JOBS_RUNS,
            pipeline_dir=PIPELINE,
        )
    
    return 0
