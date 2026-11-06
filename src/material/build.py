"""`uv run app build resume|letter|answers|outreach`: check, render and record
what a session wrote. Part of `uv run app` (src/app_cli.py)."""

from __future__ import annotations

import contextlib
import re
import sys
from datetime import date, datetime
from pathlib import Path

import yaml

from src import lint, resume_model, session_schema, state_io, track_cli
from src.docx_cover_letter import render_cover_letter
from src.docx_render import PdfResult, TemplateError, export_pdf, render_resume
from src.material.checks import (
    _names_company,
    _outreach_limits,
    _pdf_unknown,
    _prose_problems,
    _sentences,
)
from src.material.folders import (
    ANSWER_KEYS,
    ANSWERS,
    LETTER_PLAN,
    OUTREACH,
    RESUME_PLAN,
    AppError,
    _atomic_write,
    _built_dir,
    _identity,
    _load_yaml,
    _read_snapshot,
    _state,
    role_info,
)
from src.session_schema import ProblemCode
from src.shared import clock, paths


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
