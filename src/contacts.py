"""Who you know at a company: reads profile/contacts.yaml (R7: no LLM).

A missing file means no contacts. Companies match after normalize_company, the
same cleanup job_id uses, so "Apple Inc." finds a contact filed under "Apple".
"""

from __future__ import annotations

from pathlib import Path

import yaml

from src.shared import paths
from src.shared.config_keys import check_keys
from src.shared.identity import normalize_company

CONTACTS_PATH = paths.PROFILE / "contacts.yaml"
_ENTRY_KEYS = {
    "id",
    "name",
    "company",
    "role",
    "relationship",
    "channel",
    "email",
    "phone",
    "linkedin",
    "shared_affiliation",
    "tags",
    "notes",
}
_RELATIONSHIPS = {"recruiter", "referral", "alumni", "other", None}


def referrals_by_company(path: Path = CONTACTS_PATH) -> dict[str, list[str]]:
    """{normalized company: [names]} for every `relationship: referral` entry."""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    check_keys(data, {"schema_version", "contacts"}, "top level", path)
    out: dict[str, list[str]] = {}
    for i, c in enumerate(data.get("contacts") or []):
        check_keys(c, _ENTRY_KEYS, f"contacts[{i}]", path)
        if c.get("relationship") not in _RELATIONSHIPS:
            raise ValueError(
                f"{path}: contacts[{i}].relationship must be one of "
                f"{', '.join(sorted(r for r in _RELATIONSHIPS if r))}"
            )
        key = normalize_company(c.get("company"))
        if key and c.get("name") and c.get("relationship") == "referral":
            out.setdefault(key, []).append(c["name"])
    return out


def referral_note(company: str, index: dict[str, list[str]]) -> str:
    """'referral: A, B' when you know someone at company, else ''."""
    names = index.get(normalize_company(company))
    return f"referral: {', '.join(names)}" if names else ""
