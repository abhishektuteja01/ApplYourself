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
import json
import sys
from pathlib import Path
from typing import Any

from applyourself import lint, resume_model, session_schema
from applyourself.material.build import (
    cmd_build_answers,
    cmd_build_letter,
    cmd_build_outreach,
    cmd_build_resume,
)
from applyourself.material.checks import cmd_lint_outreach
from applyourself.material.folders import AppError, role_info
from applyourself.material.new import cmd_dupes, cmd_new
from applyourself.shared.logs import setup_logging


def _emit(obj: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
    return code


# =====================================================================
# CLI
# =====================================================================


def main(argv: list[str] | None = None) -> int:
    setup_logging()
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
