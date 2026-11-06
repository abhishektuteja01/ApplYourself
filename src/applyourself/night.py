"""`night` — the overnight runner: stage order, one lock, a per-night run.json.

  uv run night [--resume] [--only STAGE] [--dry-run]

Each stage is its script in scripts/stages/; this only orders, locks and records
(the stages call `claude`, never this). A failed discover or score stops the
chain; the stages after it are `deferred`. summary and email always run last.
Each stage's claude spend (the costs run_claude logs to <stage>.cost) goes into
run.json; once the night's total reaches settings.yaml `max_spend_usd_per_night`,
the claude stages still to run are `deferred` ("spend cap").
`--resume` reruns, in the last run folder, every stage that did not end ok.
A night started while another holds logs/night.lock exits 75, touching nothing
but a `not started` line in logs/pipeline_history.log.
A stage that hit the claude usage limit waits for the reset the limit message
names (or polls every settings.yaml `usage_limit_poll_minutes`, doubling) and
runs again, while the wait ends before `usage_limit_wait_until`; each wait is
in run.json `waits`. A dry run never waits.

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
from datetime import datetime, timedelta
from datetime import time as day_time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from applyourself.shared import clock, config_schema, paths, schemas
from applyourself.shared.files import atomic_write_text
from applyourself.shared.logs import setup_logging

STAGES = ("discover", "score", "synonyms", "tailor", "apply_prep", "cleanup")
STOPS = ("discover", "score")
CLAUDE_STAGES = ("score", "synonyms", "tailor", "apply_prep")
SPEND_CAP = "not run: spend cap"
FINISH = ("summary", "email")
DONE = ("ok", "skipped")
LOCKED = 75
WAIT_UNTIL = "19:00"  # usage_limit_wait_until when settings.yaml has none
POLL_MINUTES = 15  # usage_limit_poll_minutes when settings.yaml has none
POLL_MAX_DOUBLINGS = 3
RESET_MARGIN = timedelta(minutes=3)
RESET = re.compile(
    r"resets\s+(?:(?P<mon>[a-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)\b(?:\s*\((?P<tz>[\w/+-]+)\))?",
    re.I,
)
EPOCH = re.compile(r"\|(\d{10})\b")  # the older "usage limit reached|<epoch>" form
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


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


def reset_at(text: str, now: datetime) -> datetime | None:
    """When a usage-limit message says the limit resets, as naive local time
    after `now`; None when it names no time."""
    if m := EPOCH.search(text):
        return datetime.fromtimestamp(int(m[1]))  # noqa: DTZ006 - naive local, like clock.now
    m = RESET.search(text)
    if m is None:
        return None
    try:
        tz = ZoneInfo(m["tz"]) if m["tz"] else None
    except (ZoneInfoNotFoundError, ValueError):
        tz = None
    here = now.astimezone(tz) if tz else now
    hour = int(m["h"]) % 12 + (12 if m["ap"].lower() == "pm" else 0)
    try:
        at = here.replace(hour=hour, minute=int(m["m"] or 0), second=0, microsecond=0)
        if m["mon"]:
            at = at.replace(month=MONTHS.index(m["mon"].lower()) + 1, day=int(m["day"]))
            if at < here - timedelta(days=1):
                at = at.replace(year=at.year + 1)
        elif at <= here:
            at += timedelta(days=1)
    except ValueError:
        return None
    return at.astimezone().replace(tzinfo=None) if tz else at


def _wait_settings() -> tuple[str, int] | None:
    """(usage_limit_wait_until, usage_limit_poll_minutes); None when settings.yaml is unreadable."""
    try:
        until = config_schema.setting("usage_limit_wait_until", WAIT_UNTIL)
        poll = config_schema.setting("usage_limit_poll_minutes", POLL_MINUTES)
        day_time.fromisoformat(str(until))
        if int(poll) < 1:
            raise ValueError(f"usage_limit_poll_minutes {poll} is below 1")
        return str(until), int(poll)
    except (OSError, ValueError, TypeError, yaml.YAMLError) as e:
        print(f"night: no usage-limit waits ({e})", file=sys.stderr)
        return None


def _deadline(start: datetime, hhmm: str) -> datetime:
    """The first `hhmm` (local) after `start`."""
    t = day_time.fromisoformat(hhmm)
    at = start.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    return at if at > start else at + timedelta(days=1)


def _sleep_until(until: datetime) -> None:
    """Sleep to a wall-clock time, in short steps, so a Mac asleep meanwhile wakes on time."""
    while (left := (until - clock.now()).total_seconds()) > 0:
        time.sleep(min(left, 60))


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
        self.waited = 0
        self.cap = _spend_cap()
        wait = None if dry else _wait_settings()
        self.deadline = _deadline(clock.now(), wait[0]) if wait else None
        self.poll = timedelta(minutes=wait[1]) if wait else timedelta(0)

    def say(self, text: str) -> None:
        print(text, flush=True)
        with self.log.open("a", encoding="utf-8") as f:
            f.write(text + "\n")

    def save(self) -> None:
        write_json(self.run_dir / "run.json", self.run)

    def set(self, stage: str, **fields: object) -> None:
        self.run["stages"].setdefault(stage, {"stage": stage}).update(fields)
        self.save()

    def stage(self, name: str, again: bool = False) -> dict:
        """Run one stage script. `again` (a rerun after a usage-limit wait) keeps
        the items and seconds of the earlier run of this stage."""
        before = dict(self.run["stages"].get(name, {})) if again else {}
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
        # APPLY_UNATTENDED: apply run refuses --submit and bank writes (src/applyourself/apply/bank.py)
        env = {**os.environ, "RUN_DIR": str(self.run_dir), "DRY_RUN": "1" if self.dry else "0"}
        env["APPLY_UNATTENDED"] = "1"
        with self.log.open("a", encoding="utf-8") as out:
            rc = subprocess.run(
                [str(script)], stdout=out, stderr=subprocess.STDOUT, env=env, cwd=paths.REPO_ROOT
            ).returncode
        said = json.loads(result.read_text(encoding="utf-8")) if result.is_file() else {}
        items = {**(before.get("items") or {}), **(said.get("items") or {})}
        counts = dict(Counter(items.values()))
        if said.get("deferred"):
            counts["deferred"] = said["deferred"]
        status = "fail" if rc else (said.get("status") if said.get("status") == "partial" else "ok")
        self.set(
            name,
            status=status,
            rc=rc,
            end=_iso(),
            seconds=int(time.monotonic() - t0) + int(before.get("seconds") or 0),
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
        if name not in self.ran:
            self.ran.append(name)
        return self.run["stages"][name]

    def capped(self) -> bool:
        return bool(self.cap and self.run.get("cost_usd", 0) >= self.cap)

    def wait(self, name: str, polls: int) -> bool:
        """After `name` hit the usage limit: sleep until the limit resets and
        say True, or record why not and say False."""
        if self.deadline is None:
            return False
        quota = self.run_dir / f"{name}.quota"
        text = quota.read_text(encoding="utf-8") if quota.is_file() else ""
        now = clock.now()
        at = reset_at(text, now)
        if at is not None:
            until, how = max(at, now) + RESET_MARGIN, f"resets {at:%Y-%m-%d %H:%M}"
        else:
            until = now + self.poll * 2 ** min(polls, POLL_MAX_DOUBLINGS)
            how = "no reset time in the message: poll"
        entry: dict[str, object] = {
            "stage": name,
            "start": _iso(),
            "until": until.isoformat(timespec="seconds"),
            "how": how,
        }
        self.run.setdefault("waits", []).append(entry)
        if until > self.deadline:
            entry.update(
                waited=False, detail=f"past usage_limit_wait_until {self.deadline:%Y-%m-%d %H:%M}"
            )
            self.save()
            self.say(f"=== {name}: usage limit; {how}, {entry['detail']}: not waiting ===")
            return False
        entry["waited"] = True
        self.waited += 1
        self.save()
        self.say(f"=== {name}: usage limit; {how}; waiting until {until:%Y-%m-%d %H:%M} ===")
        _sleep_until(until)
        entry["end"] = _iso()
        self.save()
        return True

    def chain(self, todo: list[str], finish: bool) -> str:
        """Run todo in stage order, then the finishers; the stage that stopped the chain, or ""."""
        stopped = ""
        for name in todo:
            if stopped:
                self.set(name, status="deferred", rc=None, detail=f"not run: {stopped} failed")
                continue
            if name in CLAUDE_STAGES and self.capped():
                self.set(name, status="deferred", rc=None, detail=SPEND_CAP)
                continue
            st, polls = self.stage(name), 0
            while st.get("usage_limit") and st["status"] not in DONE and not self.capped():
                if not self.wait(name, polls):
                    break
                st, polls = self.stage(name, again=True), polls + 1
            if st["status"] == "fail" and name in STOPS:
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
        return line + (f" | waited {self.waited}x for the usage limit" if self.waited else "")


def _prune(keep_dir: Path) -> None:
    try:
        from applyourself.discovery.config import load_config

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
            # A night still running (or waiting out a usage limit): this one exits, untouched.
            lock.seek(0)
            holder = lock.read().strip() or "unknown"
            line = f"{_stamp()} not started: another night holds night.lock ({holder})"
            print(f"night: {line}", file=sys.stderr)
            if not dry:
                with (paths.LOGS / "pipeline_history.log").open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            return LOCKED
        lock.seek(0)
        lock.truncate()
        lock.write(f"pid {os.getpid()} since {_iso()}\n")
        lock.flush()
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
