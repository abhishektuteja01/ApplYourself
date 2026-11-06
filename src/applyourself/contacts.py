"""Who you know at a company: reads profile/contacts.yaml (R7: no LLM).

A missing file means no contacts. Companies match after normalize_company, the
same cleanup job_id uses, so "Apple Inc." finds a contact filed under "Apple".
"""

from __future__ import annotations

from pathlib import Path

import yaml

from applyourself.shared import paths, schemas
from applyourself.shared.config_schema import ContactsFile, validate
from applyourself.shared.identity import normalize_company


def referrals_by_company(path: Path | None = None) -> dict[str, list[str]]:
    """{normalized company: [names]} for every `relationship: referral` entry."""
    path = path or paths.CONTACTS
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    validate(ContactsFile, data, path)
    schemas.check(data.get("schema_version"), schemas.CONTACTS_SCHEMA_VERSION, path)
    out: dict[str, list[str]] = {}
    for c in data.get("contacts") or []:
        key = normalize_company(c.get("company"))
        if key and c.get("name") and c.get("relationship") == "referral":
            out.setdefault(key, []).append(c["name"])
    return out


def referral_note(company: str, index: dict[str, list[str]]) -> str:
    """'referral: A, B' when you know someone at company, else ''."""
    names = index.get(normalize_company(company))
    return f"referral: {', '.join(names)}" if names else ""
