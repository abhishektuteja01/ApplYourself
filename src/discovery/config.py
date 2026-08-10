from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
import yaml
from src import verticals
from src import paths

log = logging.getLogger(__name__)

REPO_ROOT = paths.REPO_ROOT
DEFAULT_CONFIG_PATH = REPO_ROOT / "profile" / "discovery.yaml"
_SCHEMA_VERSION = 1

@dataclass
class SourceConfig:
    enabled: bool
    pacing_seconds: float

@dataclass
class LocationAllowlist:
    countries: list[str] = field(default_factory=list)
    states: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)

@dataclass
class DiscoveryConfig:
    schema_version: int = _SCHEMA_VERSION
    deadline_hours: float = 6.0
    location_allowlist: LocationAllowlist = field(default_factory=lambda: LocationAllowlist(["United States"]))
    sources: dict[str, SourceConfig] = field(default_factory=lambda: {
        "linkedin": SourceConfig(True, 3.0),
        "indeed": SourceConfig(True, 2.0),
        "greenhouse": SourceConfig(True, 1.0),
        "lever": SourceConfig(True, 1.0),
        "ashby": SourceConfig(True, 2.0),
        "workday": SourceConfig(True, 2.0),
    })
    raw_retention_days: int = 30

def load_config(path: Path | None = None) -> DiscoveryConfig:
    try:
        verticals.get_config()
    except FileNotFoundError as e:
        # Raise, don't sys.exit: a library function must let a non-CLI caller
        # handle this. orchestrator.main turns it into the one-line CLI message.
        raise FileNotFoundError(f"profile/verticals.yaml missing: {e}") from e

    p = path or DEFAULT_CONFIG_PATH
    if not p.exists():
        log.info("profile/discovery.yaml not found, using defaults")
        return DiscoveryConfig()

    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"Malformed YAML in {p}: {e}") from e

    if not isinstance(data, dict):
        raise ValueError("Config must be a dictionary")

    cfg = DiscoveryConfig()

    version = data.get("schema_version", _SCHEMA_VERSION)
    if version != _SCHEMA_VERSION:
        raise ValueError(f"{p}: schema_version must be {_SCHEMA_VERSION}, got {version!r}")
    cfg.schema_version = _SCHEMA_VERSION

    if "sources" in data:
        allowed_sources = {"linkedin", "indeed", "greenhouse", "lever", "ashby", "workday"}
        for k, v in data["sources"].items():
            if k not in allowed_sources:
                raise ValueError(f"Unknown source key: {k}")
            cfg.sources[k] = SourceConfig(
                enabled=bool(v.get("enabled", True)),
                pacing_seconds=float(v.get("pacing_seconds", 1.0))
            )

    if "location_allowlist" in data:
        loc = data["location_allowlist"]
        cfg.location_allowlist = LocationAllowlist(
            countries=loc.get("countries", []),
            states=loc.get("states", []),
            cities=loc.get("cities", []),
        )

    cfg.deadline_hours = float(data.get("deadline_hours", cfg.deadline_hours))
    cfg.raw_retention_days = int(data.get("raw_retention_days", cfg.raw_retention_days))

    return cfg
