"""The night's shipped settings defaults: profile/settings_defaults.yaml when
you made one (it replaces the whole file), else
data/defaults/settings_defaults.yaml. A key in profile/settings.yaml wins.
No LLM (R7)."""

from __future__ import annotations

from typing import Any

import yaml

from applyourself.shared import config_schema, paths

NAME = "settings_defaults.yaml"


def load() -> dict[str, Any]:
    """The defaults, every key checked. A missing or malformed file raises ValueError."""
    p = next(
        (q for q in (paths.SETTINGS_DEFAULTS, paths.default(NAME)) if q.is_file()),
        paths.default(NAME),
    )
    if not p.is_file():
        raise ValueError(f"{p} is missing")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    config_schema.validate(config_schema.SettingsDefaults, data, p)
    return data


def value(key: str) -> Any:
    """profile/settings.yaml's `key`, else the shipped default."""
    return config_schema.setting(key, load()[key])
