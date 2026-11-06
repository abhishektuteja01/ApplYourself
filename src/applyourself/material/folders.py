"""Where a role's material lives (applications/<lane>/<dir>), its JD snapshot,
and `app new`, `app dir`, `app dupes`. Part of `uv run app` (src/applyourself/app_cli.py)."""

from __future__ import annotations

import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from applyourself import resume_model, state_io
from applyourself.shared import paths

RESUME_PLAN = "resume_plan.yaml"
LETTER_PLAN = "cover_letter.yaml"
ANSWERS = "company_answers.yaml"
OUTREACH = "outreach.yaml"
SNAPSHOT = "jd_snapshot.md"
ANSWER_KEYS = ("why_company", "why_role", "what_interests_you_about_product")


class AppError(Exception):
    """The command cannot run; the message says what to fix."""


def _clean_value(v: Any) -> Any:
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


def role_dirs(job_id: str) -> list[Path]:
    """Every folder this role has had, in any lane, oldest version first."""
    found = [p for p in paths.APPLICATIONS.glob(f"*/*_{job_id}") if p.is_dir()]
    found += [p for p in paths.APPLICATIONS.glob(f"*/*_{job_id}_v*") if p.is_dir()]
    return sorted(set(found), key=lambda p: (_version(p), p.name))


def role_folder(job_id: str) -> Path | None:
    """The role's newest folder on disk (numeric `_vN` order), or None."""
    dirs = role_dirs(job_id)
    return dirs[-1] if dirs else None


def _version(p: Path) -> int:
    m = re.search(r"_v(\d+)$", p.name)
    return int(m.group(1)) if m else 1


def _state(job_id: str) -> dict[str, Any]:
    return state_io.load_state(state_io.state_path_for(paths.PIPELINE, job_id)) or {}


def _read_snapshot(folder: Path) -> tuple[dict[str, Any], str]:
    text = (folder / SNAPSHOT).read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}, text
    head, _, body = text[4:].partition("\n---\n")
    return yaml.safe_load(head) or {}, body


def _load_yaml(path: Path) -> dict[str, Any]:
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


def built_folder(state: dict[str, Any]) -> Path | None:
    """The last folder a resume was built in (tailored_dirs[-1]), or None --
    also when `track cleanup` removed it, so nothing builds on a deleted folder,
    and when the entry points outside applications/."""
    dirs = state.get("tailored_dirs") or []
    if not dirs or Path(dirs[-1]).is_absolute() or ".." in Path(dirs[-1]).parts:
        return None
    folder = paths.APPLICATIONS / dirs[-1]
    return None if state.get("cleaned") and not folder.is_dir() else folder


def role_info(job_id: str) -> dict[str, Any]:
    """Where a role's material lives. `dir` is the last folder a resume was
    built in (tailored_dirs[-1]) — what letters, answers and /apply use.
    `work_dir` is the newest folder on disk — where the next resume builds."""
    state = _state(job_id)
    newest = role_folder(job_id)
    built_dir = built_folder(state)
    name, slug = _identity()

    def has(folder: Path | None, fname: str) -> bool:
        return folder is not None and (folder / fname).is_file()

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


def built_dir(job_id: str) -> Path:
    info = role_info(job_id)
    if not info["dir"]:
        raise AppError(f"{job_id} has no built resume yet — run /tailor {job_id} first.")
    folder: Path = paths.APPLICATIONS.parent / info["dir"]
    if not folder.is_dir():
        raise AppError(f"{info['dir']} is in state.yaml but not on disk.")
    return folder
