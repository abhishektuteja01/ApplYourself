"""`profile/discovery.yaml`: every discovery setting, loaded and checked.

`uv run discovery-check` prints what the file resolves to and every problem
with it; `discover` runs the same check before it starts. A key the loader
does not know is reported by name, never ignored.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src import paths, verticals
from src.ats_registry import ATS_SOURCE_NAMES

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = paths.REPO_ROOT / "profile" / "discovery.yaml"
SCHEMA_VERSION = 2

# The search lanes (jobspy); every other source is an ATS board lane.
JOBSPY_SOURCES = ("linkedin", "indeed")
SOURCE_NAMES = JOBSPY_SOURCES + ATS_SOURCE_NAMES

_TOP_LEVEL_KEYS = frozenset({
    "schema_version", "deadline_hours", "location_allowlist", "search_locations", "sources",
    "live_days", "search_max_age_days", "retention", "forget_after_days", "raw_keep_days",
    "log_keep_days", "dead_every_days",
})
_ALLOWLIST_KEYS = frozenset({"countries"})
_RETENTION_KEYS = frozenset({"score_threshold", "high_days", "low_days"})
_SOURCE_KEYS = frozenset({
    "enabled", "pacing_seconds", "page_delay_seconds", "page_delay_band_seconds",
    "remote_queries", "abroad", "every_days", "max_pages", "hours_old", "results_wanted",
})
_ABROAD_KEYS = frozenset({"countries", "verticals"})
# Keys a schema_version 1 file may still carry, and what replaced them.
_RETIRED_KEYS = {
    "raw_retention_days": "raw_keep_days", "board_max_age_days": "(nothing; boards are live while listed)",
    "cold_rotation_runs": "every_days", "cadence": "every_days",
    "pacing_spread": "(nothing)", "detail_reuse_days": "(nothing; the store keeps every JD)",
    "states": "(nothing)", "cities": "(nothing)", "continents": "countries",
}

# With no `search_locations`, the jobspy lanes query the allowlist's
# countries; past this many, the query count outruns the deadline.
MAX_SEARCH_LOCATIONS = 5

# The pacing floor config cannot go below, per source.
DEFAULT_MIN_PACING_SECONDS = 1.0
MIN_PACING_SECONDS = {"greenhouse": 0.5, "linkedin": 0.5, "indeed": 0.5, "workday": 0.5}
# jobspy's own page delay, and the floor under it.
DEFAULT_PAGE_DELAY_SECONDS = 3.0
DEFAULT_PAGE_DELAY_BAND_SECONDS = 4.0
MIN_PAGE_DELAY_SECONDS = 0.5


def pacing_floor(source_name: str) -> float:
    return MIN_PACING_SECONDS.get(source_name, DEFAULT_MIN_PACING_SECONDS)


@dataclass
class SourceConfig:
    enabled: bool = True
    pacing_seconds: float = 1.0
    # jobspy lanes: page delay, the remote copy of each query, the search
    # window, the result cap, and the `abroad` block.
    page_delay_seconds: float = DEFAULT_PAGE_DELAY_SECONDS
    page_delay_band_seconds: float = DEFAULT_PAGE_DELAY_BAND_SECONDS
    remote_queries: bool = True
    hours_old: int = 336
    results_wanted: int = 100
    abroad_countries: list[str] = field(default_factory=list)
    abroad_verticals: list[str] = field(default_factory=list)
    # Board lanes: days between checks of one board. Workday: pages per term.
    every_days: int = 1
    max_pages: int = 6


@dataclass
class LocationAllowlist:
    countries: list[str] = field(default_factory=list)

    def effective_countries(self) -> set[str]:
        from src.discovery.location import canonical_country
        return {canonical_country(c) or c for c in self.countries}

    def unresolved(self) -> list[str]:
        from src.discovery.location import canonical_country
        return [f"location_allowlist.countries: {c!r} is not a country name"
                for c in self.countries if canonical_country(c) is None]


def _default_sources() -> dict[str, SourceConfig]:
    return {"linkedin": SourceConfig(pacing_seconds=3.0), "indeed": SourceConfig(pacing_seconds=2.0),
            "greenhouse": SourceConfig(), "lever": SourceConfig(),
            "ashby": SourceConfig(pacing_seconds=2.0), "workday": SourceConfig(pacing_seconds=2.0)}


@dataclass
class DiscoveryConfig:
    deadline_hours: float = 4.0
    location_allowlist: LocationAllowlist = field(
        default_factory=lambda: LocationAllowlist(["United States"]))
    # Where the jobspy lanes search, as against what the view accepts.
    search_locations: list[str] = field(default_factory=list)
    sources: dict[str, SourceConfig] = field(default_factory=_default_sources)
    # The live view: seen within `live_days`; a search row also posted within
    # `search_max_age_days`. Retention counts from first_seen, by score tier.
    live_days: int = 14
    search_max_age_days: int = 14
    retention_score_threshold: float = 80.0
    retention_high_days: int = 60
    retention_low_days: int = 15
    forget_after_days: int = 120
    raw_keep_days: int = 3
    log_keep_days: int = 30
    dead_every_days: int = 14
    unknown_keys: list[str] = field(default_factory=list)

    def abroad_lanes(self) -> dict[str, set[str]]:
        """{country (lowercased): lanes allowed there}. A country absent here
        allows every lane."""
        lanes: dict[str, set[str]] = {}
        for src in self.sources.values():
            for c in src.abroad_countries:
                lanes.setdefault(c.lower(), set()).update(src.abroad_verticals)
        return lanes

    def effective_search_locations(self) -> list[str]:
        if self.search_locations:
            return sorted(self.search_locations)
        return sorted(self.location_allowlist.effective_countries()) or ["United States"]

    def validate(self) -> list[str]:
        """Every problem, in plain words. Empty means usable."""
        problems = []
        for key in self.unknown_keys:
            leaf = key.rsplit(".", 1)[-1]
            hint = f" (removed; use {_RETIRED_KEYS[leaf]})" if leaf in _RETIRED_KEYS else ""
            problems.append(f"unknown config key: {key}{hint}")
        problems += self.location_allowlist.unresolved()
        if not self.search_locations:
            n = len(self.effective_search_locations())
            if n > MAX_SEARCH_LOCATIONS:
                problems.append(
                    f"location_allowlist expands to {n} search locations (max "
                    f"{MAX_SEARCH_LOCATIONS} without an explicit search_locations). Set "
                    f"search_locations to the countries to query.")
        problems += self._abroad_problems()
        board_every = [s.every_days for n, s in self.sources.items()
                       if n not in JOBSPY_SOURCES and s.enabled]
        if board_every and self.live_days <= max(board_every) + 1:
            problems.append(
                f"live_days ({self.live_days}) must be above every_days + 1 "
                f"({max(board_every) + 1}), or a board's jobs leave the view between checks")
        return problems

    def _abroad_problems(self) -> list[str]:
        from src.discovery.location import canonical_country

        problems = []
        allowed = {c.lower() for c in self.location_allowlist.effective_countries()}
        for name, src in self.sources.items():
            if not (src.abroad_countries or src.abroad_verticals):
                continue
            where = f"sources.{name}.abroad"
            if name not in JOBSPY_SOURCES:
                problems.append(f"{where}: only {sorted(JOBSPY_SOURCES)} read it")
                continue
            if not src.abroad_countries or not src.abroad_verticals:
                problems.append(f"{where}: needs both countries and verticals")
            for c in src.abroad_countries:
                canonical = canonical_country(c)
                if canonical is None:
                    problems.append(f"{where}.countries: {c!r} is not a country name")
                elif allowed and canonical.lower() not in allowed:
                    problems.append(f"{where}.countries: {c!r} is not in location_allowlist, "
                                    f"so the view would drop every row it finds")
            lanes = verticals.get_config().verticals
            for v in src.abroad_verticals:
                if v not in lanes:
                    problems.append(f"{where}.verticals: {v!r} is not a vertical")
                elif not lanes[v].abroad_terms:
                    problems.append(f"{where}.verticals: {v!r} has no abroad_terms in "
                                    f"profile/verticals.yaml")
        return problems


def _int(value, where: str, floor: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < floor:
        raise ValueError(f"{where} must be an integer >= {floor}, got {value!r}")
    return value


def _source(name: str, v: dict, p: Path, cfg: DiscoveryConfig) -> SourceConfig:
    where = f"{p}: sources.{name}"
    cfg.unknown_keys += [f"sources.{name}.{k}" for k in v if k not in _SOURCE_KEYS]
    base = cfg.sources.get(name, SourceConfig())
    out = SourceConfig(
        enabled=bool(v.get("enabled", True)),
        pacing_seconds=float(v.get("pacing_seconds", base.pacing_seconds)),
        page_delay_seconds=float(v.get("page_delay_seconds", DEFAULT_PAGE_DELAY_SECONDS)),
        page_delay_band_seconds=float(v.get("page_delay_band_seconds",
                                            DEFAULT_PAGE_DELAY_BAND_SECONDS)),
        remote_queries=v.get("remote_queries", True),
        every_days=_int(v.get("every_days", 1), f"{where}.every_days", 1),
        max_pages=_int(v.get("max_pages", 6), f"{where}.max_pages", 1),
        hours_old=_int(v.get("hours_old", 336), f"{where}.hours_old", 1),
        results_wanted=_int(v.get("results_wanted", 100), f"{where}.results_wanted", 1),
    )
    if out.page_delay_seconds < MIN_PAGE_DELAY_SECONDS:
        raise ValueError(f"{where}.page_delay_seconds must be >= {MIN_PAGE_DELAY_SECONDS}, "
                         f"got {out.page_delay_seconds}")
    if out.page_delay_band_seconds < 0:
        raise ValueError(f"{where}.page_delay_band_seconds must be >= 0")
    if not isinstance(out.remote_queries, bool):
        raise ValueError(f"{where}.remote_queries must be true or false, got {out.remote_queries!r}")
    abroad = v.get("abroad") or {}
    if not isinstance(abroad, dict):
        raise ValueError(f"{where}.abroad must be a mapping")
    cfg.unknown_keys += [f"sources.{name}.abroad.{k}" for k in abroad if k not in _ABROAD_KEYS]
    for key in _ABROAD_KEYS:
        values = abroad.get(key) or []
        if not isinstance(values, list) or not all(isinstance(x, str) and x.strip() for x in values):
            raise ValueError(f"{where}.abroad.{key} must be a list of names")
        setattr(out, f"abroad_{key}", [x.strip() for x in values])
    return out


def load_config(path: Path | None = None) -> DiscoveryConfig:
    try:
        verticals.get_config()
    except FileNotFoundError as e:
        raise FileNotFoundError(f"profile/verticals.yaml missing: {e}") from e
    p = Path(path or DEFAULT_CONFIG_PATH)
    if not p.exists():
        log.info("profile/discovery.yaml not found, using defaults")
        return DiscoveryConfig()
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"Malformed YAML in {p}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("Config must be a dictionary")
    version = data.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise ValueError(f"{p}: schema_version must be {SCHEMA_VERSION}, got {version!r}")

    cfg = DiscoveryConfig()
    cfg.unknown_keys = [k for k in data if k not in _TOP_LEVEL_KEYS]
    for name, v in (data.get("sources") or {}).items():
        if name not in SOURCE_NAMES:
            raise ValueError(f"Unknown source key: {name}")
        cfg.sources[name] = _source(name, v or {}, p, cfg)

    allow = data.get("location_allowlist") or {}
    if not isinstance(allow, dict):
        raise ValueError(f"{p}: location_allowlist must be a mapping")
    cfg.unknown_keys += [f"location_allowlist.{k}" for k in allow if k not in _ALLOWLIST_KEYS]
    if "countries" in allow:
        cfg.location_allowlist = LocationAllowlist(list(allow.get("countries") or []))

    if "search_locations" in data:
        raw = data["search_locations"] or []
        if not isinstance(raw, list):
            raise ValueError(f"{p}: search_locations must be a list of location names")
        cfg.search_locations = [str(s).strip() for s in raw if str(s).strip()]

    cfg.deadline_hours = float(data.get("deadline_hours", cfg.deadline_hours))
    for key in ("live_days", "search_max_age_days", "forget_after_days", "raw_keep_days",
                "log_keep_days", "dead_every_days"):
        setattr(cfg, key, _int(data.get(key, getattr(cfg, key)), f"{p}: {key}", 1))
    retention = data.get("retention") or {}
    if not isinstance(retention, dict):
        raise ValueError(f"{p}: retention must be a mapping")
    cfg.unknown_keys += [f"retention.{k}" for k in retention if k not in _RETENTION_KEYS]
    cfg.retention_score_threshold = float(retention.get("score_threshold",
                                                        cfg.retention_score_threshold))
    for key in ("high_days", "low_days"):
        setattr(cfg, f"retention_{key}", _int(retention.get(key, getattr(cfg, f"retention_{key}")),
                                               f"{p}: retention.{key}", 1))
    return cfg


def main() -> int:
    """`discovery-check`: what discovery.yaml resolves to, and every problem."""
    try:
        cfg = load_config()
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"countries: {', '.join(sorted(cfg.location_allowlist.effective_countries())) or '(any)'}")
    print(f"search in: {', '.join(cfg.effective_search_locations())}"
          + ("" if cfg.search_locations else "  (from the allowlist)"))

    def line(name: str, s: SourceConfig) -> str:
        bits = [f"{s.pacing_seconds:g}s"]
        if name in JOBSPY_SOURCES:
            if not s.remote_queries:
                bits.append("no remote queries")
            if s.abroad_countries:
                bits.append(f"abroad {'/'.join(s.abroad_verticals)} in {len(s.abroad_countries)} countries")
        else:
            bits.append(f"every {s.every_days}d")
        return f"{name} ({', '.join(bits)})"

    enabled = [(n, s) for n, s in cfg.sources.items() if s.enabled]
    print("sources:   " + (", ".join(line(n, s) for n, s in enabled) or "(none enabled)"))
    print(f"live view: seen within {cfg.live_days}d; search rows posted within "
          f"{cfg.search_max_age_days}d; keep {cfg.retention_high_days}d at fit >= "
          f"{cfg.retention_score_threshold:g}, else {cfg.retention_low_days}d")
    print(f"deadline_hours: {cfg.deadline_hours:g}")
    problems = cfg.validate()
    if problems:
        print("\nERROR: discovery config problems:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
