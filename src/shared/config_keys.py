"""Unknown-key check for the profile/*.yaml loaders, so a typo'd key fails
loud instead of silently reading as empty (R7: no LLM)."""
from __future__ import annotations

import difflib
from pathlib import Path

import yaml

AUTOMATION_KEYS = frozenset({
    "schema_version", "score_threshold", "table_path", "tailor_parallel",
    "tailor_stagger_seconds", "retries", "max_budget_usd_per_call",
    "auto_add_synonyms", "unlock_max_open", "summary_email",
})


def check_keys(raw: dict, allowed, where: str, path) -> None:
    """Raise ValueError naming each key of `raw` not in `allowed`, with the
    closest valid key when one is near."""
    allowed = sorted(allowed)
    bad = []
    for key in sorted(set(raw) - set(allowed), key=str):
        close = difflib.get_close_matches(str(key), allowed, n=1)
        bad.append(f"{key!r}" + (f" (did you mean {close[0]!r}?)" if close else ""))
    if bad:
        raise ValueError(f"{path}: {where}: unknown key {', '.join(bad)}; "
                         f"valid keys: {', '.join(allowed)}")


def load_automation(path: Path) -> dict:
    """profile/automation.yaml as a dict, every key checked."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    check_keys(data, AUTOMATION_KEYS, "top level", path)
    return data
