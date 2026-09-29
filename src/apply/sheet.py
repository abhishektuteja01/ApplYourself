"""The overnight status sheet (`profile/automation.yaml` `table_path`), derived.

Every `status`, `note` and `uv_run_command` cell is rebuilt from the records
that own the facts — `pipeline/<job_id>/state.yaml`, the submit ledger, the
latest run report that names the role, and a role's waiting answers — so the
sheet can never disagree with them. Nothing else writes those columns.

`--add` appends roles newly scored at or above `score_threshold` that are not
tracked as done and not in the ledger. A row that is Applied or Closed is
removed on the rebuild (state.yaml and the ledger keep the record, and `--add`
never brings it back); an Expired row stays, so it can be marked skip.
"""
from __future__ import annotations

import re
from pathlib import Path

import openpyxl
import pandas as pd
import yaml

from src import paths, state_io
from src.app_cli import built_folder
from src.apply import detect, ledger

COLUMNS = ("score", "name", "company", "title", "source", "url", "status", "note",
           "uv_run_command")
_DONE = frozenset({"applied", "recruiter_contact", "screen", "interview", "offer"})
_CLOSED = frozenset({"skip", "withdrawn", "rejected", "ghosted"})
_ROLE_LINE = re.compile(r"^- `([0-9a-f]{8})`")
_HEADINGS = (("SUBMITTED", "submitted"), ("Submitted", "submitted"),
             ("Rejected", "rejected"), ("Clicked", "unsent"), ("Parked", "parked"),
             ("Ready", "ready"), ("Manual", "manual"), ("Skipped", "skipped"),
             ("Failed", "failed"), ("Expired", "expired"))


def latest_outcomes(runs_dir: Path) -> dict[str, tuple[str, str, str]]:
    """job_id -> (category, first detail line, report stem), from the newest
    run report naming each role."""
    out: dict[str, tuple[str, str, str]] = {}
    for report in sorted(runs_dir.glob("*.md")):
        category, job, detail = "", "", ""
        for line in report.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("## "):
                category = next((c for h, c in _HEADINGS if line[3:].startswith(h)), "")
                continue
            m = _ROLE_LINE.match(line)
            if m and category:
                job, detail = m.group(1), ""
                out[job] = (category, "", report.stem)
            elif job and line.startswith("  - ") and not detail and job in out:
                detail = line[4:].strip()
                out[job] = (out[job][0], detail, report.stem)
    return out


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
    if data.get("confirmed") is True:
        return ""
    drafted = len({k for k in data if k not in ("job_id", "confirmed")}) > 0
    stamp = path.stat().st_mtime
    if not drafted:
        return ""
    if since:
        report = paths.APPLICATIONS / "apply_runs" / f"{since}.md"
        if report.is_file() and report.stat().st_mtime >= stamp:
            return ""
    return str(path)


def status_for(job_id: str, submittable: bool, state: dict | None, sent: dict | None,
               outcome: tuple[str, str, str] | None) -> tuple[str, str, str]:
    """(status, note, uv_run_command) for one row."""
    state = state or {}
    current = state.get("state", "")
    if sent is not None and sent.get("status") == "rejected" or \
            (sent is not None and "rejected" in sent.get("detail", "")):
        return "Rejected by board", sent.get("detail", ""), ""
    if sent is not None and sent.get("status") in ("unsent", "clicking"):
        return ("Unsent — verify on board",
                f"clicked {sent.get('at', '')[:10]}; the board fired no submit request", "")
    if sent is not None or current in _DONE:
        when = (sent or {}).get("at", "") or state.get("applied_at", "")
        return "Applied", f"{current or 'sent'} {str(when)[:10]}".strip(), ""
    if current in _CLOSED:
        return f"Closed: {current}", "", ""
    if not submittable:
        return "Manual apply", "no submit path for this board", ""
    if not state.get("tailored_dirs"):
        return "New", "", ""
    waiting = _waiting(state, outcome[2] if outcome else "")
    if waiting:
        return "Needs your OK", f"answers drafted in {Path(waiting).parent.name}", ""
    if outcome:
        category, detail, _ = outcome
        if category == "ready":
            folder = built_folder(state)
            answers = folder / "answers_override.json"
            return ("Submittable", "",
                    f"uv run apply run --job-id {job_id} --answers {answers} "
                    f"--manifest {folder / 'form_manifest.submit.json'} --submit")
        if category == "expired":
            return "Expired", f"expired — {detail}" if detail else "expired", ""
        if category in ("parked", "failed", "unsent"):
            return category.capitalize(), detail, ""
    return "Resume ready", "", ""


def _submittable(job_id: str) -> bool:
    """Whether `apply run` would find a posting to fill, the same way it looks."""
    from src import apply_cli

    return apply_cli._auto_submittable(job_id)


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


def _candidates(threshold: float, present: set[str], sent: set[str],
                states: dict) -> list[dict]:
    if not paths.SCORED.exists() or not paths.CLEAN.exists():
        return []
    scored = pd.read_parquet(paths.SCORED, columns=["job_id", "fit_score"])
    clean = pd.read_parquet(paths.CLEAN, columns=["job_id", "company", "title", "source", "url"])
    rows = scored.merge(clean, on="job_id")
    rows = rows[rows["fit_score"] >= threshold]
    out = []
    for r in rows.sort_values("fit_score", ascending=False).itertuples():
        state = (states.get(r.job_id) or {}).get("state", "")
        if r.job_id in present or r.job_id in sent or state in _DONE | _CLOSED:
            continue
        out.append({"score": int(r.fit_score), "name": r.job_id, "company": r.company,
                    "title": r.title, "source": r.source, "url": r.url})
    return out


def rows(automation: Path | None = None) -> list[dict[str, str]]:
    """Every row of the sheet as it stands, as {column: text}. A pure read."""
    config = yaml.safe_load((automation or paths.PROFILE / "automation.yaml")
                            .read_text(encoding="utf-8")) or {}
    table = paths.REPO_ROOT / config.get("table_path", "top_scored_open.xlsx")
    if not table.is_file():
        return []
    _, ws, col = _load(table)
    out = []
    for r in range(2, ws.max_row + 1):
        row = {name: str(ws.cell(row=r, column=i).value or "") for name, i in col.items()}
        if row["name"]:
            out.append(row)
    return out


def job_ids(status: str, automation: Path | None = None) -> list[str]:
    """The job_ids the sheet currently shows at `status`, in sheet order. A pure read."""
    config = yaml.safe_load((automation or paths.PROFILE / "automation.yaml")
                            .read_text(encoding="utf-8")) or {}
    table = paths.REPO_ROOT / config.get("table_path", "top_scored_open.xlsx")
    if not table.is_file():
        return []
    _, ws, col = _load(table)
    return [str(ws.cell(row=r, column=col["name"]).value)
            for r in range(2, ws.max_row + 1)
            if ws.cell(row=r, column=col["status"]).value == status
            and ws.cell(row=r, column=col["name"]).value]


def _finished(status: str) -> bool:
    return status == "Applied" or status.startswith("Closed")


def refresh(add: bool = False, automation: Path | None = None) -> dict[str, int]:
    """Rebuild every row's status; with `add`, append new roles first. Rows that
    are Applied or Closed are removed. Returns a count per status, the removed
    ones under "<status> (removed)"."""
    config = yaml.safe_load((automation or paths.PROFILE / "automation.yaml")
                            .read_text(encoding="utf-8")) or {}
    table = paths.REPO_ROOT / config.get("table_path", "top_scored_open.xlsx")
    runs = paths.APPLICATIONS / "apply_runs"
    ledger_path = runs / ledger.LEDGER_NAME
    states = state_io.load_state_index(paths.PIPELINE)
    sent_ids = ledger.sent_job_ids(ledger_path)
    outcomes = latest_outcomes(runs)

    wb, ws, col = _load(table)
    present = {str(ws.cell(row=r, column=col["name"]).value)
               for r in range(2, ws.max_row + 1)}
    if add:
        for row in _candidates(float(config.get("score_threshold", 0)), present,
                               sent_ids, states):
            ws.append([row.get(name, "") for name in
                       sorted(col, key=col.get)])

    counts: dict[str, int] = {}
    finished: list[int] = []
    for r in range(2, ws.max_row + 1):
        job_id = str(ws.cell(row=r, column=col["name"]).value or "")
        if not job_id:
            continue
        url = str(ws.cell(row=r, column=col["url"]).value or "")
        submittable = detect.is_auto_submittable(url) or _submittable(job_id)
        status, note, command = status_for(
            job_id, submittable, states.get(job_id), ledger.prior(ledger_path, job_id),
            outcomes.get(job_id))
        ws.cell(row=r, column=col["status"], value=status)
        ws.cell(row=r, column=col["note"], value=note)
        ws.cell(row=r, column=col["uv_run_command"], value=command)
        if _finished(status):
            finished.append(r)
            status = f"{status} (removed)"
        counts[status] = counts.get(status, 0) + 1
    for r in reversed(finished):
        ws.delete_rows(r)
    wb.save(table)
    return counts


def check(automation: Path | None = None) -> dict[str, list[str]]:
    """Read-only look at the sheet as it stands: Applied or Closed rows still on
    it, a job_id listed twice, and rows whose posting is no longer in the live
    view (`clean.parquet`). Every list is empty on a clean sheet."""
    config = yaml.safe_load((automation or paths.PROFILE / "automation.yaml")
                            .read_text(encoding="utf-8")) or {}
    table = paths.REPO_ROOT / config.get("table_path", "top_scored_open.xlsx")
    out: dict[str, list[str]] = {"finished": [], "duplicates": [], "not_live": []}
    if not table.is_file():
        return out
    _, ws, col = _load(table)
    live = (set(pd.read_parquet(paths.CLEAN, columns=["job_id"])["job_id"])
            if paths.CLEAN.exists() else None)
    seen: set[str] = set()
    for r in range(2, ws.max_row + 1):
        job_id = str(ws.cell(row=r, column=col["name"]).value or "")
        if not job_id:
            continue
        if _finished(str(ws.cell(row=r, column=col["status"]).value or "")):
            out["finished"].append(job_id)
        if job_id in seen:
            out["duplicates"].append(job_id)
        seen.add(job_id)
        if live is not None and job_id not in live:
            out["not_live"].append(job_id)
    return out
