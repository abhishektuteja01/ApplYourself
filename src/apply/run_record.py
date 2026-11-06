"""One `apply run` as JSON: `applications/apply_runs/<stem>.json`.

The JSON is the record; the `<stem>.md` report beside it is rendered from it,
and the sheet and summary read the JSON. Shape (`schema` 1):

    {"schema": 1, "source": "run" | "backfill", "started_at": iso,
     "updated_at": iso | null,
     "totals": {"parked", "bank", "learned", "roles"} | null,
     "roles": [{"job_id", "company", "title", "category", "reason", "detail",
                "fields_parked": [{"id", "label", "kind", "reason", "tier",
                                   "source", "options", "description"}],
                "from_bank", "learned", "evidence", "at"}]}

`category` is one of `CATEGORY_ORDER`; `reason` a `ReasonCode`. A backfilled
role has `reason: unknown` and `at: null`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path
from typing import Any

from src.shared import schemas
from src.shared.files import atomic_write_text

SCHEMA = schemas.APPLY_RUN_SCHEMA_VERSION


class ReasonCode(StrEnum):
    READY = "all_resolved"
    CONFIRMED = "confirmed"
    NO_CONFIRMATION = "no_confirmation"
    CRASH_AFTER_CLICK = "crash_after_click"
    TRACK_FAILED = "track_failed"
    BOARD_REJECTED = "board_rejected"
    NO_SUBMIT_REQUEST = "no_submit_request"
    UNRESOLVED = "unresolved_questions"
    UNCONFIRMED_GUESSES = "unconfirmed_guesses"
    POSTING_EXPIRED = "posting_expired"
    MANUAL_BOARD = "manual_board"
    NO_DRIVER = "no_driver"
    ALREADY_SENT = "already_sent"
    COOLDOWN = "cooldown"
    COMPANY_CAP = "company_cap"
    BAD_OVERRIDES = "bad_overrides"
    BUILD_ERROR = "build_error"
    CAPTCHA_HEADLESS = "captcha_headless"
    BROWSER_ERROR = "browser_error"
    FILL_FAILED = "fill_failed"
    SUBMIT_GUARD = "submit_guard"
    UNKNOWN = "unknown"


CATEGORY_ORDER = (
    "submitted_untracked",
    "submitted",
    "submitted_unconfirmed",
    "rejected",
    "unsent",
    "parked",
    "ready",
    "manual",
    "skipped",
    "failed",
    "expired",
)
CATEGORY_TITLE = {
    "submitted": "Submitted",
    "submitted_unconfirmed": "Submitted (no confirmation seen — verify by hand)",
    "submitted_untracked": "SUBMITTED BUT NOT TRACKED — fix state.yaml by hand",
    "rejected": "Rejected by the board (not tracked — read its reason)",
    "unsent": "Clicked, but no submit request fired (not tracked — verify by hand)",
    "parked": "Parked",
    "ready": "Ready (not submitted)",
    "manual": "Manual-apply (no submit path for this board)",
    "skipped": "Skipped (already sent, company cooldown, or one submission per company per run)",
    "failed": "Failed",
    "expired": "Expired postings",
}
# The sheet's coarser outcome per category.
SHEET_CATEGORY = {c: "submitted" if c.startswith("submitted") else c for c in CATEGORY_ORDER}


def park_kind(row: dict) -> str:
    """Why a question parked, in the words that say what to do next."""
    if row.get("kind"):
        return str(row["kind"])
    if row.get("source") == "export_control" or "export control" in row.get("reason", ""):
        return "new question"
    if "~" in row.get("source", ""):
        return "guess to confirm"
    if row.get("tier") == "C":
        return "new question"
    return "needs a fact"


def role(o: Any) -> dict:
    """One RunOutcome as a JSON role."""
    return {
        "job_id": o.job_id,
        "company": o.company,
        "title": o.title,
        "category": o.category,
        "reason": str(o.reason or ReasonCode.UNKNOWN),
        "detail": o.detail,
        "fields_parked": [{**row, "kind": park_kind(row)} for row in o.unmapped],
        "from_bank": o.from_bank,
        "learned": list(o.learned),
        "evidence": str(o.evidence) if o.evidence else None,
        "at": o.at or None,
    }


def totals(roles: list[dict]) -> dict:
    return {
        "parked": sum(1 for r in roles if r["category"] == "parked"),
        "bank": sum(r.get("from_bank", 0) for r in roles),
        "learned": sum(len(r.get("learned") or ()) for r in roles),
        "roles": len(roles),
    }


def build(outcomes: Iterable[Any], started_at: str, updated_at: str | None) -> dict:
    roles = [role(o) for o in outcomes]
    return {
        "schema": SCHEMA,
        "source": "run",
        "started_at": started_at,
        "updated_at": updated_at,
        "totals": totals(roles),
        "roles": roles,
    }


def role_lines(r: dict) -> list[str]:
    """A role's lines in the markdown report."""
    who = f"{r.get('company', '')} — {r.get('title', '')}".strip(" —") or r["job_id"]
    lines = [f"- `{r['job_id']}` {who}"]
    if r.get("detail"):
        lines.append(f"  - {r['detail']}")
    for row in r.get("fields_parked") or ():
        lines.append(f"  - `{row['id']}` [{park_kind(row)}] {row['label']}")
        lines.append(f"    - {row.get('reason', '')}")
        if row.get("options"):
            lines.append(f"    - offers: {', '.join(row['options'])}")
        if row.get("description"):
            lines.append(f"    - note: {row['description']}")
    learned = r.get("learned") or []
    if r.get("from_bank") or learned:
        lines.append(
            f"  - answered from the bank: {r.get('from_bank', 0)}; learned: {len(learned)}"
        )
    for change in learned:
        lines.append(f"    - {change}")
    if r.get("evidence"):
        lines.append(f"  - what the board showed after the click: `{r['evidence']}`")
    return lines


def first_detail(r: dict) -> str:
    """The first non-empty `  - ` line of the role in the report."""
    for line in "\n".join(role_lines(r)).splitlines()[1:]:
        if line.startswith("  - ") and line[4:].strip():
            return line[4:].strip()
    return ""


def ordered(roles: list[dict]) -> list[dict]:
    """Roles in report order: by category, then as they ran."""
    rank = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    return sorted(roles, key=lambda r: rank.get(r["category"], len(rank)))


def render(record: dict, trend: list[str]) -> str:
    roles = record["roles"]
    lines = [
        f"# apply run — {record['started_at']}",
        "",
        f"{len(roles)} role(s) attempted.",
        "",
    ]
    for category in CATEGORY_ORDER:
        rows = [r for r in roles if r["category"] == category]
        if not rows:
            continue
        lines.append(f"## {CATEGORY_TITLE[category]} ({len(rows)})")
        lines.append("")
        for r in rows:
            lines.extend(role_lines(r))
        lines.append("")
    t = record["totals"] or totals(roles)
    lines.append(
        f"**parked {t['parked']} · answered from bank {t['bank']} · learned {t['learned']} new**"
    )
    if trend:
        lines.append("")
        lines.append("Recent runs:")
        lines.extend(trend)
    lines.append(
        f"<!-- totals parked={t['parked']} bank={t['bank']} "
        f"learned={t['learned']} roles={t['roles']} -->"
    )
    return "\n".join(lines)


def write(record: dict, path: Path) -> Path:
    """Write atomically, so a reader never sees half a run."""
    atomic_write_text(path, json.dumps(record, indent=2, ensure_ascii=False), mkdir=True)
    return path


def load(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("roles"), list):
        return None
    schemas.check(data.get("schema"), SCHEMA, path)
    return data


def _order_key(stem: str) -> str:
    # The order the .md reports always sorted in, so "<ts>.2" keeps its place.
    return f"{stem}.md"


def records(runs_dir: Path) -> list[tuple[str, dict]]:
    """(stem, record) per run, oldest first. A report with no JSON yet is read
    by the one-time backfill's parser until the backfill has run."""
    if not runs_dir.is_dir():
        return []
    stems = {p.stem: p for p in runs_dir.glob("*.json")}
    out = []
    for stem in sorted(stems.keys() | {p.stem for p in runs_dir.glob("*.md")}, key=_order_key):
        rec = load(stems[stem]) if stem in stems else None
        if rec is None and (runs_dir / f"{stem}.md").is_file():
            from src.apply import run_backfill

            rec = run_backfill.parse_report(runs_dir / f"{stem}.md")
        if rec is not None:
            out.append((stem, rec))
    return out


def outcomes_from(recs: Iterable[tuple[str, dict]]) -> dict[str, tuple[str, str, str]]:
    """job_id -> (sheet category, first detail line, report stem), newest run
    naming each role winning."""
    out: dict[str, tuple[str, str, str]] = {}
    for stem, rec in recs:
        for r in ordered(rec["roles"]):
            category = SHEET_CATEGORY.get(r["category"], "")
            if category:
                out[r["job_id"]] = (category, first_detail(r), stem)
    return out


def trend(runs_dir: Path, limit: int = 10) -> list[str]:
    """One line per recent run that recorded its totals, oldest first."""
    if not runs_dir.is_dir():
        return []
    rows = []
    for path in sorted(runs_dir.glob("*.json"), key=lambda p: _order_key(p.stem))[-limit:]:
        rec = load(path)
        t = (rec or {}).get("totals")
        if t:
            rows.append(
                f"  {path.stem}: parked {t['parked']} of {t['roles']} · "
                f"from bank {t['bank']} · learned {t['learned']}"
            )
    return rows
