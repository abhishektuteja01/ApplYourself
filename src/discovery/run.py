"""`uv run discover`: check tonight's due boards and searches, save what they
find into the jobs store as it arrives, then rebuild clean.parquet and write
the run report.

    uv run discover [--deadline-hours H] [--source NAME ...] [--max-terms N]
                    [--jobs-dir D] [--boards-dir D]

Lanes run on their own threads; a writer saves the store every few minutes,
so a killed run loses at most that much and the next run simply finds the
unchecked boards still due. Exits nonzero only when preflight fails or the
store cannot be written.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

import pandas as pd

from src import ats_http, paths, verticals
from src.ats_registry import ATS_SOURCE_NAMES
from src.discovery import store as S
from src.discovery.config import FEED_SOURCES, JOBSPY_SOURCES, SOURCE_NAMES, load_config
from src.discovery.sources import boards as B
from src.discovery.sources.ashby import AshbyLane
from src.discovery.sources.greenhouse import GreenhouseLane
from src.discovery.sources.inbox import InboxLane
from src.discovery.sources.lever import LeverLane
from src.discovery.sources.stapply import StapplyLane
from src.discovery.sources.workday import WorkdayLane
from src.parquet_io import locked, write_parquet
from src.state_io import load_state_index

log = logging.getLogger(__name__)

RUN_ID_FMT = "%Y-%m-%d_%H%M"
LANE_NAMES = ("manual", *SOURCE_NAMES)
FLUSH_SECONDS = 300


def current_run_id(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime(RUN_ID_FMT)


@dataclass
class Paths:
    """The canonical file names under `jobs`, so a shadow run keeps its own.
    `boards_dir` None means the board lists' own default."""

    jobs: Path
    boards_dir: Path | None = None
    pipeline: Path = paths.PIPELINE

    @property
    def store(self) -> Path:
        return self.jobs / paths.STORE.name

    @property
    def clean(self) -> Path:
        return self.jobs / paths.CLEAN.name

    @property
    def scored(self) -> Path:
        return self.jobs / paths.SCORED.name

    @property
    def board_state(self) -> Path:
        return self.jobs / paths.BOARD_STATE.name

    @property
    def raw(self) -> Path:
        return self.jobs / paths.JOBS_RAW.name

    @property
    def runs(self) -> Path:
        return self.jobs / paths.JOBS_RUNS.name


def _lane(name: str):
    if name in JOBSPY_SOURCES:
        from src.discovery.sources.search import SearchLane  # jobspy is slow to import
        return SearchLane(name)
    return {"greenhouse": GreenhouseLane, "lever": LeverLane, "ashby": AshbyLane,
            "workday": WorkdayLane, "stapply": StapplyLane}[name]()


def _cap_terms(vcfg, n: int):
    capped = {name: replace(v, search_terms=v.search_terms[:n], linkedin_terms=v.linkedin_terms[:n],
                            abroad_terms=v.abroad_terms[:n])
              for name, v in vcfg.verticals.items()}
    return replace(vcfg, verticals=capped)


class Writer:
    """Saves the store and the board state from the lanes' shared batch:
    every FLUSH_SECONDS on its own thread, and once more at the end. Store
    first, boards second, so a board is never marked checked while its
    postings are missing."""

    def __init__(self, crawl, p: Paths, today, tracked, live_days: int):
        self.crawl, self.p, self.today = crawl, p, today
        self.tracked, self.live_days = tracked, live_days
        self.stats = S.ApplyStats()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(FLUSH_SECONDS):
            try:
                self.flush()
            except Exception:  # noqa: BLE001 -- the final flush re-raises
                log.exception("periodic store flush failed; will retry")

    def flush(self) -> None:
        with self._lock:
            results = self.crawl.take_board_results()
            if len(self.crawl.batch):
                self.stats.merge(S.flush(self.crawl.batch, self.today, self.tracked,
                                         self.live_days, path=self.p.store))
            B.update_state(results, self.today, path=self.p.board_state)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self.flush()


def _run_lane(lane, crawl) -> dict:
    t0 = time.time()
    tally = ats_http.HttpTally()
    try:
        with ats_http.observe(tally.on_http):
            stats = lane.run(crawl, tally) if lane.name in JOBSPY_SOURCES else lane.run(crawl)
    except Exception:  # noqa: BLE001 -- one lane's crash costs the others nothing
        log.exception("lane %s crashed; continuing", lane.name)
        return {"status": "crashed", "duration_s": round(time.time() - t0, 1),
                "crash": traceback.format_exc().rstrip(), "http": tally.totals()}
    out = asdict(stats)
    out.update(status="ok" if stats.passed else "zero",
               duration_s=round(time.time() - t0, 1), http=tally.totals())
    return out


def _write_raw(listed: list[dict], p: Paths, run_id: str, today, keep_days: int) -> int:
    """Tonight's classified listings, pass or fail, for debugging and the
    onboarding level-band count. Nothing in the pipeline reads them."""
    p.raw.mkdir(parents=True, exist_ok=True)
    if listed:
        write_parquet(pd.DataFrame(listed), p.raw / f"{run_id}.parquet")
    cutoff = pd.Timestamp(today) - pd.Timedelta(days=keep_days)
    pruned = 0
    for f in p.raw.glob("*.parquet"):
        try:
            day = pd.Timestamp(f.name[:10])
        except ValueError:
            continue
        if day < cutoff:
            f.unlink(missing_ok=True)
            pruned += 1
    return pruned


def rebuild_view(cfg, p: Paths, today, state_index: dict) -> tuple[dict, dict]:
    """Refresh remembered scores, forget long-gone postings, then write
    clean.parquet. Returns (store stats, view funnel)."""
    from src.discovery.location import Allowlist

    tracked = frozenset(state_index)
    scores = S.read_scores(p.scored)
    with locked(p.store):
        store = S.remember_scores(S.load_store(p.store), scores)
        store, forgotten = S.forget(store, today, cfg.forget_after_days, tracked)
        S.save_store(store, p.store)
    inp = S.inputs_from(cfg, today, state_index, p.scored)
    view, funnel = S.build_view(store, Allowlist(cfg), inp)
    S.write_view(view, p.clean)
    return {"postings": len(store), "job_ids": int(store["job_id"].nunique()),
            "forgotten": forgotten}, funnel


def render_md(report: dict) -> str:
    lines = [f"# Run {report['run_id']}", "",
             f"Wall time: {report['wall_time_s']:.0f}s | clean rows: {report['view'].get('clean', 0)}"
             f" | new job ids: {len(report['new_job_ids'])}", ""]
    if report["overrides"]:
        lines += [f"Overrides (this run only): {', '.join(report['overrides'])}", ""]
    lines += ["| lane | status | time | listed | passed | known | new | detail ok/failed | errors |",
              "|---|---|---|---|---|---|---|---|---|"]
    for name, lane in report["lanes"].items():
        lines.append(
            f"| {name} | {lane['status']}{' (cut short)' if lane.get('truncated') else ''} "
            f"| {lane.get('duration_s', 0):.0f}s | {lane.get('listed', 0)} | {lane.get('passed', 0)} "
            f"| {lane.get('known', 0)} | {lane.get('new', 0)} "
            f"| {lane.get('detail_ok', 0)}/{lane.get('detail_failed', 0)} "
            f"| {len(lane.get('errors', []))} |")
    lines += ["", "## Boards", "",
              "| lane | due | checked | ok | empty | 404 | error |", "|---|---|---|---|---|---|---|"]
    for name, lane in report["lanes"].items():
        if "boards_due" in lane and name in ATS_SOURCE_NAMES:
            lines.append(f"| {name} | {lane['boards_due']} | {lane['boards_checked']} "
                         f"| {lane['boards_ok']} | {lane['boards_empty']} | {lane['boards_404']} "
                         f"| {lane['boards_error']} |")
    lines += ["", "## HTTP", "", "| lane | requests | seconds | 403 | 429 | errors |",
              "|---|---|---|---|---|---|"]
    for name, lane in report["lanes"].items():
        h = lane.get("http") or {}
        if h.get("requests"):
            lines.append(f"| {name} | {h['requests']} | {h['seconds']:.0f} | {h['403']} "
                         f"| {h['429']} | {h['errors']} |")
    st = report["store"]
    lines += ["", "## Store", "",
              f"- inserted {st.get('inserted', 0)}, refreshed {st.get('refreshed', 0)}, "
              f"touched {st.get('touched', 0)}, reposted {st.get('reposted', 0)}, "
              f"forgotten {st.get('forgotten', 0)}",
              f"- new postings by id rule: {st.get('by_rule', {})}",
              f"- postings {st.get('postings', 0)}, job ids {st.get('job_ids', 0)}"]
    if st.get("collisions"):
        lines += ["- **job_id collisions dropped:**"] + [f"  - {c}" for c in st["collisions"]]
    lines += ["", "## Clean view", ""]
    lines += [f"- {k}: {v}" for k, v in report["view"].items()]
    for name, lane in report["lanes"].items():
        if lane.get("crash") or lane.get("errors") or lane.get("notes"):
            lines += ["", f"### {name}", ""]
            lines += [f"- {n}" for n in lane.get("notes", [])]
            if lane.get("capped"):
                lines += ["- hit the cap:"] + [f"  - {c}" for c in lane["capped"]]
            lines += [f"- error: {e}" for e in lane.get("errors", [])[:50]]
            if len(lane.get("errors", [])) > 50:
                lines.append(f"- ... {len(lane['errors']) - 50} more errors")
            if lane.get("crash"):
                lines += ["", "```", lane["crash"], "```"]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="discover")
    parser.add_argument("--deadline-hours", type=float, metavar="H",
                        help="this run's budget; 0 checks nothing and only rebuilds clean.parquet")
    parser.add_argument("--source", action="append", metavar="NAME",
                        help=f"run only these lanes, repeatable: {', '.join(LANE_NAMES)}")
    parser.add_argument("--max-terms", type=int, metavar="N",
                        help="use only each lane's first N search terms")
    parser.add_argument("--jobs-dir", type=Path, default=paths.JOBS,
                        help="read and write the store, view and reports here")
    parser.add_argument("--boards-dir", type=Path, default=B.BOARDS_DIR,
                        help="where boards.csv and boards.local.csv live")
    args = parser.parse_args(argv)

    unknown = sorted(set(args.source or []) - set(LANE_NAMES))
    if unknown:
        sys.exit(f"ERROR: unknown --source {', '.join(unknown)}. One of: {', '.join(LANE_NAMES)}.")
    if args.deadline_hours is not None and args.deadline_hours < 0:
        sys.exit("ERROR: --deadline-hours must be >= 0.")
    if args.max_terms is not None and args.max_terms < 1:
        sys.exit("ERROR: --max-terms must be >= 1.")
    try:
        cfg = load_config()
        from src.discovery.location import require_libpostal
        require_libpostal()
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        sys.exit(f"ERROR: {e}")
    problems = cfg.validate()
    if problems:
        sys.exit("ERROR: discovery config problems (run `uv run discovery-check`):\n"
                 + "\n".join(f"  {p}" for p in problems))

    started = datetime.now()
    run_id = current_run_id(started)
    today = pd.Timestamp(started.date())
    p = Paths(jobs=args.jobs_dir, boards_dir=args.boards_dir)
    p.runs.mkdir(parents=True, exist_ok=True)
    print(f"run id: {run_id}", flush=True)

    overrides = []
    if args.deadline_hours is not None:
        cfg.deadline_hours = args.deadline_hours
        overrides.append(f"--deadline-hours {args.deadline_hours:g}")
    vcfg = verticals.get_config()
    if args.max_terms:
        vcfg = _cap_terms(vcfg, args.max_terms)
        overrides.append(f"--max-terms {args.max_terms}")
    if args.source:
        overrides.append("--source " + " ".join(args.source))
        wanted = [n for n in LANE_NAMES if n in args.source]
    else:
        wanted = [n for n in LANE_NAMES if n == "manual"
                  or (n in cfg.sources and cfg.sources[n].enabled)]
    if cfg.deadline_hours <= 0:
        wanted = []
    deadline_ts = time.time() + cfg.deadline_hours * 3600 if wanted else 0.0

    state_index = load_state_index(p.pipeline)
    tracked = frozenset(state_index)
    store = S.load_store(p.store)
    known = {k: {"updated_at": u, "company": c}
             for k, u, c in zip(store["posting_key"], store["updated_at"], store["company"])}
    del store
    crawl = B.Crawl(cfg, vcfg, today, deadline_ts, known, S.Batch(),
                    board_state=p.board_state, boards_dir=p.boards_dir)
    writer = Writer(crawl, p, today, tracked, cfg.live_days)

    lanes: dict[str, dict] = {}
    t0 = time.time()
    writer.start()
    inbox = InboxLane() if "manual" in wanted else None
    if inbox is not None:
        lanes["manual"] = _run_lane(inbox, crawl)
    threaded = [_lane(n) for n in wanted if n != "manual"]
    for lane in threaded:
        if lane.name in FEED_SOURCES:
            # Before the board lanes start: they skip the boards it covered.
            lane.prepare(crawl)
    if threaded:
        with ThreadPoolExecutor(max_workers=len(threaded)) as pool:
            futures = {lane.name: pool.submit(_run_lane, lane, crawl) for lane in threaded}
            for name, future in futures.items():
                lanes[name] = future.result()
    writer.close()
    if inbox is not None:
        inbox.done()

    store_stats, funnel = rebuild_view(cfg, p, today, state_index)
    pruned = _write_raw(crawl.take_listed(), p, run_id, today, cfg.raw_keep_days)
    applied = asdict(writer.stats)
    report = {
        "run_id": run_id, "run_date": str(today.date()),
        "wall_time_s": round(time.time() - t0, 1), "overrides": overrides,
        "lanes": {n: lanes[n] for n in LANE_NAMES if n in lanes},
        "store": {**{k: v for k, v in applied.items() if k != "new_job_ids"},
                  **store_stats, "raw_pruned": pruned},
        "new_job_ids": applied["new_job_ids"],
        "view": funnel,
    }
    (p.runs / f"{run_id}.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    (p.runs / f"{run_id}.md").write_text(render_md(report), encoding="utf-8")
    print(f"clean rows: {funnel.get('clean', 0)} | new job ids: {len(report['new_job_ids'])} "
          f"| report: {p.runs / (run_id + '.md')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
