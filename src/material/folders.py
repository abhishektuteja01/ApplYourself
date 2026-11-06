"""Where a role's material lives (applications/<lane>/<dir>), its JD snapshot,
and `app new`, `app dir`, `app dupes`. Part of `uv run app` (src/app_cli.py)."""

from __future__ import annotations

import contextlib
import math
import os
import re
import sys
import tempfile
from pathlib import Path

import pandas as pd
import yaml

from src import resume_model, score_store, state_io, track_cli
from src.apply import ledger
from src.discovery.store import load_maybe_duplicates
from src.shared import clock, paths, verticals

RESUME_PLAN = "resume_plan.yaml"
LETTER_PLAN = "cover_letter.yaml"
ANSWERS = "company_answers.yaml"
OUTREACH = "outreach.yaml"
SNAPSHOT = "jd_snapshot.md"
ANSWER_KEYS = ("why_company", "why_role", "what_interests_you_about_product")


class AppError(Exception):
    """The command cannot run; the message says what to fix."""


def _clean_value(v):
    """Parquet values as plain YAML/JSON: arrays -> lists, NaN/NaT -> ""."""
    if hasattr(v, "tolist") and not isinstance(v, (str, bytes)):
        v = v.tolist()
    if isinstance(v, list):
        return [_clean_value(x) for x in v]
    if v is None or v is pd.NaT or (isinstance(v, float) and math.isnan(v)):
        return ""
    if hasattr(v, "isoformat"):
        return v.isoformat()[:10]
    return v


def _slug(text: str, limit: int = 60) -> str:
    s = re.sub(r"[^\w]+", "-", str(text).lower(), flags=re.UNICODE).strip("-_")
    if len(s) > limit:
        s = s[:limit].rsplit("-", 1)[0] or s[:limit]
    return s


def _file_slug(name: str) -> str:
    return re.sub(r"[^\w]+", "_", name, flags=re.UNICODE).strip("_")


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _role_dirs(job_id: str) -> list[Path]:
    """Every folder this role has had, in any lane, oldest version first."""
    found = [p for p in paths.APPLICATIONS.glob(f"*/*_{job_id}") if p.is_dir()]
    found += [p for p in paths.APPLICATIONS.glob(f"*/*_{job_id}_v*") if p.is_dir()]
    return sorted(set(found), key=lambda p: (_version(p), p.name))


def role_folder(job_id: str) -> Path | None:
    """The role's newest folder on disk (numeric `_vN` order), or None."""
    dirs = _role_dirs(job_id)
    return dirs[-1] if dirs else None


def _version(p: Path) -> int:
    m = re.search(r"_v(\d+)$", p.name)
    return int(m.group(1)) if m else 1


def _state(job_id: str) -> dict:
    return state_io.load_state(state_io.state_path_for(paths.PIPELINE, job_id)) or {}


def _read_snapshot(folder: Path) -> tuple[dict, str]:
    text = (folder / SNAPSHOT).read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}, text
    head, _, body = text[4:].partition("\n---\n")
    return yaml.safe_load(head) or {}, body


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        raise AppError(f"{path.name} missing in {path.parent.name} — write it first.")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AppError(f"{path.name}: top level must be a mapping")
    return data


def _identity() -> tuple[str, str]:
    name = str(resume_model.load_sources().resume.get("name") or "").strip()
    if not name:
        raise AppError("profile/resume.yaml has no `name`.")
    return name, _file_slug(name)


def built_folder(state: dict) -> Path | None:
    """The last folder a resume was built in (tailored_dirs[-1]), or None --
    also when `track cleanup` removed it, so nothing builds on a deleted folder,
    and when the entry points outside applications/."""
    dirs = state.get("tailored_dirs") or []
    if not dirs or Path(dirs[-1]).is_absolute() or ".." in Path(dirs[-1]).parts:
        return None
    folder = paths.APPLICATIONS / dirs[-1]
    return None if state.get("cleaned") and not folder.is_dir() else folder


def role_info(job_id: str) -> dict:
    """Where a role's material lives. `dir` is the last folder a resume was
    built in (tailored_dirs[-1]) — what letters, answers and /apply use.
    `work_dir` is the newest folder on disk — where the next resume builds."""
    state = _state(job_id)
    newest = role_folder(job_id)
    built_dir = built_folder(state)
    name, slug = _identity()

    def has(folder: Path | None, fname: str) -> bool:
        return bool(folder) and (folder / fname).is_file()

    return {
        "job_id": job_id,
        "state": state.get("state", ""),
        "vertical": (
            built_dir.relative_to(paths.APPLICATIONS).parts[0]
            if built_dir
            else newest.parent.name
            if newest
            else ""
        ),
        "dir": str(built_dir.relative_to(paths.APPLICATIONS.parent)) if built_dir else "",
        "work_dir": str(newest.relative_to(paths.APPLICATIONS.parent)) if newest else "",
        "name": name,
        "file_slug": slug,
        "has_resume": has(built_dir, f"{slug}_Resume.docx"),
        "has_letter": has(built_dir, f"{slug}_Cover_Letter.docx"),
        "has_answers": has(built_dir, ANSWERS),
    }


def _built_dir(job_id: str) -> Path:
    info = role_info(job_id)
    if not info["dir"]:
        raise AppError(f"{job_id} has no built resume yet — run /tailor {job_id} first.")
    folder = paths.APPLICATIONS.parent / info["dir"]
    if not folder.is_dir():
        raise AppError(f"{info['dir']} is in state.yaml but not on disk.")
    return folder


def _load_row(job_id: str) -> dict:
    if not paths.CLEAN.exists():
        raise AppError("jobs/clean.parquet missing — run discovery first.")
    if not paths.SCORED.exists():
        raise AppError("jobs/scored.parquet missing — run /score first.")
    clean = pd.read_parquet(paths.CLEAN)
    scored = score_store.view(paths.SCORED)
    c = clean[clean["job_id"] == job_id]
    s = scored[scored["job_id"] == job_id]
    if c.empty:
        raise AppError(f"job_id {job_id} not in clean.parquet")
    if s.empty:
        raise AppError(f"job_id {job_id} not in scored.parquet — run /score first")
    return {k: _clean_value(v) for k, v in {**c.iloc[0].to_dict(), **s.iloc[0].to_dict()}.items()}


def cmd_new(job_id: str, today: str | None = None) -> dict:
    today = today or clock.today().isoformat()
    if not (paths.PROFILE / "resume_template.docx").is_file():
        raise AppError("profile/resume_template.docx missing.")
    resume_model.load_sources()
    row = _load_row(job_id)
    cfg = verticals.get_config()
    lane = row.get("vertical") or ""
    if lane not in cfg.verticals or cfg.verticals[lane].resume is None:
        raise AppError(
            f"{job_id} is scored in lane {lane!r}, which has no resume: block in "
            f"profile/verticals.yaml. Re-score it or add the lane."
        )
    with contextlib.redirect_stdout(sys.stderr):
        registered = track_cli.main(["ensure", job_id])
    if registered != 0:
        raise AppError(f"could not register {job_id} in pipeline/.")

    newest = role_folder(job_id)
    _, slug = _identity()
    recorded = _state(job_id).get("tailored_dirs") or []
    if (
        newest
        and newest.parent.name == lane
        and (
            not (newest / f"{slug}_Resume.docx").exists()
            or str(newest.relative_to(paths.APPLICATIONS)) not in recorded
        )
    ):
        folder = newest  # an unfinished or unrecorded run: resume it
    else:
        base = f"{today}_{_slug(row.get('company', '')) or job_id}_{_slug(row.get('title', ''))}_{job_id}"
        folder = paths.APPLICATIONS / lane / (f"{base}_v{_version(newest) + 1}" if newest else base)
        folder.mkdir(parents=True)

    facts = {
        "job_id": job_id,
        "company": row.get("company", ""),
        "title": row.get("title", ""),
        "url": row.get("url", ""),
        "posted_date": row.get("posted_date", ""),
        "vertical": lane,
        "keywords_to_mirror": [str(k) for k in row.get("keywords_to_mirror") or []],
        "snapshot_at": today,
    }
    _atomic_write(
        folder / SNAPSHOT,
        "---\n"
        + yaml.safe_dump(facts, sort_keys=False, allow_unicode=True)
        + "---\n\n"
        + str(row.get("jd_text") or "")
        + "\n",
    )
    return {
        **role_info(job_id),
        "work_dir": str(folder.relative_to(paths.APPLICATIONS.parent)),
        "company": facts["company"],
        "title": facts["title"],
        "keywords_to_mirror": facts["keywords_to_mirror"],
    }


# A partner in these states (or untracked) is no risk: nothing was sent for it.
_IDLE_STATES = frozenset({"", "saved", "skip"})


def cmd_dupes(job_id: str) -> dict:
    """Other job_ids recorded in maybe_duplicates.jsonl with this one whose
    role is tailored or further, or was submitted. Read-only; a warning for
    the session to show, never a stop."""
    submitted = paths.APPLY_RUNS / ledger.LEDGER_NAME
    sent = ledger.sent_job_ids(submitted) if submitted.exists() else set()
    found: dict[str, dict] = {}
    for rec in load_maybe_duplicates(paths.MAYBE_DUPES):
        ids = str(rec["pair"]).split("|")
        if job_id not in ids or len(ids) != 2:
            continue
        other = ids[1] if ids[0] == job_id else ids[0]
        state = _state(other)
        status = str(state.get("state") or "")
        if status in _IDLE_STATES and other not in sent:
            continue
        mine = rec.get("job_id") == other
        hit = {
            "job_id": other,
            "company": state.get("company") or rec.get("company" if mine else "other_company", ""),
            "title": state.get("title") or rec.get("title", ""),
            "state": status,
            "submitted": other in sent,
            "reason": rec.get("reason", ""),
            "jd_score": rec.get("jd_score"),
        }
        if other not in found or (hit["jd_score"] or 0) > (found[other]["jd_score"] or 0):
            found[other] = hit
    return {"job_id": job_id, "duplicates": sorted(found.values(), key=lambda h: h["job_id"])}
