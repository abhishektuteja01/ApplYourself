"""One-time: turn the old `apply_runs/<stem>.md` reports into `<stem>.json`.

The only markdown parser of run reports. It writes a JSON beside each report
that has none; the reports themselves are left as they are. `--dry-run` writes
nothing: it counts what parses and checks that the sheet built from the
backfilled JSON matches the sheet built from the reports today.

  uv run python -m applyourself.apply.run_backfill --dry-run [--runs DIR]
  uv run python -m applyourself.apply.run_backfill --apply [--runs DIR]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from applyourself.apply import run_record
from applyourself.apply.run_record import ReasonCode
from applyourself.shared import paths

_HEADER = re.compile(r"^# apply run\b(?: — (\S+))?")
_ROLE_LINE = re.compile(r"^- `([0-9a-f]{8})`(?: (.*))?$")
_PARKED_ROW = re.compile(r"^`(.+?)` \[(.+?)\] (.*)$")
_BANK = re.compile(r"^answered from the bank: (\d+); learned: (\d+)$")
_EVIDENCE = re.compile(r"^what the board showed after the click: `(.*)`$")
_TOTALS = re.compile(r"<!-- totals parked=(\d+) bank=(\d+) learned=(\d+) roles=(\d+) -->")
# Most specific first: several headings start with "Submitted".
_HEADINGS = (
    ("SUBMITTED", "submitted_untracked"),
    ("Submitted (no confirmation", "submitted_unconfirmed"),
    ("Submitted", "submitted"),
    ("Rejected", "rejected"),
    ("Clicked", "unsent"),
    ("Parked", "parked"),
    ("Ready", "ready"),
    ("Manual", "manual"),
    ("Skipped", "skipped"),
    ("Failed", "failed"),
    ("Expired", "expired"),
)


def _new_role(job_id: str, who: str, category: str) -> dict:
    company, _, title = (who if who != job_id else "").partition(" — ")
    return {
        "job_id": job_id,
        "company": company,
        "title": title,
        "category": category,
        "reason": str(ReasonCode.UNKNOWN),
        "detail": "",
        "fields_parked": [],
        "from_bank": 0,
        "learned": [],
        "evidence": None,
        "at": None,
    }


def parse_report(path: Path) -> dict | None:
    """The report as a run record, or None when it is not a run report."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    header = _HEADER.match(lines[0]) if lines else None
    if header is None:
        return None
    roles: list[dict] = []
    category = ""
    cur: dict | None = None
    # What the next "    - " line belongs to: a parked row, the learned list, or the detail.
    nest: str = ""
    for line in lines[1:]:
        if line.startswith("## "):
            category = next((c for h, c in _HEADINGS if line[3:].startswith(h)), "")
            cur, nest = None, ""
            continue
        m = _ROLE_LINE.match(line)
        if m:
            cur, nest = None, ""
            if category:
                cur = _new_role(m.group(1), m.group(2) or "", category)
                roles.append(cur)
            continue
        if cur is None or not line.strip():
            if not line.strip():
                cur, nest = None, ""
            continue
        if line.startswith("  - "):
            body = line[4:]
            if p := _PARKED_ROW.match(body):
                cur["fields_parked"].append(
                    {"id": p.group(1), "kind": p.group(2), "label": p.group(3)}
                )
                nest = "row"
            elif b := _BANK.match(body):
                cur["from_bank"] = int(b.group(1))
                nest = "learned"
            elif e := _EVIDENCE.match(body):
                cur["evidence"] = e.group(1)
                nest = ""
            elif not cur["detail"] and not cur["fields_parked"]:
                cur["detail"] = body
                nest = "detail"
            else:
                cur["detail"] += "\n" + line
            continue
        if line.startswith("    - ") and nest == "row":
            row, body = cur["fields_parked"][-1], line[6:]
            if body.startswith("offers: ") and "reason" in row:
                row["options"] = body[len("offers: ") :].split(", ")
            elif body.startswith("note: ") and "reason" in row:
                row["description"] = body[len("note: ") :]
            elif "reason" not in row:
                row["reason"] = body
            continue
        if line.startswith("    - ") and nest == "learned":
            cur["learned"].append(line[6:])
            continue
        if cur["detail"]:
            cur["detail"] += "\n" + line
    t = _TOTALS.search(text)
    return {
        "schema": run_record.SCHEMA,
        "source": "backfill",
        "started_at": header.group(1) or path.stem,
        "updated_at": None,
        "totals": (
            {"parked": int(t[1]), "bank": int(t[2]), "learned": int(t[3]), "roles": int(t[4])}
            if t
            else None
        ),
        "roles": roles,
    }


def legacy_outcomes(runs_dir: Path) -> dict[str, tuple[str, str, str]]:
    """job_id -> (category, first detail line, report stem), read the way the
    sheet read the reports before the JSON: the reference the backfill is checked against."""
    role_line = re.compile(r"^- `([0-9a-f]{8})`")
    headings = tuple((h, run_record.SHEET_CATEGORY[c]) for h, c in _HEADINGS)
    out: dict[str, tuple[str, str, str]] = {}
    for report in sorted(runs_dir.glob("*.md")):
        category, job, detail = "", "", ""
        for line in report.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("## "):
                category = next((c for h, c in headings if line[3:].startswith(h)), "")
                continue
            m = role_line.match(line)
            if m and category:
                job, detail = m.group(1), ""
                out[job] = (category, "", report.stem)
            elif job and line.startswith("  - ") and not detail and job in out:
                detail = line[4:].strip()
                out[job] = (out[job][0], detail, report.stem)
    return out


def plan(runs_dir: Path) -> tuple[dict[str, dict], list[str], int]:
    """(stem -> record to write, reports that do not parse, reports that already have JSON)."""
    todo: dict[str, dict] = {}
    failed: list[str] = []
    done = 0
    for report in sorted(runs_dir.glob("*.md")):
        if report.with_suffix(".json").exists():
            done += 1
            continue
        rec = parse_report(report)
        if rec is None:
            failed.append(report.name)
        else:
            todo[report.stem] = rec
    return todo, failed, done


def compare(runs_dir: Path, todo: dict[str, dict]) -> tuple[list[str], list[str]]:
    """(roles whose outcome differs, sheet rows whose status differs) between
    the reports read today and the backfilled records."""
    from applyourself.apply import sheet

    stems = {p.stem for p in runs_dir.glob("*.md")} | {p.stem for p in runs_dir.glob("*.json")}
    recs = []
    for stem in sorted(stems, key=lambda s: f"{s}.md"):
        rec = todo.get(stem) or run_record.load(runs_dir / f"{stem}.json")
        if rec is not None:
            recs.append((stem, rec))
    before = legacy_outcomes(runs_dir)
    after = run_record.outcomes_from(recs)
    outcome_diff = sorted(j for j in before.keys() | after.keys() if before.get(j) != after.get(j))
    old, new = sheet.statuses(before), sheet.statuses(after)
    row_diff = sorted(j for j in old if old[j] != new[j])
    return outcome_diff, row_diff


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_backfill", description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--runs", type=Path, default=None, help="default: applications/apply_runs")
    args = parser.parse_args(argv)
    runs_dir = args.runs or paths.APPLICATIONS / "apply_runs"
    todo, failed, done = plan(runs_dir)
    print(f"reports: {len(todo)} parse, {len(failed)} fail, {done} already have JSON")
    for name in failed:
        print(f"  does not parse: {name}")
    outcome_diff, row_diff = compare(runs_dir, todo)
    print(f"outcomes that differ: {len(outcome_diff)}; sheet rows that differ: {len(row_diff)}")
    for job in (outcome_diff + row_diff)[:20]:
        print(f"  differs: {job}")
    if args.dry_run:
        return 1 if outcome_diff or row_diff else 0
    if outcome_diff or row_diff:
        print("ERROR: not writing; the backfill would change the sheet", file=sys.stderr)
        return 1
    for stem, rec in todo.items():
        run_record.write(rec, runs_dir / f"{stem}.json")
    print(f"wrote {len(todo)} JSON record(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
