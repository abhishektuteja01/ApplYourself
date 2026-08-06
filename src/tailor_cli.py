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
                                         ROW_JSON/APPLICANT_NAME/FILE_SLUG) to
                                         STDOUT; the full row JSON +
                                         status go to STDERR. Fails loud (nonzero,
                                         no partial vars) at the first bad check.

  identity <vertical>                    print APPLICANT_NAME/FILE_SLUG only,
                                         read from that vertical's resume_file,
                                         for the commands that don't run prep.

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
import shlex
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

# The applicant's name is the first bold line of the vertical's resume_file —
# the same line src/docx_render.py parses as the `name` block. Reading it here
# keeps one source of truth for it and keeps the name out of `src/` and out of
# the command prose (R2).
_NAME_RE = re.compile(r"^\s*#?\s*\*\*(.+?)\*\*\s*$")
# Unicode-aware: an accented name keeps its letters ("José Álvarez" ->
# "José_Álvarez", not "Jos_lvarez").
_FILE_SLUG_RE = re.compile(r"[^\w]+", re.UNICODE)


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")


def file_slug(name: str) -> str:
    """Output-filename stem from a display name: "Ada Lovelace" -> "Ada_Lovelace"."""
    return _FILE_SLUG_RE.sub("_", name).strip("_")


def resume_display_name(resume_path: Path) -> str:
    """The applicant's display name, read from the first bold line of a
    vertical's resume_file. Raises SystemExit when the file has no such line,
    since every downstream artifact is named after it."""
    if not resume_path.is_file():
        raise _die(f"{resume_path} missing — the vertical's resume_file must exist.")
    for line in resume_path.read_text(encoding="utf-8").splitlines():
        m = _NAME_RE.match(line)
        if m and m.group(1).strip():
            return m.group(1).strip()
    raise _die(
        f"{resume_path} has no name line. Its first bold line must be your "
        "name (e.g. `**Ada Lovelace**`) — /tailor names every output file "
        "after it and src/docx_render.py renders it as the resume header."
    )


def _identity_for(vertical: str) -> tuple[str, str]:
    """(display_name, file_slug) for a configured vertical. resume_file is
    repo-relative, resolved against verticals.REPO_ROOT the same way
    verticals.main()'s existence check does."""
    cfg = verticals.get_config()
    if vertical not in cfg.verticals:
        raise _die(f"unknown vertical {vertical!r} — configured: {', '.join(cfg.names)}")
    name = resume_display_name(verticals.REPO_ROOT / cfg.verticals[vertical].resume_file)
    return name, file_slug(name)


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
    p.write_text(json.dumps(serializable, default=str, indent=2), encoding="utf-8")
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

    diction = (PROFILE / "de_ai_rules.yaml").read_text(encoding="utf-8")
    diction_pass = "true" if "bullets_diction_pass_completed: true" in diction else "false"

    vertical = _resolve_vertical(row)
    applicant_name, applicant_slug = _identity_for(vertical)
    company_slug = _slug(str(row.get("company", "")))
    title_slug = _slug(str(row.get("title", "")))[:60]

    dirname = _versioned_dirname(vertical, company_slug, title_slug, job_id, today)
    out_dir = APPLICATIONS / dirname
    # Invariant check, not an expected path: _versioned_dirname allocates an
    # unused suffix, so an existing dir means that allocation is broken. Kept
    # because what it protects is a previous run's artifacts.
    if out_dir.exists():
        raise _die(
            f"{out_dir} already exists -- refusing to overwrite a previous "
            "tailor's artifacts. Move or delete it, then re-run."
        )
    out_dir.mkdir(parents=True)

    # STDERR: human status + the full row (kept out of stdout so the eval below
    # stays clean; the command's Step 2 also reads the row.json file).
    print(f"tailoring to: {out_dir}  (vertical={vertical})", file=sys.stderr)
    print(row_json.read_text(encoding="utf-8"), file=sys.stderr)

    # STDOUT: quoted assignments for `eval "$(uv run tailor-prep ...)"`.
    # APPLICANT_NAME comes from a user-authored file and may contain an
    # apostrophe ("O'Brien"), so every value goes through shlex.quote rather
    # than relying on the others being quote-free slugs.
    print(f"VERTICAL={shlex.quote(vertical)}")
    print(f"DIRNAME={shlex.quote(dirname)}")
    print(f"OUT_DIR={shlex.quote(str(out_dir))}")
    print(f"DICTION_PASS={shlex.quote(diction_pass)}")
    print(f"ROW_JSON={shlex.quote(str(row_json))}")
    print(f"APPLICANT_NAME={shlex.quote(applicant_name)}")
    print(f"FILE_SLUG={shlex.quote(applicant_slug)}")
    return 0


def _cmd_identity(args: argparse.Namespace) -> int:
    """Same APPLICANT_NAME/FILE_SLUG lines prep prints, for the commands that
    don't run prep (/cover-letter reuses /tailor's existing output dir)."""
    name, slug = _identity_for(args.vertical)
    print(f"APPLICANT_NAME={shlex.quote(name)}")
    print(f"FILE_SLUG={shlex.quote(slug)}")
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    job_id = args.job_id
    out_dir = Path(args.out_dir)
    today = args.today or date.today().isoformat()

    row_json = _row_json_path(job_id)
    if not row_json.is_file():
        raise _die(f"{row_json} missing — run `tailor-prep {job_id}` first.")
    row = json.loads(row_json.read_text(encoding="utf-8"))

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
    (out_dir / "jd_snapshot.md").write_text(snap, encoding="utf-8")
    print(f"jd_snapshot.md written: {len(jd_text)} chars of JD body")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if argv and argv[0] == "identity":
        parser = argparse.ArgumentParser(prog="python -m src.tailor_cli identity")
        parser.add_argument("vertical")
        return _cmd_identity(parser.parse_args(argv[1:]))

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
