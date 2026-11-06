"""Deterministic setup chores for /onboarding (R7: no LLM calls).

One command for every mechanical step of a fresh install: copy each
`profile/*.example.*` template to its real name, strip the shipped example
lanes out of the copied `verticals.yaml`, set the home country's
work-authorization row in `profile/application_answers.yaml` from the answer,
probe for libpostal, and — behind `--with-apply` — set up the submission path.

Never destructive: an existing file is skipped and listed, `--force`
overwrites, `--dry-run` prints the full plan and touches nothing.

Templates NOT copied by default, because /onboarding puts them on its "later,
when you want it" menu rather than the setup path:
  voice_samples / stories / contacts / pii_denylist -> --with-optional
      (/outreach's voice samples, /apply's story bank, optional contact and
      company lists, and the PII gate, which only matters once
      application_answers.yaml holds a real email and phone)

--with-apply copies no extra template: it installs the apply dependency group
and Playwright's Chrome. application_answers.yaml is always copied, so
/onboarding can fill it from the resume it has already parsed.

The verticals.yaml strip is line-based, not a parse-and-dump: `ruamel.yaml` is
not a dependency and PyYAML would drop every comment in the file.

After the strip, `verticals.yaml` no longer loads: `lanes:` is empty, there
are no classifier rules, and `default_vertical` names the lane the user has not
written yet. `/new-vertical <name>` fills all three and writes the lane's
`profile/verticals/<name>/lane.yaml`. That is the intended hand-off state, and
the summary says so.

Run: `uv run onboard-scaffold --vertical <name> --work-auth <status>`.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from applyourself.shared import paths
from applyourself.shared.logs import setup_logging

# Gated out of the default path — see the module docstring.
OPTIONAL_TEMPLATES = (
    "voice_samples.example.md",
    "stories.example.md",
    "contacts.example.yaml",
    "pii_denylist.example.txt",
)

WORK_AUTH_CHOICES = ("citizen", "needs_now", "time_limited")

# The loader's own lane-name pattern. An invalid name here writes an
# unloadable default_vertical, and the strip is not re-runnable to fix it.
VERTICAL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

LIBPOSTAL_INSTALL = {
    "darwin": "brew install libpostal",
    "linux": (
        "build libpostal from source (github.com/openvenues/libpostal) "
        "— no distro packages it; install its dev headers, ~2 GB data download"
    ),
}


@dataclass
class Report:
    """What happened, in the order it happened, plus what was left alone."""

    done: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(paths.root()))
    except ValueError:
        return str(path)


def _target_for(template: Path) -> Path:
    """profile/verticals.example.yaml -> profile/verticals.yaml."""
    head, _, tail = template.name.partition(".example.")
    return template.with_name(f"{head}.{tail}")


def _templates(with_optional: bool) -> list[Path]:
    """Every shipped template on the selected path, discovered by glob so a new
    one is picked up without a code change."""
    excluded = set()
    if not with_optional:
        excluded |= set(OPTIONAL_TEMPLATES)
    return [p for p in sorted(paths.PROFILE.glob("*.example.*")) if p.name not in excluded]


def copy_templates(templates: list[Path], force: bool, dry_run: bool, rep: Report) -> set[str]:
    """Copy each template to its real name. Returns the target names written
    (or, under --dry-run, that would be written)."""
    written: set[str] = set()
    for template in templates:
        target = _target_for(template)
        if target.exists() and not force:
            rep.skipped.append(f"{_rel(target)} exists — kept")
            continue
        verb = "overwrite" if target.exists() else "copy"
        if dry_run:
            rep.done.append(f"would {verb}: {_rel(template)} -> {_rel(target)}")
        else:
            shutil.copyfile(template, target)
            rep.done.append(f"{verb}: {_rel(template)} -> {_rel(target)}")
        written.add(target.name)
    return written


# --- verticals.yaml -------------------------------------------------------


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_filler(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _block_span(lines: list[str], start: int, indent: int) -> int:
    """End index (exclusive) of the block opened at `start`: everything up to
    the next real key at `indent` or less. Trailing blanks, and trailing
    comments no deeper than the block itself, introduce whatever comes next —
    they are handed back. A deeper comment is the block's own trailing note."""
    end = start + 1
    while end < len(lines):
        if not _is_filler(lines[end]) and _indent(lines[end]) <= indent:
            break
        end += 1
    while end - 1 > start:
        prev = lines[end - 1]
        if prev.strip() and _indent(prev) > indent:
            break
        end -= 1
    return end


def example_lane_names(example_path: Path) -> list[str]:
    """The lanes the shipped template defines. Derived, never hardcoded: a
    vertical name in src/ would break the vertical-agnostic rule."""
    data = yaml.safe_load(example_path.read_text(encoding="utf-8"))
    return list((data or {}).get("lanes") or [])


def strip_example_lanes(text: str, lanes: list[str], default_vertical: str) -> str:
    """Empty `lanes:`, remove every classifier rule naming an example lane,
    and repoint default_vertical. Comments and key order survive."""
    lines = text.splitlines()
    drop: set[int] = set()

    # A contiguous comment run goes as a unit if any line in it names a lane
    # being removed: half a paragraph about lanes that no longer exist reads
    # worse than none, and the sentences wrap across lines.
    run: list[int] = []
    for i, line in enumerate(lines + [""]):
        if line.strip().startswith("#"):
            run.append(i)
            continue
        if any(lane in lines[j] for j in run for lane in lanes):
            drop.update(run)
        run = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("- vertical:"):
            named = stripped.split(":", 1)[1].strip()
            if named in lanes:
                drop.update(range(i, _block_span(lines, i, _indent(line))))

    out: list[str] = [
        "# Your vertical registry, scaffolded from verticals.example.yaml by",
        "# `uv run onboard-scaffold`. Names must match ^[a-z][a-z0-9_]*$.",
        "",
    ]
    for i, line in enumerate(lines):
        if i in drop:
            continue
        if line.startswith("default_vertical:"):
            out.append(f"default_vertical: {default_vertical}")
            continue
        if line.startswith("lanes:"):
            out.append(f"# /new-vertical {default_vertical} adds your lane here.")
            out.append("lanes: []")
            continue
        if not line.strip() and out and not out[-1].strip():
            continue  # the removed blocks leave runs of blank lines behind
        out.append(line)
        stripped = line.strip()
        if stripped == "classifier_rules:":
            out.append(f"  # /new-vertical {default_vertical} writes your rules here.")
    return "\n".join(out) + "\n"


def reconcile_verticals(default_vertical: str, copied: bool, dry_run: bool, rep: Report) -> None:
    """Strip the example scaffolding out of the freshly copied verticals.yaml.

    Only ever touches a file this run wrote — so skip/force are inherited from
    the copy above. A verticals.yaml that was skipped is a live config with
    real lanes in it, and the strip is not idempotent against one."""
    example = paths.PROFILE / "verticals.example.yaml"
    target = paths.PROFILE / "verticals.yaml"
    if not example.is_file():
        rep.failed.append(f"{_rel(example)} missing — cannot scaffold verticals.yaml")
        return
    if not copied:
        rep.skipped.append(f"{_rel(target)} kept — example-lane strip not applied")
        return
    lanes = example_lane_names(example)
    if dry_run:
        rep.done.append(
            f"would strip from {_rel(target)}: `lanes:` {', '.join(lanes)}, every "
            f"classifier rule naming one; set default_vertical: {default_vertical}"
        )
        return
    stripped = strip_example_lanes(target.read_text(encoding="utf-8"), lanes, default_vertical)
    target.write_text(stripped, encoding="utf-8")
    rep.done.append(
        f"stripped {_rel(target)}: removed lanes {', '.join(lanes)} and their "
        f"classifier rules; default_vertical: {default_vertical}"
    )
    rep.notes.append(
        f"{_rel(target)} does not load yet — no lanes, no classifier rules. "
        f"Run `/new-vertical {default_vertical}` next."
    )


# --- work authorization -----------------------------------------------------

_ROW_BY_WORK_AUTH = {
    "citizen": {"authorized": True, "requires_sponsorship": False, "unrestricted": True},
    "time_limited": {"authorized": True, "requires_sponsorship": True},
    "needs_now": {"authorized": False, "requires_sponsorship": True},
}


def apply_work_auth(work_auth: str, dry_run: bool, rep: Report) -> None:
    """Set the home country's row in application_answers.yaml's
    work_authorization.countries from the work-authorization answer. Scoring
    and /apply both read it there. Idempotent; a re-run with a new answer
    updates it."""
    from applyourself.apply import answers as A
    from applyourself.apply import bank

    target = paths.PROFILE / "application_answers.yaml"
    source = target if target.is_file() else paths.PROFILE / "application_answers.example.yaml"
    if not source.is_file():
        rep.failed.append(f"{_rel(target)} missing and no template to copy it from")
        return
    data = bank.read(source)
    home = A.canonical_place((data.get("identity") or {}).get("country", ""))
    if not home:
        rep.failed.append(
            f"{_rel(target)}: identity.country names no country, so the "
            "home work-authorization row cannot be set"
        )
        return
    countries = data.setdefault("work_authorization", {}).setdefault("countries", {})
    key = next((k for k in countries if A.canonical_place(str(k)) == home), home)
    old = dict(countries.get(key) or {})
    row = dict(_ROW_BY_WORK_AUTH[work_auth])
    same = all(old.get(k) == v for k, v in row.items()) and bool(old.get("unrestricted")) == bool(
        row.get("unrestricted")
    )
    if same:
        rep.notes.append(f"{_rel(target)} already says {work_auth} for {home}")
        return
    if "start_in_days" in old:
        row["start_in_days"] = old["start_in_days"]
    cleared = [k for k in ("status_label", "followup") if old.get(k)]
    label = f"set {home}'s work authorization: {work_auth}"
    if dry_run:
        rep.done.append(f"would {label} in {_rel(target)}")
        return
    countries[key] = row
    bank.write(data, target)
    rep.done.append(f"{label} — {_rel(target)}")
    if cleared:
        rep.notes.append(
            f"{_rel(target)}: {', '.join(cleared)} described the old status "
            f"and were cleared; set them for {home} if a form should use them"
        )


# --- advisory probes and the optional apply path --------------------------


def probe_libpostal(rep: Report) -> None:
    """Advisory only: never installs, never changes the exit code."""
    try:
        __import__("postal.parser")
    except Exception:  # noqa: BLE001 - a broken native install raises anything
        hint = LIBPOSTAL_INSTALL.get(sys.platform, LIBPOSTAL_INSTALL["linux"])
        rep.notes.append(
            f"libpostal not importable — `uv run discover` needs it. Install it "
            f"({hint}), then `uv sync --group discovery`. Nothing else needs it."
        )
    else:
        rep.notes.append("libpostal importable — `uv run discover` is ready")


def setup_apply_path(dry_run: bool, rep: Report) -> bool:
    """`uv sync --group apply` plus Playwright's Chrome. True if both
    succeeded."""
    commands = [
        ["uv", "sync", "--group", "apply"],
        ["uv", "run", "playwright", "install", "chrome"],
    ]
    env = {**os.environ, "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1"}
    ok = True
    for cmd in commands:
        printable = " ".join(cmd)
        if dry_run:
            rep.done.append(f"would run: {printable}")
            continue
        result = subprocess.run(cmd, cwd=paths.REPO_ROOT, env=env, check=False)
        if result.returncode == 0:
            rep.done.append(f"ran: {printable}")
        else:
            rep.failed.append(f"`{printable}` exited {result.returncode}")
            ok = False
            break  # the Chrome install needs the synced group
    return ok


def _print_report(rep: Report, dry_run: bool) -> None:
    heading = "PLAN (--dry-run, nothing written)" if dry_run else "DONE"
    print(f"--- {heading} ---")
    for line in rep.done or ["(nothing)"]:
        print(f"  {line}")
    if rep.skipped:
        print("--- SKIPPED (already present; --force overwrites) ---")
        for line in rep.skipped:
            print(f"  {line}")
    if rep.notes:
        print("--- NOTES ---")
        for line in rep.notes:
            print(f"  {line}")
    if rep.failed:
        print("--- FAILED ---", file=sys.stderr)
        for line in rep.failed:
            print(f"  {line}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(
        prog="onboard-scaffold",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--vertical",
        required=True,
        help="name of the lane /new-vertical will write; becomes "
        "default_vertical. Must match ^[a-z][a-z0-9_]*$",
    )
    parser.add_argument("--work-auth", required=True, choices=WORK_AUTH_CHOICES)
    parser.add_argument(
        "--with-apply", action="store_true", help="also set up the /apply submission path"
    )
    parser.add_argument(
        "--with-optional",
        action="store_true",
        help="also copy the later-menu templates (voice samples, contacts, PII denylist)",
    )
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; write nothing")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if not VERTICAL_NAME_RE.match(args.vertical):
        print(
            f"--vertical {args.vertical!r} must match "
            f"{VERTICAL_NAME_RE.pattern} (lowercase, digits, underscores)",
            file=sys.stderr,
        )
        return 2

    rep = Report()
    templates = _templates(args.with_optional)
    written = copy_templates(templates, args.force, args.dry_run, rep)
    reconcile_verticals(args.vertical, "verticals.yaml" in written, args.dry_run, rep)
    apply_work_auth(args.work_auth, args.dry_run, rep)
    probe_libpostal(rep)

    ok = True
    if args.with_apply:
        ok = setup_apply_path(args.dry_run, rep)

    _print_report(rep, args.dry_run)
    return 0 if ok and not rep.failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
