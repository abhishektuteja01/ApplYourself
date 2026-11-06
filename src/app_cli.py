"""`uv run app` — the deterministic half of /tailor, /cover-letter, /outreach and
/company-answers (R7: no LLM calls; R10: never writes `state:`).

The role folder is the only state these commands share:

  applications/<lane>/<date>_<company>_<title>_<job_id>[_vN]/
    jd_snapshot.md        written by `new`: the JD plus its facts (incl. keywords)
    resume_plan.yaml      the session's choices for the resume
    trace.md, *_Resume.*  written by `build resume`
    company_answers.yaml  the session's research answers, checked by `build answers`
    cover_letter.yaml     the session's letter, rendered by `build letter`
    outreach.yaml         the session's outreach message, written by `build outreach`

  new <job_id>                  checks, registers the role, opens its folder
  dir <job_id>                  where things are, as JSON
  build resume <job_id> [--final]
  build letter <job_id>
  build answers <job_id>
  build outreach <job_id>       check outreach.yaml, write the draft under
                                pipeline/<job_id>/outreach/ and record it in outreach[]
  dupes <job_id>                other ids that may be this role, already in play
  schema resume|letter|answers|outreach
                                the keys that session file may hold (JSON Schema)
  lint outreach <draft> --company C [--recipient R]
                                fix the draft's mechanical slips in place, list
                                the phrase violations left

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
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import pandas as pd
import yaml

from src import lint, resume_model, score_store, session_schema, state_io, track_cli
from src.apply import ledger
from src.discovery.store import load_maybe_duplicates
from src.docx_cover_letter import render_cover_letter
from src.docx_render import PdfResult, TemplateError, export_pdf, render_resume
from src.session_schema import ProblemCode
from src.shared import clock, config_schema, paths, verticals

RESUME_PLAN = "resume_plan.yaml"
LETTER_PLAN = "cover_letter.yaml"
ANSWERS = "company_answers.yaml"
OUTREACH = "outreach.yaml"
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
    found = [p for p in paths.APPLICATIONS.glob(f"*/*_{job_id}") if p.is_dir()]
    found += [p for p in paths.APPLICATIONS.glob(f"*/*_{job_id}_v*") if p.is_dir()]
    return sorted(set(found), key=_version)


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


# =====================================================================
# dir
# =====================================================================


def built_folder(state: dict) -> Path | None:
    """The last folder a resume was built in (tailored_dirs[-1]), or None --
    also when `track cleanup` removed it, so nothing builds on a deleted folder."""
    dirs = state.get("tailored_dirs") or []
    if not dirs:
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


# =====================================================================
# new
# =====================================================================


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


# =====================================================================
# build resume
# =====================================================================


def _pdf_unknown(where: str, pdf: dict) -> dict:
    return {
        "where": where,
        "code": ProblemCode.PAGES,
        "detail": f"page count unknown: pdf {pdf['status']} "
        f"({pdf['detail']}); not a plan problem: rebuild once the pdf works, or pass --no-pdf",
    }


def _pdf(docx: Path, token: str, no_pdf: bool) -> PdfResult:
    """The docx's pdf; with --no-pdf none is made (Word never starts), and an
    older one is removed so it cannot pass for this docx."""
    if no_pdf:
        docx.with_suffix(".pdf").unlink(missing_ok=True)
        return PdfResult(status="skipped", path="", pages=0, detail="--no-pdf")
    return export_pdf(docx, token)


def cmd_build_resume(job_id: str, final: bool = False, no_pdf: bool = False) -> tuple[dict, int]:
    info = role_info(job_id)
    if not info["work_dir"]:
        raise AppError(f"{job_id} has no folder — run `uv run app new {job_id}` first.")
    folder = paths.APPLICATIONS.parent / info["work_dir"]
    lane = folder.parent.name
    facts, _ = _read_snapshot(folder)
    plan = _load_yaml(folder / RESUME_PLAN)
    if bad := session_schema.problems("resume", plan):
        return {"ok": False, "problems": bad}, 2
    src = resume_model.load_sources()
    built = resume_model.build(
        src,
        lane,
        plan,
        company=str(facts.get("company", "")),
        keywords=tuple(facts.get("keywords_to_mirror") or ()),
        final=final,
    )
    if built.problems:
        return {"ok": False, "problems": [p.as_dict() for p in built.problems]}, 2

    docx = folder / f"{info['file_slug']}_Resume.docx"
    try:
        render_resume(built.blocks, paths.PROFILE / "resume_template.docx", docx)
    except (TemplateError, FileNotFoundError) as exc:
        raise AppError(str(exc)) from exc
    pdf = _pdf(docx, folder.name, no_pdf)
    if pdf["status"] == "ok" and pdf["pages"] > 1:
        return {
            "ok": False,
            "problems": [
                {
                    "where": "resume",
                    "code": ProblemCode.PAGES,
                    "detail": f"renders to {pdf['pages']} pages; drop or shorten bullets",
                }
            ],
        }, 2
    if pdf["status"] != "ok" and not no_pdf:
        return {"ok": False, "problems": [_pdf_unknown("resume", pdf)]}, 2

    header = [
        f"# trace — {folder.name}",
        "",
        f"job_id `{job_id}`, lane `{lane}`. Written by `uv run app build resume`.",
        "",
    ]
    _atomic_write(folder / "trace.md", "\n".join(header + built.trace) + "\n")
    rel = str(folder.relative_to(paths.APPLICATIONS))
    state_io.append_tailored_dir(state_io.state_path_for(paths.PIPELINE, job_id), rel)
    return {
        "ok": True,
        "dir": str(folder.relative_to(paths.APPLICATIONS.parent)),
        "docx": docx.name,
        "pdf": pdf,
        "swapped": built.swapped,
        "warnings": built.warnings,
    }, 0


# =====================================================================
# Prose checks (letter, answers)
# =====================================================================


def _prose_problems(
    src,
    where: str,
    text: str,
    uses: list[str],
    grounds: str,
    keep: list[str],
    warnings: list[dict],
    context: Literal["resume", "outreach"] = "resume",
) -> list[dict]:
    """Numbers must come from a cited bullet or the JD; lint with keep-terms.
    `warn` lint hits go to `warnings`, never to the problems."""
    out = [
        {"where": where, "code": ProblemCode.UNKNOWN_ID, "detail": f"{u} is not in bullets.md"}
        for u in uses
        if u not in src.bullets
    ]
    allowed = " ".join(
        [grounds, resume_model.context_text(src)] + [resume_model.bullet_text(src, u) for u in uses]
    )
    out += [
        {
            "where": where,
            "code": ProblemCode.NUMBER,
            "detail": f"{n} is in no cited bullet and not in the JD",
        }
        for n in resume_model.ungrounded_numbers(text, allowed)
    ]
    found = lint.find_phrase_violations(text, context=context, rules=src.rules, keep=keep)
    warnings += [resume_model.lint_warning(where, v) for v in found if v["level"] == "warn"]
    out += [
        {
            "where": where,
            "code": ProblemCode.LINT,
            "detail": f"banned phrase {v['phrase']!r} ({v['category']})",
        }
        for v in lint.blocking(found)
    ]
    return out


def _sentences(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s])


def cmd_build_answers(job_id: str) -> tuple[dict, int]:
    folder = _built_dir(job_id)
    facts, jd = _read_snapshot(folder)
    data = _load_yaml(folder / ANSWERS)
    if bad := session_schema.problems("answers", data):
        return {"ok": False, "problems": bad}, 2
    src = resume_model.load_sources()
    problems: list[dict] = []
    warnings: list[dict] = []
    uses = [str(u) for u in data.get("uses") or []]
    keep = resume_model.keep_terms(src, str(facts.get("company", "")))
    for k in ANSWER_KEYS:
        v = data.get(k)
        if v is None:
            continue
        if not isinstance(v, str) or not v.strip():
            problems.append(
                {"where": k, "code": ProblemCode.LAYOUT, "detail": "must be text or null"}
            )
            continue
        v = lint.fix_mechanical(v.strip(), src.rules)[0]
        data[k] = v
        if not 2 <= _sentences(v) <= 4:
            problems.append(
                {
                    "where": k,
                    "code": ProblemCode.LAYOUT,
                    "detail": f"{_sentences(v)} sentences; write 2-4",
                }
            )
        problems += _prose_problems(src, k, v, uses, jd, keep, warnings)
    if problems:
        return {"ok": False, "problems": problems}, 2
    _atomic_write(
        folder / ANSWERS, yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=1000)
    )
    return {
        "ok": True,
        "dir": str(folder.relative_to(paths.APPLICATIONS.parent)),
        "answered": [k for k in ANSWER_KEYS if data.get(k)],
        "no_research": [k for k in ANSWER_KEYS if not data.get(k)],
        "warnings": warnings,
    }, 0


def _names_company(company: str, text: str) -> bool:
    """The full name, or its first word ("Bain" for "Bain & Company")."""
    low = text.lower()
    first = re.split(r"[\s,&]+", company.strip())[0]
    return company.lower() in low or (
        len(first) >= 3 and re.search(rf"(?<!\w){re.escape(first.lower())}(?!\w)", low) is not None
    )


def cmd_build_letter(
    job_id: str, today: date | None = None, no_pdf: bool = False
) -> tuple[dict, int]:
    folder = _built_dir(job_id)
    facts, jd = _read_snapshot(folder)
    data = _load_yaml(folder / LETTER_PLAN)
    if bad := session_schema.problems("letter", data):
        return {"ok": False, "problems": bad}, 2
    src = resume_model.load_sources()
    _, slug = _identity()
    company = str(facts.get("company", ""))
    problems: list[dict] = []
    warnings: list[dict] = []

    body = data.get("body")
    if (
        not isinstance(body, list)
        or not body
        or not all(isinstance(p, str) and p.strip() for p in body)
    ):
        return {
            "ok": False,
            "problems": [
                {
                    "where": "body",
                    "code": ProblemCode.LAYOUT,
                    "detail": "must be a list of paragraph strings",
                }
            ],
        }, 2
    salutation = str(data.get("salutation") or "").strip()
    if not salutation:
        problems.append({"where": "salutation", "code": ProblemCode.LAYOUT, "detail": "missing"})
    recipient = re.sub(r"^(dear|hi|hello)\s+|,$", "", salutation, flags=re.IGNORECASE).strip()
    keep = resume_model.keep_terms(src, company, recipient)
    uses = [str(u) for u in data.get("uses") or []]
    if not uses:
        problems.append(
            {
                "where": "uses",
                "code": ProblemCode.LAYOUT,
                "detail": "cite the bullets the letter draws on",
            }
        )

    def fix(t):
        return lint.fix_mechanical(t.strip(), src.rules)[0]

    salutation = fix(salutation)
    body = [fix(p) for p in body]
    problems += _prose_problems(src, "salutation", salutation, [], "", keep, warnings)
    for i, p in enumerate(body):
        problems += _prose_problems(src, f"body[{i}]", p, uses, jd, keep, warnings)
    if company and not _names_company(company, " ".join(body)):
        problems.append(
            {"where": "body", "code": ProblemCode.LAYOUT, "detail": f"never names {company!r}"}
        )
    if problems:
        return {"ok": False, "problems": problems}, 2

    today = today or clock.today()
    content = {
        "salutation": salutation,
        "body": body,
        "date": f"{today:%B} {today.day}, {today.year}",
    }
    docx = folder / f"{slug}_Cover_Letter.docx"
    try:
        render_cover_letter(content, paths.PROFILE / "cover_letter_template.docx", docx)
    except (TemplateError, FileNotFoundError, ValueError) as exc:
        raise AppError(str(exc)) from exc
    pdf = _pdf(docx, folder.name, no_pdf)
    words = sum(len(p.split()) for p in body)
    if pdf["status"] == "ok" and pdf["pages"] > 1:
        return {
            "ok": False,
            "words": words,
            "problems": [
                {
                    "where": "body",
                    "code": ProblemCode.PAGES,
                    "detail": f"renders to {pdf['pages']} pages ({words} words); cut the body",
                }
            ],
        }, 2
    if pdf["status"] != "ok" and not no_pdf:
        return {"ok": False, "words": words, "problems": [_pdf_unknown("body", pdf)]}, 2
    return {
        "ok": True,
        "dir": str(folder.relative_to(paths.APPLICATIONS.parent)),
        "docx": docx.name,
        "pdf": pdf,
        "words": words,
        "warnings": warnings,
    }, 0


# =====================================================================
# build outreach
# =====================================================================


def _outreach_limits() -> dict:
    limits = config_schema.setting("outreach_word_limits", None)
    if not limits:
        raise AppError(
            "profile/settings.yaml has no outreach_word_limits; copy that block "
            "from profile/settings.example.yaml."
        )
    return limits


def cmd_build_outreach(job_id: str, now: datetime | None = None) -> tuple[dict, int]:
    folder = _built_dir(job_id)
    facts, jd = _read_snapshot(folder)
    data = _load_yaml(folder / OUTREACH)
    if bad := session_schema.problems("outreach", data):
        return {"ok": False, "problems": bad}, 2
    limits = _outreach_limits()
    src = resume_model.load_sources()
    company = str(facts.get("company", ""))
    channel, medium = data["channel"], data["medium"]
    to = str(data.get("to") or "").strip()
    subject = lint.fix_mechanical(str(data.get("subject") or "").strip(), src.rules)[0]
    body = lint.fix_mechanical(str(data["body"]).strip(), src.rules)[0]
    uses = [str(u) for u in data.get("uses") or []]
    keep = resume_model.keep_terms(src, company, *([to] if to else []))
    problems: list[dict] = []
    warnings: list[dict] = []

    def layout(where: str, detail: str) -> None:
        problems.append({"where": where, "code": ProblemCode.LAYOUT, "detail": detail})

    def length(where: str, words: int, limit: int, what: str) -> None:
        if words > limit:
            problems.append(
                {
                    "where": where,
                    "code": ProblemCode.LENGTH,
                    "detail": f"{words} words; {what} allows {limit}",
                }
            )

    if channel != "recruiter" and not to:
        layout("to", f"a {channel} message is addressed to a person")
    if medium == "email" and not subject:
        layout("subject", "an email needs a subject line")
    if medium == "linkedin" and subject:
        layout("subject", "a LinkedIn DM has no subject line")
    limit = limits[f"recruiter_{medium}" if channel == "recruiter" else channel]
    words = len(body.split())
    length("body", words, limit, f"a {channel} {medium} message")
    length("subject", len(subject.split()), limits["subject"], "a subject line")
    if subject:
        problems += _prose_problems(src, "subject", subject, uses, jd, keep, warnings, "outreach")
    problems += _prose_problems(src, "body", body, uses, jd, keep, warnings, "outreach")
    if problems:
        return {"ok": False, "words": words, "problems": problems}, 2

    at = (now or clock.now()).replace(microsecond=0)
    slug = re.sub(r"[^a-z0-9]+", "-", to.lower()).strip("-") or "unknown"
    name = f"{at.date().isoformat()}_{channel}_{slug}.md"
    head = {
        "job_id": job_id,
        "channel": channel,
        "to": to or None,
        "contact_id": data.get("contact_id"),
        "medium": medium,
        "generated_at": at.isoformat(),
        "status": "draft",
    }
    parts = ["---", yaml.safe_dump(head, sort_keys=False, allow_unicode=True).rstrip(), "---", ""]
    if subject:
        parts += ["# Subject", "", subject, ""]
    parts += ["# Body", "", body, "", "# Why this approach", "", str(data["why"]).strip(), ""]
    draft = paths.PIPELINE / job_id / "outreach" / name
    draft.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(draft, "\n".join(parts))

    entries = _state(job_id).get("outreach") or []
    if name not in {e.get("draft_file") for e in entries if isinstance(e, dict)}:
        argv = ["outreach-draft", job_id, "--channel", channel, "--to", to or "unknown"]
        argv += ["--draft-file", name, "--medium", medium]
        if data.get("contact_id"):
            argv += ["--contact-id", str(data["contact_id"])]
        with contextlib.redirect_stdout(sys.stderr):
            if track_cli.main(argv) != 0:
                raise AppError(f"could not record {name} in {job_id}'s outreach[].")
    return {
        "ok": True,
        "draft": str(draft.relative_to(paths.PIPELINE.parent)),
        "draft_file": name,
        "words": words,
        "limit": limit,
        "warnings": warnings,
    }, 0


# =====================================================================
# dupes
# =====================================================================

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


def cmd_lint_outreach(draft: str, company: str, recipient: str | None) -> dict:
    path = Path(draft)
    src = resume_model.load_sources()
    fixed, subs = lint.fix_mechanical(path.read_text(encoding="utf-8"), src.rules)
    path.write_text(fixed, encoding="utf-8")
    keep = resume_model.keep_terms(src, company, *([recipient] if recipient else []))
    found = lint.find_phrase_violations(fixed, context="outreach", rules=src.rules, keep=keep)
    return {
        "mech_subs": len(subs),
        "violations": lint.blocking(found),
        "warnings": [v for v in found if v["level"] == "warn"],
    }


# =====================================================================
# CLI
# =====================================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="uv run app", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_new = sub.add_parser("new")
    p_new.add_argument("job_id")
    p_new.add_argument("--today", default=None)
    p_dir = sub.add_parser("dir")
    p_dir.add_argument("job_id")
    p_dupes = sub.add_parser("dupes")
    p_dupes.add_argument("job_id")
    p_build = sub.add_parser("build")
    p_build.add_argument("what", choices=["resume", "letter", "answers", "outreach"])
    p_build.add_argument("job_id")
    p_build.add_argument(
        "--final",
        action="store_true",
        help="resume only: fall back to canonical text where a rewording fails",
    )
    p_build.add_argument(
        "--no-pdf", action="store_true", help="resume/letter: pass without a pdf page count"
    )
    p_schema = sub.add_parser("schema")
    p_schema.add_argument("kind", choices=list(session_schema.KINDS))
    p_lint = sub.add_parser("lint")
    p_lint.add_argument("what", choices=["outreach"])
    p_lint.add_argument("draft")
    p_lint.add_argument("--company", required=True)
    p_lint.add_argument("--recipient", default=None)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.cmd == "new":
            return _emit(cmd_new(args.job_id, args.today))
        if args.cmd == "dir":
            return _emit(role_info(args.job_id))
        if args.cmd == "dupes":
            return _emit(cmd_dupes(args.job_id))
        if args.cmd == "schema":
            fname, model = session_schema.KINDS[args.kind]
            return _emit({"file": fname, "schema": model.model_json_schema()})
        if args.cmd == "lint":
            if not Path(args.draft).is_file():
                print(json.dumps({"ok": False, "error": f"{args.draft}: no such draft"}))
                return 2
            return _emit(cmd_lint_outreach(args.draft, args.company, args.recipient))
        if args.what == "resume":
            out, code = cmd_build_resume(args.job_id, final=args.final, no_pdf=args.no_pdf)
        elif args.what == "letter":
            out, code = cmd_build_letter(args.job_id, no_pdf=args.no_pdf)
        elif args.what == "outreach":
            out, code = cmd_build_outreach(args.job_id)
        else:
            out, code = cmd_build_answers(args.job_id)
        return _emit(out, code)
    except (AppError, resume_model.SourceError, lint.LintConfigError) as exc:
        return _emit({"ok": False, "error": str(exc)}, 1)


if __name__ == "__main__":
    sys.exit(main())
