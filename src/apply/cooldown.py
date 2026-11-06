"""The company cooldown: no second application to a company within
`company_cooldown_days` (profile/settings.yaml) of the last one.

Every application counts: an `applied` entry in any role's state_history (by
hand or by the submitter) and every submit-ledger line that may have reached
the board. Companies match on `normalize_company`, the name job_id hashes.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path

from src import state_io
from src.apply import ledger
from src.shared import clock, paths
from src.shared.identity import normalize_company


def cooldown_days(data: dict | None = None) -> int:
    from src.shared.config_schema import setting

    return int(setting("company_cooldown_days", 2, data))


def _at(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except ValueError:
        return None


def _keep(latest: dict[str, datetime], company: object, at: datetime | None) -> None:
    key = normalize_company(str(company or ""))
    if key and at is not None and (key not in latest or at > latest[key]):
        latest[key] = at


def ledger_applications(
    recs: Iterable[dict], latest: dict[str, datetime] | None = None
) -> dict[str, datetime]:
    """normalized company -> latest ledger line that may have reached a board."""
    latest = {} if latest is None else latest
    for rec in recs:
        if ledger.verdict_of(rec) in (ledger.Verdict.SENT, ledger.Verdict.UNKNOWN):
            _keep(latest, rec.get("company"), _at(rec.get("at")))
    return latest


def last_applications(
    pipeline_dir: Path | None = None,
    ledger_path: Path | None = None,
    exclude: str = "",
) -> dict[str, datetime]:
    """normalized company -> the latest application to it, from every
    state.yaml's `applied` entries and the submit ledger, leaving out the
    role `exclude`."""
    latest: dict[str, datetime] = {}
    for data in state_io.iter_states(pipeline_dir or paths.PIPELINE):
        if exclude and data.get("job_id") == exclude:
            continue
        for entry in data.get("state_history") or []:
            if isinstance(entry, dict) and entry.get("state") == "applied":
                _keep(latest, data.get("company"), _at(entry.get("at")))
    if ledger_path is not None:
        recs = ledger.records(ledger_path)
        ledger_applications((r for r in recs if not exclude or r.get("job_id") != exclude), latest)
    return latest


def until(
    company: str,
    latest: dict[str, datetime],
    days: int | None = None,
    now: datetime | None = None,
) -> date | None:
    """The day the cooldown on this company ends, or None when it is not on one."""
    at = latest.get(normalize_company(company))
    if at is None:
        return None
    end = at + timedelta(days=cooldown_days() if days is None else days)
    return end.date() if end > (now or clock.now()) else None


def note(day: date) -> str:
    return f"cooldown until {day.isoformat()}"
