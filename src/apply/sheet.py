"""The work queue, and the overnight status sheet (`profile/automation.yaml`
`table_path`) that renders it.

`queue()` is computed, never read back from the sheet: every role scored at or
above scoring.yaml `cutoffs.sheet` (`score_store.view`) that is in the live view
(`clean.parquet`), whose state is not past tailored or closed, and whose status
is not Applied or Closed; highest score first, then job_id.
Each row's `status`, `note` and `uv_run_command` come from the records that own
the facts — `pipeline/<job_id>/state.yaml`, the submit ledger, the latest run
record that names the role, and a role's waiting answers. `apply sheet --list`,
triage and the night stages read `job_ids()`; `refresh()` rewrites the xlsx
from `queue()` atomically, so the file is a view and nothing reads it as input.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import openpyxl
import pandas as pd
import yaml

from src import state_io
from src.app_cli import built_folder
from src.apply import confirmation, cooldown, detect, ledger, run_record
from src.score_filter import load_cutoffs
from src.score_store import view
from src.shared import paths
from src.shared.config_schema import load_automation
from src.shared.parquet_io import locked

COLUMNS = ("score", "name", "company", "title", "source", "url", "status", "note", "uv_run_command")
_DONE = frozenset({"applied", "screen", "interview", "offer"})
_CLOSED = frozenset({"skip", "withdrawn", "rejected", state_io.GHOSTED})


def latest_outcomes(runs_dir: Path) -> dict[str, tuple[str, str, str]]:
    """job_id -> (category, first detail line, report stem), from the newest
    run record naming each role."""
    return run_record.outcomes_from(run_record.records(runs_dir))


def _waiting(state: dict, since: str) -> str:
    """The answers file, when /apply drafted answers after the role's latest
    run and is waiting for the user to confirm them."""
    folder = built_folder(state)
    if folder is None:
        return ""
    path = folder / "answers_override.json"
    if not path.is_file():
        return ""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return ""
    if confirmation.is_confirmed(path):
        return ""
    drafted = len({k for k in data if k not in ("job_id", "confirmed")}) > 0
    stamp = path.stat().st_mtime
    if not drafted:
        return ""
    if since:
        runs = paths.APPLICATIONS / "apply_runs"
        report = runs / f"{since}.json"
        report = report if report.is_file() else runs / f"{since}.md"
        if report.is_file() and report.stat().st_mtime >= stamp:
            return ""
    return str(path)


def status_for(
    job_id: str,
    submittable: bool,
    state: dict | None,
    sent: dict | None,
    outcome: tuple[str, str, str] | None,
    cool: date | None = None,
    days: int | None = None,
) -> tuple[str, str, str]:
    """(status, note, uv_run_command) for one row. `cool`: the day the
    company cooldown ends, which shows in place of Submittable. `days`:
    ghosted_after_days, read once by the caller."""
    state = state or {}
    current = state_io.shown_state(state, days=days) if state else ""
    verdict = ledger.verdict_of(sent)
    if verdict is ledger.Verdict.REJECTED:
        return "Rejected by board", f"rejected by the board: {sent.get('detail', '')}", ""
    if verdict is ledger.Verdict.UNKNOWN:
        why = (
            "the board fired no submit request"
            if sent.get("status") == "unsent"
            else "no outcome was recorded after the click"
        )
        return "Unsent", f"clicked {sent.get('at', '')[:10]}; {why}, verify on the board", ""
    if verdict is ledger.Verdict.SENT or current in _DONE:
        when = (sent or {}).get("at", "") or state.get("applied_at", "")
        return "Applied", f"{current or 'sent'} {str(when)[:10]}".strip(), ""
    if current in _CLOSED:
        return f"Closed: {current}", "", ""
    if state.get("cleaned") and built_folder(state) is None:
        when = state["cleaned"].get("date", "")
        return "Cleaned", f"folder cleaned {when}; /tailor {job_id} builds a new one", ""
    if not state.get("tailored_dirs"):
        return "New", "", ""
    if not submittable:
        return "Manual apply", "no submit path for this board", ""
    waiting = _waiting(state, outcome[2] if outcome else "")
    if waiting:
        return "Needs your OK", f"answers drafted in {Path(waiting).parent.name}", ""
    if outcome:
        category, detail, _ = outcome
        if category == "ready":
            folder = built_folder(state)
            answers = folder / "answers_override.json"
            return (
                "Cooldown" if cool else "Submittable",
                cooldown.note(cool) if cool else "",
                f"uv run apply run --job-id {job_id} --answers {answers} "
                f"--manifest {folder / 'form_manifest.submit.json'} --submit",
            )
        if category == "expired":
            return "Expired", f"expired — {detail}" if detail else "expired", ""
        if category in ("parked", "failed"):
            return "Parked", f"failed: {detail}" if category == "failed" else detail, ""
        if category == "unsent":
            return "Unsent", detail, ""
    return "Resume ready", "", ""


@dataclass(frozen=True)
class Snapshot:
    """Every record the queue reads, loaded once per call."""

    states: dict[str, dict]
    ledger_path: Path
    outcomes: dict[str, tuple[str, str, str]]
    latest: dict
    live: dict[str, dict]
    ghost_days: int
    cool_days: int


def snapshot(outcomes: dict | None = None) -> Snapshot:
    runs = paths.APPLICATIONS / "apply_runs"
    ledger_path = runs / ledger.LEDGER_NAME
    live: dict[str, dict] = {}
    if paths.CLEAN.exists():
        clean = pd.read_parquet(
            paths.CLEAN, columns=["job_id", "company", "title", "source", "url"]
        )
        live = {str(r["job_id"]): r for r in clean.drop_duplicates("job_id").to_dict("records")}
    return Snapshot(
        states=state_io.load_state_index(paths.PIPELINE),
        ledger_path=ledger_path,
        outcomes=latest_outcomes(runs) if outcomes is None else outcomes,
        latest=cooldown.last_applications(ledger_path=ledger_path),
        live=live,
        ghost_days=state_io.ghosted_after_days(),
        cool_days=cooldown.cooldown_days(),
    )


def _load(table: Path):
    if table.is_file():
        wb = openpyxl.load_workbook(table)
        ws = wb.active
        header = [c.value for c in ws[1]]
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(list(COLUMNS))
        header = list(COLUMNS)
    missing = [c for c in COLUMNS if c not in header]
    for name in missing:
        ws.cell(row=1, column=len(header) + 1, value=name)
        header.append(name)
    return wb, ws, {name: i + 1 for i, name in enumerate(header)}


def _table(automation: Path | None) -> Path:
    config = load_automation(automation or paths.PROFILE / "automation.yaml")
    return paths.root() / config.get("table_path", "top_scored_open.xlsx")


def _sheet_rows(table: Path) -> list[dict[str, str]]:
    """The xlsx rows as they stand, as {column: text}."""
    if not table.is_file():
        return []
    _, ws, col = _load(table)
    out = []
    for r in range(2, ws.max_row + 1):
        row = {name: str(ws.cell(row=r, column=i).value or "") for name, i in col.items()}
        if row["name"]:
            out.append(row)
    return out


def _finished(status: str) -> bool:
    return status == "Applied" or status.startswith("Closed")


def _form_url(state: dict | None) -> str:
    """The form URL the role's last plan read, so a ledger line written under
    another job_id for the same posting still counts as sent."""
    folder = built_folder(state or {})
    plan = folder / "plan.json" if folder else None
    if plan is None or not plan.is_file():
        return ""
    try:
        return str(json.loads(plan.read_text(encoding="utf-8")).get("form_url") or "")
    except ValueError:
        return ""


def _row_status(job_id: str, snap: Snapshot) -> tuple[str, str, str]:
    """A posting is submittable when its live-view or state.yaml URL is one
    `apply run` can fill, the same two places `resolve_url` looks."""
    state = snap.states.get(job_id)
    urls = ((snap.live.get(job_id) or {}).get("url"), (state or {}).get("url"))
    company = str((state or {}).get("company") or "")
    return status_for(
        job_id,
        any(detect.is_auto_submittable(str(u or "")) for u in urls),
        state,
        ledger.prior(snap.ledger_path, job_id, _form_url(state)),
        snap.outcomes.get(job_id),
        cooldown.until(company, snap.latest, days=snap.cool_days),
        days=snap.ghost_days,
    )


def queue(snap: Snapshot | None = None) -> list[dict[str, str]]:
    """The work queue, in order, as {column: text}. A pure read."""
    snap = snap or snapshot()
    if not paths.SCORED.exists():
        return []
    scored = view(paths.SCORED)[["job_id", "fit_score"]]
    scored = scored[scored["fit_score"] >= float(load_cutoffs().sheet)]
    out = []
    for r in scored.sort_values(["fit_score", "job_id"], ascending=[False, True]).itertuples():
        live = snap.live.get(r.job_id)
        stored = str((snap.states.get(r.job_id) or {}).get("state") or "")
        if live is None or stored in _DONE | _CLOSED:
            continue
        status, note, command = _row_status(r.job_id, snap)
        if _finished(status):
            continue
        row = {"score": int(r.fit_score), "name": r.job_id, **live}
        row.update(status=status, note=note, uv_run_command=command)
        out.append({n: "" if pd.isna(row.get(n)) else str(row.get(n) or "") for n in COLUMNS})
    return out


def rows(automation: Path | None = None) -> list[dict[str, str]]:
    """Every row of the work queue. A pure read; `automation` is kept for callers."""
    return queue()


def job_ids(status: str, automation: Path | None = None) -> list[str]:
    """The job_ids the queue holds at `status`, in queue order. A pure read."""
    return [row["name"] for row in queue() if row["status"] == status]


def refresh(add: bool = False, automation: Path | None = None) -> dict[str, int]:
    """Rewrite the sheet from `queue()` (tmp file, then rename). `add` is kept
    for callers: every refresh holds every queued role. Returns a count per
    status; a role the old sheet held that left the queue counts under
    "<status> (removed)"."""
    table = _table(automation)
    snap = snapshot()
    new = queue(snap)
    counts: dict[str, int] = {}
    for row in new:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    kept = {row["name"] for row in new}
    with locked(table):
        for old in _sheet_rows(table):
            if old["name"] not in kept:
                key = f"{_row_status(old['name'], snap)[0]} (removed)"
                counts[key] = counts.get(key, 0) + 1
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(list(COLUMNS))
        for row in new:
            ws.append([int(row["score"]) if row["score"] else "", *(row[c] for c in COLUMNS[1:])])
        tmp = table.with_name(f".{table.name}.tmp")
        wb.save(tmp)
        os.replace(tmp, table)
    return counts


def check(automation: Path | None = None) -> dict[str, list[str]]:
    """Read-only look at the sheet as it stands: Applied or Closed rows still on
    it, a job_id listed twice, and rows whose posting is no longer in the live
    view (`clean.parquet`), and rows whose status differs from what the records
    give now. Every list is empty on a clean sheet."""
    table = _table(automation)
    out: dict[str, list[str]] = {
        "finished": [],
        "duplicates": [],
        "not_live": [],
        "wrong_status": [],
    }
    if not table.is_file():
        return out
    snap = snapshot()
    seen: set[str] = set()
    for row in _sheet_rows(table):
        job_id = row["name"]
        if _finished(row["status"]):
            out["finished"].append(job_id)
        if job_id in seen:
            out["duplicates"].append(job_id)
        seen.add(job_id)
        if paths.CLEAN.exists() and job_id not in snap.live:
            out["not_live"].append(job_id)
        if _row_status(job_id, snap)[0] != row["status"]:
            out["wrong_status"].append(job_id)
    return out


def compare(automation: Path | None = None) -> dict[str, list[str]]:
    """Read-only: the sheet as it stands against `queue()`. `missing`: computed
    but not on the sheet; `extra`: on the sheet but not computed; `moved`: the
    shared roles, when their order differs."""
    on_sheet = [row["name"] for row in _sheet_rows(_table(automation))]
    computed = [row["name"] for row in queue()]
    shared = set(on_sheet) & set(computed)
    sheet_order = [j for j in on_sheet if j in shared]
    queue_order = [j for j in computed if j in shared]
    return {
        "sheet": on_sheet,
        "queue": computed,
        "missing": [j for j in computed if j not in set(on_sheet)],
        "extra": [j for j in on_sheet if j not in set(computed)],
        "moved": [] if sheet_order == queue_order else queue_order,
    }


def statuses(outcomes: dict, automation: Path | None = None) -> dict[str, tuple[str, str, str]]:
    """job_id -> (status, note, command) for every queued role, from the given
    run outcomes. A pure read; the run backfill compares two of these."""
    snap = snapshot(outcomes)
    return {row["name"]: _row_status(row["name"], snap) for row in queue(snap)}
