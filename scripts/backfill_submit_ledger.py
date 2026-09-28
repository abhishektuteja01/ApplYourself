"""Rebuild `applications/apply_runs/submitted.jsonl` from what already went out.

One-off. Sources: every run report's submitted sections, every post-submit
evidence capture (which carries the form URL and any rejection text), and every
pipeline role whose history reached `applied`. Writes one `backfill` line per
role not already in the ledger. Safe to re-run.

    uv run python scripts/backfill_submit_ledger.py [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from src import paths  # noqa: E402
from src.apply import ledger  # noqa: E402

RUNS = paths.APPLICATIONS / "apply_runs"
_SUBMITTED_HEADING = re.compile(r"^## (Submitted|SUBMITTED)")
_ROLE_LINE = re.compile(r"^- `([0-9a-f]{8})`")
_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2})-(\d{2})-(\d{2})")


def _at(name: str) -> str:
    m = _STAMP.search(name)
    return f"{m.group(1)}:{m.group(2)}:{m.group(3)}" if m else ""


def from_reports() -> dict[str, dict]:
    found: dict[str, dict] = {}
    for report in sorted(RUNS.glob("*.md")):
        in_submitted = False
        for line in report.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                in_submitted = bool(_SUBMITTED_HEADING.match(line))
                continue
            m = _ROLE_LINE.match(line)
            if in_submitted and m and m.group(1) not in found:
                found[m.group(1)] = {"at": _at(report.name),
                                     "detail": f"run report {report.name}"}
    return found


def from_evidence() -> dict[str, dict]:
    found: dict[str, dict] = {}
    for cap in sorted((RUNS / "evidence").glob("*.txt")):
        text = cap.read_text(encoding="utf-8", errors="replace")
        job = re.search(r"^job_id: (\S+)", text, re.M)
        url = re.search(r"^form_url: (\S+)", text, re.M)
        company = re.search(r"^company: (.*)$", text, re.M)
        if not job:
            continue
        rec = found.setdefault(job.group(1), {"at": _at(cap.name)})
        if url:
            rec["form_url"] = url.group(1)
        if company:
            rec["company"] = company.group(1).strip()
        if "couldn't submit your application" in text.casefold() or \
                "couldn’t submit your application" in text.casefold():
            rec["detail"] = f"board rejected it (evidence {cap.name})"
    return found


def from_pipeline() -> dict[str, dict]:
    found: dict[str, dict] = {}
    for state_file in sorted(paths.PIPELINE.glob("*/state.yaml")):
        st = yaml.safe_load(state_file.read_text(encoding="utf-8")) or {}
        history = [h.get("state") for h in st.get("state_history") or [] if isinstance(h, dict)]
        if st.get("state") == "applied" or "applied" in history:
            found[state_file.parent.name] = {
                "company": st.get("company", ""), "title": st.get("title", ""),
                "form_url": st.get("url", ""), "detail": "state.yaml reached applied",
            }
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    path = RUNS / ledger.LEDGER_NAME
    have = ledger.sent_job_ids(path)
    reports, evidence, pipeline = from_reports(), from_evidence(), from_pipeline()
    # Evidence alone only proves a click when a report or a state says so, or
    # the board answered; a capture is written for every attempted click.
    job_ids = set(reports) | set(pipeline) | {
        j for j, r in evidence.items() if "rejected" in r.get("detail", "")}
    added = 0
    for job_id in sorted(job_ids - have):
        rec = {**pipeline.get(job_id, {}), **evidence.get(job_id, {}), **reports.get(job_id, {})}
        if "rejected" in evidence.get(job_id, {}).get("detail", ""):
            rec["detail"] = evidence[job_id]["detail"]
        if not args.dry_run:
            ledger.append(path, job_id, "backfill", form_url=rec.get("form_url", ""),
                          company=rec.get("company", ""), title=rec.get("title", ""),
                          detail=rec.get("detail", ""), at=rec.get("at") or None)
        added += 1
    print(f"reports {len(reports)} · evidence {len(evidence)} · pipeline {len(pipeline)} "
          f"· already in ledger {len(have)} · {'would add' if args.dry_run else 'added'} {added}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
