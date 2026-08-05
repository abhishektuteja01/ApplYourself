"""Deterministic CLI plumbing for /tailor (R7: no LLM calls).

Consolidates the deterministic front-matter of /tailor so the slash-command
markdown stays a thin sequence of one-liners (same pattern as score_cli /
track_cli). Judging and drafting stay in the .claude/commands/tailor.md session.

  prep <job_id> [--today YYYY-MM-DD]     run every prereq check, load+merge the
                                         clean/scored row to /tmp/tailor_<id>_
                                         row.json, resolve the vertical, and
                                         create the versioned output dir. Prints
                                         shell-eval-able var assignments
                                         (VERTICAL/DIRNAME/OUT_DIR/DICTION_PASS/
                                         ROW_JSON) to STDOUT; the full row JSON +
                                         status go to STDERR. Fails loud (nonzero,
                                         no partial vars) at the first bad check.

  snapshot <job_id> <out_dir> [--today YYYY-MM-DD]
                                         write <out_dir>/jd_snapshot.md from the
                                         row.json already on disk (no second
                                         clean.parquet read).

Run directly: `uv run python -m src.tailor_cli <job_id>` or
`uv run tailor-prep <job_id>`.

The verticals config itself (schema + per-vertical prose/resume files) is
validated by the separate `uv run python -m src.verticals` prereq the command
runs first; prep only consumes the loaded config via verticals.get_config().
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from src import verticals

CLEAN = Path("jobs/clean.parquet")
SCORED = Path("jobs/scored.parquet")
PIPELINE = Path("pipeline")
PROFILE = Path("profile")
APPLICATIONS = Path("applications")
TMPDIR = Path("/tmp")

# profile/preferences.md is deliberately NOT required here: /tailor never reads
# it (it is a /score concern), and scored.parquet — a hard prereq below — cannot
# exist without /score having already gated on it.
REQUIRED_PROFILE_FILES: list[tuple[str, str]] = [
    ("bullets.md", "author it."),
    ("de_ai_rules.yaml", "author it."),
    ("skills_master.md", "author it."),
    ("resume_template.docx",
     "author it in Word with the ATS constraints (Calibri, single column, "
     "no tables, bold section headers, no images/icons)."),
]

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")


def _row_json_path(job_id: str) -> Path:
    return TMPDIR / f"tailor_{job_id}_row.json"


def _die(message: str) -> "SystemExit":
    return SystemExit(f"ERROR: {message}")


def _load_row(job_id: str) -> dict:
    """Merge the clean + scored rows for job_id, matching the historical inline
    dump (clean first, scored overrides). Raises SystemExit if either is
    missing the id."""
    clean = pd.read_parquet(CLEAN).set_index("job_id")
    scored = pd.read_parquet(SCORED).set_index("job_id")
    if job_id not in clean.index:
        raise _die(f"job_id {job_id} not in clean.parquet")
    if job_id not in scored.index:
        raise _die(f"job_id {job_id} not in scored.parquet -- run /score first")
    return {**clean.loc[job_id].to_dict(), **scored.loc[job_id].to_dict()}


def _dump_row_json(job_id: str, row: dict) -> Path:
    p = _row_json_path(job_id)
    serializable = {
        k: (v.isoformat() if hasattr(v, "isoformat") else v)
        for k, v in row.items()
    }
    p.write_text(json.dumps(serializable, default=str, indent=2))
    return p


def _resolve_vertical(row: dict) -> str:
    cfg = verticals.get_config()
    v = row.get("vertical") or ""
    return v if v in cfg.verticals else cfg.default_vertical


def _versioned_dirname(vertical: str, company_slug: str, title_slug: str,
                       job_id: str, today: str) -> str:
    """Versioning across the role's lifetime: the first tailor gets the bare
    name, each re-tailor bumps _vN. The leading date is always today's, never
    the original date.

    Version numbers are max(existing)+1, never len(existing): counting priors
    reused a live name once any intermediate version had been deleted."""
    base = f"{vertical}/{today}_{company_slug}_{title_slug}_{job_id}"
    prior = list((APPLICATIONS / vertical).glob(f"*_{job_id}"))
    prior += list((APPLICATIONS / vertical).glob(f"*_{job_id}_v*"))
    if not prior:
        return base
    highest = 1
    for p in prior:
        m = re.search(rf"_{re.escape(job_id)}_v(\d+)$", p.name)
        if m:
            highest = max(highest, int(m.group(1)))
    return f"{base}_v{highest + 1}"


def _cmd_prep(args: argparse.Namespace) -> int:
    job_id = args.job_id
    today = args.today or date.today().isoformat()

    if not CLEAN.exists():
        raise _die("jobs/clean.parquet missing — run discovery first.")
    if not SCORED.exists():
        raise _die("jobs/scored.parquet missing — run /score first.")
    for fname, hint in REQUIRED_PROFILE_FILES:
        if not (PROFILE / fname).is_file():
            raise _die(f"profile/{fname} missing — {hint}")
    state_path = PIPELINE / job_id / "state.yaml"
    if not state_path.is_file():
        raise _die(
            f"pipeline/{job_id}/state.yaml missing. Run "
            f"`uv run python -m src.track_cli ensure {job_id}` first to "
            "register the role. /tailor does this for you."
        )

    row = _load_row(job_id)
    row_json = _dump_row_json(job_id, row)

    diction = (PROFILE / "de_ai_rules.yaml").read_text()
    diction_pass = "true" if "bullets_diction_pass_completed: true" in diction else "false"

    vertical = _resolve_vertical(row)
    company_slug = _slug(str(row.get("company", "")))
    title_slug = _slug(str(row.get("title", "")))[:60]

    dirname = _versioned_dirname(vertical, company_slug, title_slug, job_id, today)
    out_dir = APPLICATIONS / dirname
    # _versioned_dirname never reuses a number, so this means a bug.
    if out_dir.exists():
        raise _die(
            f"{out_dir} already exists -- refusing to overwrite a previous "
            "tailor's artifacts. Move or delete it, then re-run."
        )
    out_dir.mkdir(parents=True)

    # STDERR: human status + the full row (kept out of stdout so the eval below
    # stays clean; the command's Step 2 also reads the row.json file).
    print(f"tailoring to: {out_dir}  (vertical={vertical})", file=sys.stderr)
    print(row_json.read_text(), file=sys.stderr)

    # STDOUT: single-quoted assignments for `eval "$(uv run tailor-prep ...)"`.
    # Every value is a slug / hex id / configured vertical name / fixed literal,
    # so it never contains a single quote.
    print(f"VERTICAL='{vertical}'")
    print(f"DIRNAME='{dirname}'")
    print(f"OUT_DIR='{out_dir}'")
    print(f"DICTION_PASS='{diction_pass}'")
    print(f"ROW_JSON='{row_json}'")
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    job_id = args.job_id
    out_dir = Path(args.out_dir)
    today = args.today or date.today().isoformat()

    row_json = _row_json_path(job_id)
    if not row_json.is_file():
        raise _die(f"{row_json} missing — run `tailor-prep {job_id}` first.")
    row = json.loads(row_json.read_text())

    jd_text = row.get("jd_text") or ""
    snap = (
        "---\n"
        f"job_id: {job_id}\n"
        f"company: {row.get('company', '')}\n"
        f"title: {row.get('title', '')}\n"
        f"url: {row.get('url', '')}\n"
        f"posted_date: {row.get('posted_date', '')}\n"
        f"snapshot_at: {today}\n"
        "---\n\n"
        f"{jd_text}\n"
    )
    (out_dir / "jd_snapshot.md").write_text(snap)
    print(f"jd_snapshot.md written: {len(jd_text)} chars of JD body")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if argv and argv[0] == "snapshot":
        parser = argparse.ArgumentParser(prog="python -m src.tailor_cli snapshot")
        parser.add_argument("job_id")
        parser.add_argument("out_dir")
        parser.add_argument("--today", default=None)
        return _cmd_snapshot(parser.parse_args(argv[1:]))

    parser = argparse.ArgumentParser(
        prog="python -m src.tailor_cli", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("job_id")
    parser.add_argument("--today", default=None)
    return _cmd_prep(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
