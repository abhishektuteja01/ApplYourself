"""`night` — the overnight runner: stage order, one lock, a per-night run.json.

  uv run night [--resume] [--only STAGE] [--dry-run]

Each stage is its script in scripts/stages/; this only orders, locks and records
(the stages call `claude`, never this). A failed discover or score stops the
chain; the stages after it are `deferred`. summary and email always run last.
Each stage's claude spend (the costs run_claude logs to <stage>.cost) goes into
run.json; once the night's total reaches settings.yaml `max_spend_usd_per_night`,
the claude stages still to run are `deferred` ("spend cap").
`--resume` reruns, in the last run folder, every stage that did not end ok.

  DRY_RUN=1          same as --dry-run: the stages print what they would run
  STAGE_LIMIT=N      passed to the stages: caps the roles tailor and apply_prep take
  SKIP_STAGES="a b"  leave those stages out; run.json marks them `skipped`
  STAGES_DIR=D       run the stage scripts from D (tests)

  night record STAGE RC STATUS DEFERRED DETAIL   a stage's own result (lib_pipeline.sh stage_end)
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import yaml

from src.shared import clock, config_schema, paths, schemas
from src.shared.files import atomic_write_text
from src.shared.logs import setup_logging

STAGES = ("discover", "score", "synonyms", "tailor", "apply_prep", "cleanup")
STOPS = ("discover", "score")
CLAUDE_STAGES = ("score", "synonyms", "tailor", "apply_prep")
SPEND_CAP = "not run: spend cap"
FINISH = ("summary", "email")
DONE = ("ok", "skipped")
LOCKED = 75


def write_json(path: Path, data: dict) -> None:
    atomic_write_text(path, json.dumps(data, indent=2))


def load(run_dir: Path) -> dict | None:
    path = run_dir / "run.json"
    if not path.is_file():
        return None
    run = json.loads(path.read_text(encoding="utf-8"))
    schemas.check(run.get("schema_version"), schemas.NIGHT_RUN_SCHEMA_VERSION, path)
    return run


def _stamp() -> str:
    return clock.now().strftime("%Y-%m-%d_%H%M%S")


def _iso() -> str:
    return clock.now().isoformat(timespec="seconds")


def _cost(path: Path) -> float:
    """The sum of a <stage>.cost file's `<tag> <usd>` lines; 0 when there is none."""
    total = 0.0
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                total += float(line.split()[-1])
            except (ValueError, IndexError):
                continue
    return round(total, 4)


def _spend_cap() -> float | None:
    """settings.yaml max_spend_usd_per_night; absent, 0 or an unreadable file is no cap."""
    try:
        cap = config_schema.setting("max_spend_usd_per_night", 0)
    except (OSError, ValueError, yaml.YAMLError) as e:
        print(f"night: no spend cap ({e})", file=sys.stderr)
        return None
    return float(cap) if cap and float(cap) > 0 else None


def record(argv: list[str]) -> int:
    """Write <run_dir>/<stage>.result.json; a <stage>_results.txt of `<id> <status>` lines becomes items."""
    ap = argparse.ArgumentParser(prog="night record")
    for name in ("stage", "rc", "status", "deferred", "detail"):
        ap.add_argument(name)
    ap.add_argument("--run-dir", type=Path, required=True)
    a = ap.parse_args(argv)
    items: dict[str, str] = {}
    lines = a.run_dir / f"{a.stage}_results.txt"
    if lines.is_file():
        for line in lines.read_text(encoding="utf-8").splitlines():
            if len(p := line.split()) == 2:
                items[p[0]] = p[1]
    result = {
        "rc": int(a.rc),
        "status": a.status,
        "deferred": int(a.deferred or 0),
        "detail": a.detail.strip(),
        "items": items,
    }
    write_json(a.run_dir / f"{a.stage}.result.json", result)
    return 0


class Night:
    def __init__(self, run_dir: Path, run: dict, dry: bool, stages_dir: Path) -> None:
        self.run_dir, self.run, self.dry, self.stages_dir = run_dir, run, dry, stages_dir
        self.log = run_dir / "pipeline.log"
        self.ran: list[str] = []
        self.cap = _spend_cap()

    def say(self, text: str) -> None:
        print(text, flush=True)
        with self.log.open("a", encoding="utf-8") as f:
            f.write(text + "\n")

    def save(self) -> None:
        write_json(self.run_dir / "run.json", self.run)

    def set(self, stage: str, **fields: object) -> None:
        self.run["stages"].setdefault(stage, {"stage": stage}).update(fields)
        self.save()

    def stage(self, name: str) -> dict:
        script = self.stages_dir / f"{name}.sh"
        if not os.access(script, os.X_OK):
            self.set(name, status="skipped", rc=None, detail="no stage script")
            return self.run["stages"][name]
        result = self.run_dir / f"{name}.result.json"
        result.unlink(missing_ok=True)
        quota = self.run_dir / f"{name}.quota"  # lib_pipeline.sh run_claude marks a usage limit
        quota.unlink(missing_ok=True)
        t0 = time.monotonic()
        self.set(name, status="running", rc=None, start=_iso(), end=None, detail="", counts={})
        # APPLY_UNATTENDED: apply run refuses --submit and bank writes (src/apply/bank.py)
        env = {**os.environ, "RUN_DIR": str(self.run_dir), "DRY_RUN": "1" if self.dry else "0"}
        env["APPLY_UNATTENDED"] = "1"
        with self.log.open("a", encoding="utf-8") as out:
            rc = subprocess.run(
                [str(script)], stdout=out, stderr=subprocess.STDOUT, env=env, cwd=paths.REPO_ROOT
            ).returncode
        said = json.loads(result.read_text(encoding="utf-8")) if result.is_file() else {}
        items = said.get("items") or {}
        counts = dict(Counter(items.values()))
        if said.get("deferred"):
            counts["deferred"] = said["deferred"]
        status = "fail" if rc else (said.get("status") if said.get("status") == "partial" else "ok")
        self.set(
            name,
            status=status,
            rc=rc,
            end=_iso(),
            seconds=int(time.monotonic() - t0),
            detail=said.get("detail", ""),
            counts=counts,
            cost_usd=_cost(self.run_dir / f"{name}.cost"),
            usage_limit=quota.is_file(),
            **({"items": items} if items else {}),
        )
        self.run["cost_usd"] = round(
            sum(st.get("cost_usd") or 0 for st in self.run["stages"].values()), 4
        )
        self.save()
        self.ran.append(name)
        return self.run["stages"][name]

    def chain(self, todo: list[str], finish: bool) -> str:
        """Run todo in stage order, then the finishers; the stage that stopped the chain, or ""."""
        stopped = ""
        for name in todo:
            if stopped:
                self.set(name, status="deferred", rc=None, detail=f"not run: {stopped} failed")
                continue
            if name in CLAUDE_STAGES and self.cap and self.run.get("cost_usd", 0) >= self.cap:
                self.set(name, status="deferred", rc=None, detail=SPEND_CAP)
                continue
            if self.stage(name)["status"] == "fail" and name in STOPS:
                stopped = name
                self.say(f"=== {name} failed: stopping the chain ===")
        for name in FINISH if finish else ():
            self.stage(name)  # best effort: a failed summary or email never changes the exit
        return stopped

    def history_line(self) -> str:
        line = self.run["stamp"] + (" resume" if self.run.get("resumes") else "")
        for name in self.ran:
            st = self.run["stages"][name]
            ok = {"ok": "ok", "partial": "partial"}.get(st["status"], f"FAIL(code={st['rc']})")
            line += f" | {name} {ok} {st.get('seconds', 0)}s"
        return line


def _prune(keep_dir: Path) -> None:
    try:
        from src.discovery.config import load_config

        keep = int(load_config().log_keep_days)
    except (OSError, ValueError, TypeError, ImportError, yaml.YAMLError):
        return  # a config that fails to load skips the prune
    cutoff = time.time() - (keep + 1) * 86400
    for d in paths.LOGS.glob("pipeline_*"):
        if d.is_dir() and d != keep_dir and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)


def _last_run(dry: bool) -> tuple[Path, dict] | None:
    """The newest run folder with a run.json of the same kind (dry or real)."""
    for d in sorted(paths.LOGS.glob("pipeline_*"), reverse=True):
        if re.fullmatch(r"pipeline_\d{4}-\d{2}-\d{2}_\d{6}", d.name):
            run = load(d)
            if run is not None and bool(run.get("dry_run")) == dry:
                return d, run
    return None


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["record"]:
        return record(argv[1:])
    ap = argparse.ArgumentParser(prog="night", description=__doc__.split("\n\n")[0])
    ap.add_argument("--resume", action="store_true", help="rerun the last run's stages not ok")
    ap.add_argument("--only", choices=STAGES + FINISH, help="run just this stage")
    ap.add_argument("--dry-run", action="store_true", help="the stages print only (DRY_RUN=1)")
    args = ap.parse_args(argv)
    dry = args.dry_run or os.environ.get("DRY_RUN") == "1"
    skip = set(os.environ.get("SKIP_STAGES", "").split())
    stages_dir = Path(os.environ.get("STAGES_DIR") or paths.REPO_ROOT / "scripts" / "stages")

    paths.LOGS.mkdir(parents=True, exist_ok=True)
    with (paths.LOGS / "night.lock").open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(
                f"night: another run holds {paths.LOGS / 'night.lock'}; not starting",
                file=sys.stderr,
            )
            return LOCKED
        return _night(args, dry, skip, stages_dir)


def _night(args: argparse.Namespace, dry: bool, skip: set[str], stages_dir: Path) -> int:
    if args.resume:
        last = _last_run(dry)
        if last is None:
            print(f"night: no run.json to resume under {paths.LOGS}", file=sys.stderr)
            return 1
        run_dir, run = last
        run.setdefault("resumes", []).append(_iso())
        if args.only:
            todo = [args.only]
        else:
            todo = [
                s
                for s in STAGES
                if s not in skip and run["stages"].get(s, {}).get("status") not in DONE
            ]
        if not todo:
            print(f"night: every stage in {run_dir.name} ended ok; nothing to resume")
            return 0
    else:
        stamp = _stamp()
        run_dir = paths.LOGS / f"pipeline_{stamp}"
        run_dir.mkdir(parents=True, exist_ok=True)
        todo = [args.only] if args.only else [s for s in STAGES if s not in skip]
        run = {
            "schema_version": schemas.NIGHT_RUN_SCHEMA_VERSION,
            "stamp": stamp,
            "started": _iso(),
            "dry_run": dry,
            "stages": {},
        }
        for s in STAGES:
            status = "pending" if s in todo else "skipped"
            run["stages"][s] = {"stage": s, "status": status, "rc": None}
    night = Night(run_dir, run, dry, stages_dir)
    night.save()
    night.say(f"=== pipeline start {run['stamp']} (run dir {run_dir} dry_run={int(dry)}) ===")
    night.say(f"=== stages: {' '.join(todo)} ===")

    caffeinate = None
    if not dry:
        _prune(run_dir)
        if shutil.which("caffeinate"):  # hold off sleep while this runs (macOS)
            caffeinate = subprocess.Popen(["caffeinate", "-s", "-i", "-w", str(os.getpid())])
    try:
        stopped = night.chain([s for s in todo if s in STAGES], finish=args.only is None)
        if args.only in FINISH:
            night.stage(args.only)
    finally:
        if caffeinate:
            caffeinate.terminate()
            caffeinate.wait()
    run["ended"] = _iso()
    night.save()

    line = night.history_line() + (f" | stopped at {stopped}" if stopped else "")
    if not dry:
        with (paths.LOGS / "pipeline_history.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    night.say(f"=== pipeline end {_stamp()} ===")
    print(line)
    only_failed = args.only is not None and run["stages"][args.only]["status"] == "fail"
    return 1 if stopped or only_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
