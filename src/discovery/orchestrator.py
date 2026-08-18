from __future__ import annotations

import logging
import argparse
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import pandas as pd

from src.discovery.config import load_config
from src.discovery import cleaning
from src.discovery.inbox import InboxSource
from src.discovery.sources.jobspy_source import LinkedinSource, IndeedSource
from src.discovery.sources.ats.greenhouse import GreenhouseSource
from src.discovery.sources.ats.lever import LeverSource
from src.discovery.sources.ats.ashby import AshbySource
from src.discovery.sources.ats.workday import WorkdaySource
from src.discovery.sources.ats.registry import ATS_SOURCE_NAMES
from src.discovery import universe
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

RUN_ID_FMT = "%Y-%m-%d_%H%M"
# Shards are named "<run_id>_<source>.parquet" and cleaning only reads back the
# ones whose name parses as this shape, so a malformed --resume id scrapes into
# files nothing will ever load.
_RUN_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}$")


def current_run_id(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime(RUN_ID_FMT)

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
    return [InboxSource(), LinkedinSource(), IndeedSource(), GreenhouseSource(), LeverSource(),
            AshbySource(), WorkdaySource()]


def _run_source(source, ctx, run_id, scraped_date, shard_file) -> dict:
    """One source, start to shard. Runs on its own thread for everything but
    the inbox.

    A fetch failure is *contained* and returned as data, so one lane's crash
    costs the others nothing. Anything raised past the fetch — validate_frame,
    write_parquet — is deliberately **not** caught: it propagates out of the
    future, aborts the run, and the caller's finally still guarantees cleaning.
    """
    t0 = time.time()
    try:
        res = source.fetch(ctx)
    except Exception:  # noqa: BLE001 — deliberate per-source containment
        log.exception("Source %s crashed; continuing.", source.name)
        return {"duration": time.time() - t0, "crash": traceback.format_exc().rstrip()}

    outcome = {
        "duration": time.time() - t0,
        "crash": None,
        "result": res,
        # Read at this lane's own finish, not after the join: a lane that
        # completed inside the budget must not be labelled by a later one.
        "truncated": ctx.deadline_reached(),
    }

    df = pd.DataFrame(res.rows) if res.rows else pd.DataFrame()
    if not df.empty:
        df["ingested_run_id"] = run_id
        df["scraped_date"] = scraped_date
        df = validate_frame(df)
    else:
        df = pd.DataFrame(columns=COLUMNS + ["ingested_run_id", "scraped_date"])
        df = validate_frame(df)

    write_parquet(df, shard_file)
    outcome["rows"] = 0 if df.empty else len(df)
    # A permanent-only health strike (universe.update_health) misses this: a
    # wholesale transient block (429/403/400 across every request) still
    # reports success=True there. Zero rows with errors recorded is the only
    # place that distinguishes "nothing matched" from "everything errored" —
    # without it, a night like Workday's 2640/2640 HTTP 400s logs nothing
    # above trace level.
    if outcome["rows"] == 0 and res.errors:
        log.warning(
            "%s: 0 rows kept despite %d error(s) — likely every request "
            "failing, not an empty crawl. First: %s",
            source.name, len(res.errors), res.errors[0],
        )
    return outcome


def _render_source(name: str, outcome: dict) -> list[str]:
    """One `### Source:` section. Lanes finish out of order, so sections are
    rendered from the collected outcomes in fixed_order, never as they land."""
    lines = [f"### Source: {name}", f"Time: {outcome['duration']:.1f}s"]

    if outcome["crash"] is not None:
        lines += ["**CRASHED** — no shard written, source skipped this run",
                  "```", outcome["crash"], "```", ""]
        return lines

    if not outcome["rows"]:
        lines.append("ZERO rows (inbox empty)" if name == "manual"
                     else "ZERO rows (likely rate-limited or no results)")
    else:
        lines.append(f"Rows: {outcome['rows']}")

    res = outcome["result"]
    if res.errors:
        lines.append("Errors:")
        lines.extend(f"- {e}" for e in res.errors)
    if res.report_lines:
        lines.extend(res.report_lines)
    if outcome["truncated"]:
        lines.append("**DEADLINE REACHED** — partial shard, this source was cut short")
    lines.append("")
    return lines

def main(args=None):
    # asctime is load-bearing: urllib3's retry warnings propagate to root, and
    # without a timestamp on them a slow night cannot be reconstructed after
    # the fact — which is exactly what happened on 2026-08-08.
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", type=str, metavar="YYYY-MM-DD_HHMM",
                        help="Resume run ID")
    parsed = parser.parse_args(args)

    if parsed.resume is not None and not _RUN_ID_RE.match(parsed.resume):
        sys.exit(f"ERROR: --resume {parsed.resume!r} is not a run id. "
                 "Expected YYYY-MM-DD_HHMM, as printed by the run being resumed.")

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

    # One ledger per ATS since the sources run concurrently, so the prune count
    # is a sum. Via universe.health_path so the test fixture's HEALTH_DIR patch
    # covers this read too.
    pruned_count = 0
    found_ledger = False
    for ats in ATS_SOURCE_NAMES:
        path = universe.health_path(ats)
        if not path.exists():
            continue
        try:
            pruned_count += int(pd.read_parquet(path)["pruned_at"].notna().sum())
            found_ledger = True
        except (OSError, ValueError, KeyError) as e:
            log.warning("Could not read %s universe health for report: %s", ats, e)
    if found_ledger:
        report_lines.append(f"Universe Health: {pruned_count} companies pruned (3x dead board)")
        report_lines.append("")

    # Everything above is the run preamble; a resume appends only what follows.
    preamble_len = len(report_lines)

    sources = get_sources()
    fixed_order = ["manual", "linkedin", "indeed", "greenhouse", "lever", "ashby", "workday"]

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

    def pending(source) -> bool:
        shard_file = JOBS_RAW / f"{run_id}_{source.name}.parquet"
        if parsed.resume and shard_file.exists():
            log.info("Skipping %s, shard exists.", source.name)
            return False
        return True

    def lane_args(source):
        return (source, ctx, run_id, scraped_date,
                JOBS_RAW / f"{run_id}_{source.name}.parquet")

    outcomes: dict[str, dict] = {}
    not_started: list[str] = []
    run_t0 = time.time()

    try:
        # The inbox *moves* what it reads into .processed/, so it stays serial
        # and first — it is the one source doing local filesystem mutation, and
        # it costs no measurable time anyway.
        for source in [s for s in enabled_sources if s.name == "manual"]:
            if pending(source):
                outcomes[source.name] = _run_source(*lane_args(source))

        # Rate limits are per-host and these five hit unrelated services, so
        # nothing is gained by serializing them. One thread each: the work is
        # entirely I/O-bound.
        lanes = [s for s in enabled_sources if s.name != "manual" and pending(s)]
        if lanes and ctx.deadline_reached():
            # Budget already spent, so starting a lane would only write an
            # empty shard that --resume would then skip.
            not_started = [s.name for s in lanes]
        elif lanes:
            with ThreadPoolExecutor(max_workers=len(lanes)) as pool:
                futures = {pool.submit(_run_source, *lane_args(s)): s for s in lanes}
                # A raise here is a write/validate failure, not a fetch failure
                # (_run_source contains those). Let it out: the pool's __exit__
                # joins the other lanes so their shards land, then the finally
                # still writes the report and runs cleaning.
                for future, source in futures.items():
                    outcomes[source.name] = future.result()

    finally:
        if outcomes:
            # The per-source Time: values overlap now, so they no longer sum to
            # the run. This is the number that says whether the lanes helped.
            serial = sum(o["duration"] for o in outcomes.values())
            report_lines.append(
                f"Wall time: {time.time() - run_t0:.1f}s "
                f"(sources ran concurrently; {serial:.1f}s if summed serially)")
            report_lines.append("")

        for name in fixed_order:
            if name in outcomes:
                report_lines.extend(_render_source(name, outcomes[name]))
        if not_started:
            report_lines.append(
                f"**DEADLINE REACHED** before {', '.join(not_started)} started — "
                "no shard written, --resume will retry them.")

        report_path = JOBS_RUNS / f"{run_id}.md"

        if parsed.resume and report_path.exists():
            report_path.write_text(
                report_path.read_text(encoding="utf-8") + "\n" + "\n".join(report_lines[preamble_len:]) + "\n",
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
