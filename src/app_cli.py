"""`uv run app` — the deterministic half of /tailor, /cover-letter and
/company-answers (R7: no LLM calls; R10: never writes `state:`).

The role folder is the only state these commands share:

  applications/<lane>/<date>_<company>_<title>_<job_id>[_vN]/
    jd_snapshot.md        written by `new`: the JD plus its facts (incl. keywords)
    resume_plan.yaml      the session's choices for the resume
    trace.md, *_Resume.*  written by `build resume`
    company_answers.yaml  the session's research answers, checked by `build answers`
    cover_letter.yaml     the session's letter, rendered by `build letter`

  new <job_id>                  checks, registers the role, opens its folder
  dir <job_id>                  where things are, as JSON
  build resume <job_id> [--final]
  build letter <job_id>
  build answers <job_id>

Every command prints one JSON object. Exit 0 = ok, 2 = problems for the
session to fix (listed in "problems"), 1 = cannot run.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
import yaml

from src import lint, paths, resume_model, state_io, track_cli, verticals
from src.docx_cover_letter import render_cover_letter
from src.docx_render import ResumeContentError, TemplateError, export_pdf, render_resume

CLEAN = paths.CLEAN
SCORED = paths.SCORED
PIPELINE = paths.PIPELINE
PROFILE = paths.PROFILE
APPLICATIONS = paths.APPLICATIONS

RESUME_PLAN = "resume_plan.yaml"
LETTER_PLAN = "cover_letter.yaml"
ANSWERS = "company_answers.yaml"
SNAPSHOT = "jd_snapshot.md"
ANSWER_KEYS = ("why_company", "why_role", "what_interests_you_about_product")


class AppError(Exception):
    """The command cannot run; the message says what to fix."""


# =====================================================================
# Small helpers
# =====================================================================

def _emit(obj: dict, code: int = 0) -> int:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
    return code


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
    found = [p for p in APPLICATIONS.glob(f"*/*_{job_id}") if p.is_dir()]
    found += [p for p in APPLICATIONS.glob(f"*/*_{job_id}_v*") if p.is_dir()]
    return sorted(set(found), key=_version)


def _version(p: Path) -> int:
    m = re.search(r"_v(\d+)$", p.name)
    return int(m.group(1)) if m else 1


def _state(job_id: str) -> dict:
    return state_io.load_state(state_io.state_path_for(PIPELINE, job_id)) or {}


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


# =====================================================================
# dir
# =====================================================================

def role_info(job_id: str) -> dict:
    """Where a role's material lives. `dir` is the last folder a resume was
    built in (tailored_dirs[-1]) — what letters, answers and /apply use.
    `work_dir` is the newest folder on disk — where the next resume builds."""
    state = _state(job_id)
    dirs = _role_dirs(job_id)
    built = (state.get("tailored_dirs") or [None])[-1]
    built_dir = APPLICATIONS / built if built else None
    name, slug = _identity()

    def has(folder: Path | None, fname: str) -> bool:
        return bool(folder) and (folder / fname).is_file()

    return {
        "job_id": job_id,
        "state": state.get("state", ""),
        "vertical": built.split("/", 1)[0] if built else (dirs[-1].parent.name if dirs else ""),
        "dir": str(built_dir.relative_to(APPLICATIONS.parent)) if built_dir else "",
        "work_dir": str(dirs[-1].relative_to(APPLICATIONS.parent)) if dirs else "",
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
    folder = APPLICATIONS.parent / info["dir"]
    if not folder.is_dir():
        raise AppError(f"{info['dir']} is in state.yaml but not on disk.")
    return folder


# =====================================================================
# new
# =====================================================================

def _load_row(job_id: str) -> dict:
    if not CLEAN.exists():
        raise AppError("jobs/clean.parquet missing — run discovery first.")
    if not SCORED.exists():
        raise AppError("jobs/scored.parquet missing — run /score first.")
    clean = pd.read_parquet(CLEAN)
    scored = pd.read_parquet(SCORED)
    c = clean[clean["job_id"] == job_id]
    s = scored[scored["job_id"] == job_id]
    if c.empty:
        raise AppError(f"job_id {job_id} not in clean.parquet")
    if s.empty:
        raise AppError(f"job_id {job_id} not in scored.parquet — run /score first")
    return {k: _clean_value(v) for k, v in {**c.iloc[0].to_dict(), **s.iloc[0].to_dict()}.items()}


def cmd_new(job_id: str, today: str | None = None) -> dict:
    today = today or date.today().isoformat()
    for fname in ("resume_template.docx",):
        if not (PROFILE / fname).is_file():
            raise AppError(f"profile/{fname} missing.")
    resume_model.load_sources()
    row = _load_row(job_id)
    cfg = verticals.get_config()
    lane = row.get("vertical") or ""
    if lane not in cfg.verticals or cfg.verticals[lane].resume is None:
        raise AppError(f"{job_id} is scored in lane {lane!r}, which has no resume: block in "
                       f"profile/verticals.yaml. Re-score it or add the lane.")
    with contextlib.redirect_stdout(sys.stderr):
        registered = track_cli.main(["ensure", job_id])
    if registered != 0:
        raise AppError(f"could not register {job_id} in pipeline/.")

    dirs = _role_dirs(job_id)
    newest = dirs[-1] if dirs else None
    _, slug = _identity()
    if newest and newest.parent.name == lane and not (newest / f"{slug}_Resume.docx").exists():
        folder = newest                                     # an unfinished run: resume it
    else:
        base = f"{today}_{_slug(row.get('company', '')) or job_id}_{_slug(row.get('title', ''))}_{job_id}"
        folder = APPLICATIONS / lane / (f"{base}_v{_version(newest) + 1}" if newest else base)
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
    _atomic_write(folder / SNAPSHOT, "---\n" + yaml.safe_dump(facts, sort_keys=False, allow_unicode=True)
                  + "---\n\n" + str(row.get("jd_text") or "") + "\n")
    return {**role_info(job_id), "work_dir": str(folder.relative_to(APPLICATIONS.parent)),
            "company": facts["company"], "title": facts["title"],
            "keywords_to_mirror": facts["keywords_to_mirror"]}


# =====================================================================
# build resume
# =====================================================================

def cmd_build_resume(job_id: str, final: bool = False) -> tuple[dict, int]:
    info = role_info(job_id)
    if not info["work_dir"]:
        raise AppError(f"{job_id} has no folder — run `uv run app new {job_id}` first.")
    folder = APPLICATIONS.parent / info["work_dir"]
    lane = folder.parent.name
    facts, _ = _read_snapshot(folder)
    plan = _load_yaml(folder / RESUME_PLAN)
    src = resume_model.load_sources()
    built = resume_model.build(src, lane, plan, company=str(facts.get("company", "")),
                               keywords=tuple(facts.get("keywords_to_mirror") or ()), final=final)
    if built.problems:
        return {"ok": False, "problems": [p.as_dict() for p in built.problems]}, 2

    docx = folder / f"{info['file_slug']}_Resume.docx"
    try:
        render_resume(built.markdown, PROFILE / "resume_template.docx", docx)
    except (TemplateError, ResumeContentError, FileNotFoundError) as exc:
        raise AppError(str(exc)) from exc
    pdf = export_pdf(docx, folder.name)
    if pdf["status"] == "ok" and pdf["pages"] > 1:
        return {"ok": False, "problems": [{"where": "resume", "code": "pages",
                "detail": f"renders to {pdf['pages']} pages; drop or shorten bullets"}]}, 2

    header = [f"# trace — {folder.name}", "",
              f"job_id `{job_id}`, lane `{lane}`. Written by `uv run app build resume`.", ""]
    _atomic_write(folder / "trace.md", "\n".join(header + built.trace) + "\n")
    rel = str(folder.relative_to(APPLICATIONS))
    state_io.append_tailored_dir(state_io.state_path_for(PIPELINE, job_id), rel)
    return {"ok": True, "dir": str(folder.relative_to(APPLICATIONS.parent)), "docx": docx.name,
            "pdf": pdf, "swapped": built.swapped}, 0


# =====================================================================
# Prose checks (letter, answers)
# =====================================================================

def _prose_problems(src, where: str, text: str, uses: list[str], grounds: str,
                    keep: list[str]) -> list[dict]:
    """Numbers must come from a cited bullet or the JD; lint with keep-terms."""
    out = [{"where": where, "code": "unknown_id", "detail": f"{u} is not in bullets.md"}
           for u in uses if u not in src.bullets]
    allowed = " ".join([grounds, resume_model.context_text(src)]
                       + [resume_model.bullet_text(src, u) for u in uses])
    out += [{"where": where, "code": "number", "detail": f"{n} is in no cited bullet and not in the JD"}
            for n in resume_model.ungrounded_numbers(text, allowed)]
    out += [{"where": where, "code": "lint", "detail": f"banned phrase {v['phrase']!r} ({v['category']})"}
            for v in lint.find_phrase_violations(text, context="resume", rules=src.rules, keep=keep)]
    return out


def _sentences(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s])


def cmd_build_answers(job_id: str) -> tuple[dict, int]:
    folder = _built_dir(job_id)
    facts, jd = _read_snapshot(folder)
    data = _load_yaml(folder / ANSWERS)
    src = resume_model.load_sources()
    problems: list[dict] = []
    expected = {"researched_at", "sources", "uses", *ANSWER_KEYS}
    for k in sorted(set(data) ^ expected):
        problems.append({"where": k, "code": "layout",
                         "detail": "unknown key" if k in data else "missing key"})
    uses = [str(u) for u in data.get("uses") or []]
    keep = resume_model.keep_terms(src, str(facts.get("company", "")))
    for k in ANSWER_KEYS:
        v = data.get(k)
        if v is None:
            continue
        if not isinstance(v, str) or not v.strip():
            problems.append({"where": k, "code": "layout", "detail": "must be text or null"})
            continue
        v = lint.fix_mechanical(v.strip(), src.rules)[0]
        data[k] = v
        if not 2 <= _sentences(v) <= 4:
            problems.append({"where": k, "code": "layout", "detail": f"{_sentences(v)} sentences; write 2-4"})
        problems += _prose_problems(src, k, v, uses, jd, keep)
    if problems:
        return {"ok": False, "problems": problems}, 2
    _atomic_write(folder / ANSWERS, yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=1000))
    return {"ok": True, "dir": str(folder.relative_to(APPLICATIONS.parent)),
            "answered": [k for k in ANSWER_KEYS if data.get(k)],
            "no_research": [k for k in ANSWER_KEYS if not data.get(k)]}, 0


def _names_company(company: str, text: str) -> bool:
    """The full name, or its first word ("Bain" for "Bain & Company")."""
    low = text.lower()
    first = re.split(r"[\s,&]+", company.strip())[0]
    return company.lower() in low or (len(first) >= 3 and re.search(
        rf"(?<!\w){re.escape(first.lower())}(?!\w)", low) is not None)


def cmd_build_letter(job_id: str, today: date | None = None) -> tuple[dict, int]:
    folder = _built_dir(job_id)
    facts, jd = _read_snapshot(folder)
    data = _load_yaml(folder / LETTER_PLAN)
    src = resume_model.load_sources()
    name, slug = _identity()
    company = str(facts.get("company", ""))
    problems: list[dict] = []

    body = data.get("body")
    if not isinstance(body, list) or not body or not all(isinstance(p, str) and p.strip() for p in body):
        return {"ok": False, "problems": [{"where": "body", "code": "layout",
                "detail": "must be a list of paragraph strings"}]}, 2
    salutation = str(data.get("salutation") or "").strip()
    if not salutation:
        problems.append({"where": "salutation", "code": "layout", "detail": "missing"})
    recipient = re.sub(r"^(dear|hi|hello)\s+|,$", "", salutation, flags=re.I).strip()
    keep = resume_model.keep_terms(src, company, recipient)
    uses = [str(u) for u in data.get("uses") or []]
    if not uses:
        problems.append({"where": "uses", "code": "layout", "detail": "cite the bullets the letter draws on"})
    fix = lambda t: lint.fix_mechanical(t.strip(), src.rules)[0]
    salutation = fix(salutation)
    body = [fix(p) for p in body]
    problems += _prose_problems(src, "salutation", salutation, [], "", keep)
    for i, p in enumerate(body):
        problems += _prose_problems(src, f"body[{i}]", p, uses, jd, keep)
    if company and not _names_company(company, " ".join(body)):
        problems.append({"where": "body", "code": "layout", "detail": f"never names {company!r}"})
    if problems:
        return {"ok": False, "problems": problems}, 2

    today = today or date.today()
    content = {"salutation": salutation, "body": body,
               "date": f"{today:%B} {today.day}, {today.year}",
               "closing": str(data.get("closing") or ""), "signoff_name": name}
    docx = folder / f"{slug}_Cover_Letter.docx"
    try:
        render_cover_letter(content, PROFILE / "cover_letter_template.docx", docx)
    except (TemplateError, FileNotFoundError, ValueError) as exc:
        raise AppError(str(exc)) from exc
    pdf = export_pdf(docx, folder.name)
    words = sum(len(p.split()) for p in body)
    if pdf["status"] == "ok" and pdf["pages"] > 1:
        return {"ok": False, "words": words, "problems": [{"where": "body", "code": "pages",
                "detail": f"renders to {pdf['pages']} pages ({words} words); cut the body"}]}, 2
    return {"ok": True, "dir": str(folder.relative_to(APPLICATIONS.parent)), "docx": docx.name,
            "pdf": pdf, "words": words}, 0


# =====================================================================
# CLI
# =====================================================================

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uv run app", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_new = sub.add_parser("new")
    p_new.add_argument("job_id")
    p_new.add_argument("--today", default=None)
    p_dir = sub.add_parser("dir")
    p_dir.add_argument("job_id")
    p_build = sub.add_parser("build")
    p_build.add_argument("what", choices=["resume", "letter", "answers"])
    p_build.add_argument("job_id")
    p_build.add_argument("--final", action="store_true",
                         help="resume only: fall back to canonical text where a rewording fails")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.cmd == "new":
            return _emit(cmd_new(args.job_id, args.today))
        if args.cmd == "dir":
            return _emit(role_info(args.job_id))
        if args.what == "resume":
            out, code = cmd_build_resume(args.job_id, final=args.final)
        elif args.what == "letter":
            out, code = cmd_build_letter(args.job_id)
        else:
            out, code = cmd_build_answers(args.job_id)
        return _emit(out, code)
    except (AppError, resume_model.SourceError, lint.LintConfigError) as exc:
        return _emit({"ok": False, "error": str(exc)}, 1)


if __name__ == "__main__":
    sys.exit(main())
