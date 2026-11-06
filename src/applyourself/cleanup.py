"""`track cleanup`: delete the application folders of roles that are done.

- `skip`, and `withdrawn` that never reached `applied`: the folders go.
- `rejected`, `withdrawn` after `applied`, and `ghosted` (state_io.shown_state):
  one file, `applications/_archive/<lane>/<folder>.md`, keeps the built
  folder's resume (its `resume.md`, else the text of its `*_Resume.docx`) and,
  under `## Job description`, its `jd_snapshot.md`; then the folders go.

Each cleaned role gets a `cleaned: {date, archived}` side field, written by
track_cli through `state_io.mark_cleaned`; `state:` never changes (R10). A cleaned role's recorded
folder is then gone, so `material.folders.built_folder` gives None and the sheet shows
it `Cleaned`: nothing rebuilds it until a fresh /tailor.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from applyourself import state_io
from applyourself.apply import ledger
from applyourself.material import folders as app_folders
from applyourself.shared import clock, paths

ARCHIVE = "_archive"


@dataclass
class Action:
    job_id: str
    state: str
    folders: list[Path]
    archive_from: Path | None = None
    archive_jd: Path | None = None
    archive_to: Path | None = None
    bytes: int = field(default=0)


def _reached_applied(data: dict[str, Any], sent: set[str]) -> bool:
    history = data.get("state_history") or []
    return (
        bool(data.get("applied_at"))
        or any(isinstance(e, dict) and e.get("state") == "applied" for e in history)
        or data.get("job_id") in sent
    )


def _role_folders(job_id: str, data: dict[str, Any]) -> list[Path]:
    """Every folder of this role under a lane, never `_archive` or `apply_runs`."""
    apps = paths.APPLICATIONS
    found = set(app_folders.role_dirs(job_id))
    found |= {apps / d for d in data.get("tailored_dirs") or [] if (apps / d).is_dir()}
    return sorted(
        p
        for p in found
        if p.parent.parent == apps
        and not p.parent.name.startswith("_")
        and p.parent.name != "apply_runs"
        and job_id in p.name
    )


def _size(folder: Path) -> int:
    return sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())


def _resume_source(folder: Path) -> Path | None:
    md = folder / "resume.md"
    if md.is_file():
        return md
    return next(iter(sorted(folder.glob("*_Resume.docx"))), None)


def plan(pipeline_dir: Path | None = None) -> list[Action]:
    """What a cleanup would do, read-only."""
    sent = ledger.sent_job_ids(paths.APPLICATIONS / "apply_runs" / ledger.LEDGER_NAME)
    days = state_io.ghosted_after_days()
    actions = []
    for data in state_io.iter_states(pipeline_dir or paths.PIPELINE):
        job_id = str(data.get("job_id") or "")
        if not job_id:
            continue
        shown = state_io.shown_state(data, days=days)
        if shown == "skip" or shown == "withdrawn":
            archive = _reached_applied(data, sent)
        elif shown in ("rejected", state_io.GHOSTED):
            archive = True
        else:
            continue
        folders = _role_folders(job_id, data)
        if not folders:
            continue
        action = Action(job_id, shown, folders, bytes=sum(_size(f) for f in folders))
        if archive:
            built = app_folders.built_folder(data)
            source_dir = built if built is not None and built in folders else folders[-1]
            action.archive_from = _resume_source(source_dir)
            jd = source_dir / app_folders.SNAPSHOT
            action.archive_jd = jd if jd.is_file() else None
            if action.archive_from is not None or action.archive_jd is not None:
                lane = source_dir.parent.name
                action.archive_to = paths.APPLICATIONS / ARCHIVE / lane / f"{source_dir.name}.md"
        actions.append(action)
    return actions


def _docx_text(path: Path) -> str:
    from docx import Document

    return "\n\n".join(p.text for p in Document(str(path)).paragraphs if p.text.strip()) + "\n"


def _archive(action: Action) -> str | None:
    if action.archive_to is None:
        return None
    text = ""
    if action.archive_from is not None:
        text = (
            action.archive_from.read_text(encoding="utf-8")
            if action.archive_from.suffix == ".md"
            else _docx_text(action.archive_from)
        )
    if action.archive_jd is not None:
        jd = action.archive_jd.read_text(encoding="utf-8")
        text = f"{text.rstrip()}\n\n## Job description\n\n{jd}".lstrip()
    action.archive_to.parent.mkdir(parents=True, exist_ok=True)
    action.archive_to.write_text(text, encoding="utf-8")
    return str(action.archive_to.relative_to(paths.APPLICATIONS.parent))


def apply(actions: list[Action], record: Callable[[str, dict[str, Any]], None]) -> None:
    """Archive, `record(job_id, cleaned)` (track_cli writes it to state.yaml),
    then delete the folders."""
    today = clock.today().isoformat()
    for action in actions:
        record(action.job_id, {"date": today, "archived": _archive(action)})
        for folder in action.folders:
            shutil.rmtree(folder)


def render(actions: list[Action], applied: bool) -> str:
    folders = sum(len(a.folders) for a in actions)
    archived = sum(1 for a in actions if a.archive_to is not None)
    mb = sum(a.bytes for a in actions) / 1_000_000
    by_state: dict[str, int] = {}
    for a in actions:
        by_state[a.state] = by_state.get(a.state, 0) + 1
    states = ", ".join(f"{n} {s}" for s, n in sorted(by_state.items())) or "none"
    verb = "deleted" if applied else "would delete"
    kept = "archived" if applied else "to archive"
    return (
        f"{verb} {folders} folders ({mb:.1f} MB) for {len(actions)} roles ({states}); "
        f"{archived} resumes {kept}"
    )
