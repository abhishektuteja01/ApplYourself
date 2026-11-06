"""The checks `uv run app build` runs on session-written text: prose, pages,
outreach lint. Part of `uv run app` (src/app_cli.py)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from src import lint, resume_model
from src.material.folders import AppError
from src.session_schema import ProblemCode
from src.shared import config_schema


def _pdf_unknown(where: str, pdf: dict) -> dict:
    return {
        "where": where,
        "code": ProblemCode.PAGES,
        "detail": f"page count unknown: pdf {pdf['status']} "
        f"({pdf['detail']}); not a plan problem: rebuild once the pdf works, or pass --no-pdf",
    }


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


def _names_company(company: str, text: str) -> bool:
    """The full name, or its first word ("Bain" for "Bain & Company")."""
    low = text.lower()
    first = re.split(r"[\s,&]+", company.strip())[0]
    return company.lower() in low or (
        len(first) >= 3 and re.search(rf"(?<!\w){re.escape(first.lower())}(?!\w)", low) is not None
    )


def _outreach_limits() -> dict:
    limits = config_schema.setting("outreach_word_limits", None)
    if not limits:
        raise AppError(
            "profile/settings.yaml has no outreach_word_limits; copy that block "
            "from profile/settings.example.yaml."
        )
    return limits


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
