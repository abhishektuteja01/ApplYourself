"""`uv run discover-digest`: tonight's run against its own recent history.

Reads `jobs/runs/*.json` (written by `run.py`) and `jobs/boards.parquet`.
Every number the `/discover` digest shows is computed here; the command
session only renders it (R7). Reads, never writes.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
from pathlib import Path

from src.shared import paths

JOBS_RUNS = paths.JOBS_RUNS
BOARD_STATE = paths.BOARD_STATE
DEFAULT_WINDOW = 7
# How far below its own median a lane has to fall before it is called out.
BELOW_MEDIAN_FRACTION = 0.5
_RUN_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}$")


def load_reports(runs_dir: Path | None = None) -> list[dict]:
    """Every readable run report, newest first."""
    runs_dir = Path(runs_dir or JOBS_RUNS)
    out = []
    for path in sorted(runs_dir.glob("*.json"), key=lambda f: f.stem, reverse=True):
        if not _RUN_ID_RE.match(path.stem):
            continue
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            out.append({"run_id": path.stem, "lanes": {}, "view": {}, "unreadable": True})
    return out


def _median(values):
    return statistics.median(values) if values else None


def build_digest(reports: list[dict], window: int = DEFAULT_WINDOW) -> dict | None:
    if not reports:
        return None
    latest, history = reports[0], [r for r in reports[1:window + 1] if not r.get("unreadable")]
    digest = {
        "run_id": latest["run_id"],
        "wall_time_s": latest.get("wall_time_s"),
        "final_rows": (latest.get("view") or {}).get("clean"),
        "new_job_ids": len(latest.get("new_job_ids") or []),
        "view": latest.get("view") or {},
        "history_runs": len(history),
        "trends": [], "crashed": [], "silent_zeros": [], "below_median": [],
        "truncated": [], "not_started": [],
        "notes": ["latest report unreadable"] if latest.get("unreadable") else [],
    }
    for name, lane in sorted((latest.get("lanes") or {}).items()):
        past = [h["lanes"][name] for h in history if name in (h.get("lanes") or {})]
        rows = lane.get("passed")
        median_rows = _median([x["passed"] for x in past if x.get("passed") is not None])
        errors = len(lane.get("errors") or [])
        median_errors = _median([float(len(x.get("errors") or [])) for x in past])
        ratio = rows / median_rows if rows is not None and median_rows else None
        streak = 0
        if lane.get("status") == "zero":
            streak = 1
            for x in past:
                if x.get("status") != "zero":
                    break
                streak += 1
        trend = {
            "name": name, "status": lane.get("status"), "rows": rows,
            "median_rows": median_rows, "ratio": ratio,
            "below_median": ratio is not None and ratio < BELOW_MEDIAN_FRACTION,
            "zero_streak": streak, "errors": errors, "median_errors": median_errors,
            "error_delta": errors - median_errors if median_errors is not None else None,
            # Zero rows, no error, and this lane normally finds rows: the quiet
            # block. Gated on the median, so an empty inbox never fires.
            "silent_zero": lane.get("status") == "zero" and not errors and bool(median_rows),
            "http_403": (lane.get("http") or {}).get("403", 0),
            "http_429": (lane.get("http") or {}).get("429", 0),
        }
        digest["trends"].append(trend)
        if lane.get("status") == "crashed":
            digest["crashed"].append(name)
        if trend["silent_zero"]:
            digest["silent_zeros"].append(name)
        if trend["below_median"]:
            digest["below_median"].append(name)
        if lane.get("truncated"):
            digest["truncated"].append(name)
        if lane.get("boards_due") and lane.get("boards_checked", 0) < lane["boards_due"]:
            digest["not_started"].append(f"{name}: {lane['boards_due'] - lane['boards_checked']} boards left due")
    digest["boards"] = board_summary()
    return digest


def board_summary(path: Path | None = None) -> dict:
    """Per ATS: boards known, dead, and never with a gate-passing job."""
    path = Path(path or BOARD_STATE)
    if not path.exists():
        return {}
    try:
        import pandas as pd
        state = pd.read_parquet(path)
    except (ImportError, OSError, ValueError):
        return {}
    out = {}
    for ats, g in state.groupby("ats"):
        out[ats] = {"boards": int(len(g)), "dead": int((g["status"] == "dead").sum()),
                    "never_found": int(g["last_found"].isna().sum())}
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="discover-digest")
    parser.add_argument("--runs-dir", type=Path, default=None)
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    parser.add_argument("--run-id", default=None, help="digest this run instead of the newest")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.window < 0:
        print("ERROR: --window must be >= 0.")
        return 1
    reports = load_reports(args.runs_dir)
    if args.run_id:
        idx = next((i for i, r in enumerate(reports) if r.get("run_id") == args.run_id), None)
        if idx is None:
            print(f"ERROR: no report for run id {args.run_id!r}.")
            return 1
        reports = reports[idx:]
    digest = build_digest(reports, args.window)
    if digest is None:
        print("ERROR: no run reports found.")
        return 1
    if args.json:
        print(json.dumps(digest, indent=2))
        return 0
    print(f"run {digest['run_id']}  wall {digest['wall_time_s'] or 0:.0f}s  "
          f"clean {digest['final_rows']}  new ids {digest['new_job_ids']}")
    for t in digest["trends"]:
        bits = [f"{t['name']}: {t['status']}", f"rows={t['rows']}"]
        if t["median_rows"] is not None:
            bits.append(f"median={t['median_rows']:g}")
        if t["errors"]:
            bits.append(f"errors={t['errors']}")
        if t["http_429"] or t["http_403"]:
            bits.append(f"403={t['http_403']} 429={t['http_429']}")
        print("  " + "  ".join(bits))
    for label in ("crashed", "silent_zeros", "below_median", "truncated", "not_started"):
        if digest[label]:
            print(f"{label}: {', '.join(digest[label])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
