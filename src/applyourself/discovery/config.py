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

from applyourself.shared import paths, verticals
from applyourself.shared.job_boards import ATS_SOURCE_NAMES
from applyourself.shared.logs import setup_logging

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

# Every lane and its kind: the search lanes (jobspy), the bulk-file feed, and
# the ATS board lanes (from job_boards, which /apply shares).
LANE_KINDS: dict[str, str] = {
    "linkedin": "search",
    "indeed": "search",
    "stapply": "feed",
    **{name: "board" for name in ATS_SOURCE_NAMES},
}
JOBSPY_SOURCES = tuple(n for n, k in LANE_KINDS.items() if k == "search")
FEED_SOURCES = tuple(n for n, k in LANE_KINDS.items() if k == "feed")
SOURCE_NAMES = tuple(LANE_KINDS)
# Default pacing where a lane differs from SourceConfig's.
_DEFAULT_PACING = {"linkedin": 3.0, "indeed": 2.0, "ashby": 2.0, "workday": 2.0}

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "deadline_hours",
        "location_allowlist",
        "search_locations",
        "abroad",
        "sources",
        "live_days",
        "search_max_age_days",
        "retention",
        "retention_from",
        "forget_after_days",
        "raw_keep_days",
        "log_keep_days",
        "dead_every_days",
    }
)
_ALLOWLIST_KEYS = frozenset({"countries"})
_RETENTION_KEYS = frozenset({"high_days", "low_days"})
_SOURCE_KEYS = frozenset(
    {
        "enabled",
        "pacing_seconds",
        "page_delay_seconds",
        "page_delay_band_seconds",
        "remote_queries",
        "every_days",
        "max_pages",
        "hours_old",
        "results_wanted",
        "max_snapshot_age_hours",
        "page_read_max_failures",
        "page_read_retry_days",
        "companies",
        "terms_per_lane",
    }
)
_ABROAD_KEYS = frozenset({"lanes", "sources"})
_COMPANIES_KEYS = frozenset({"terms", "ids"})
# Keys a schema_version 1 file may still carry, and what replaced them.
_RETIRED_KEYS = {
    "raw_retention_days": "raw_keep_days",
    "board_max_age_days": "(nothing; boards are live while listed)",
    "cold_rotation_runs": "every_days",
    "cadence": "every_days",
    "pacing_spread": "(nothing)",
    "detail_reuse_days": "(nothing; the store keeps every JD)",
    "states": "(nothing)",
    "cities": "(nothing)",
    "continents": "countries",
    "score_threshold": "verticals.yaml score.cutoff (defaults: or the lane's)",
    "abroad": "the top-level abroad: {lanes, sources}; its countries are "
    "application_answers.yaml work_authorization.countries",
}

RETENTION_FROM = ("first", "newest")
# The board lanes that search server-side, and how many of each lane's first
# search terms they use.
TERMS_PER_LANE_SOURCES = ("workday", "eightfold")
DEFAULT_TERMS_PER_LANE = 3

# With no `search_locations`, the jobspy lanes query the allowlist's
# countries; past this many, the query count outruns the deadline.
MAX_SEARCH_LOCATIONS = 5

# The most results one query can return. jobspy 1.1.82 reads LinkedIn 10 a page,
# steps `start` by the results so far and stops at start=1000: 14 pages.
MAX_RESULTS_WANTED = {"linkedin": 140}

# The pacing floor config cannot go below, per source.
DEFAULT_MIN_PACING_SECONDS = 1.0
MIN_PACING_SECONDS = {"greenhouse": 0.5, "linkedin": 0.5, "indeed": 0.5, "workday": 0.5}
# jobspy's own page delay, and the floor under it.
DEFAULT_PAGE_DELAY_SECONDS = 3.0
DEFAULT_PAGE_DELAY_BAND_SECONDS = 4.0
MIN_PAGE_DELAY_SECONDS = 0.5


def pacing_floor(source_name: str) -> float:
    return MIN_PACING_SECONDS.get(source_name, DEFAULT_MIN_PACING_SECONDS)


def pacing_for(cfg, source_name: str) -> float:
    """Seconds between one source's requests: its `pacing_seconds`, never below its floor."""
    return max(pacing_floor(source_name), cfg.sources[source_name].pacing_seconds)


@dataclass
class SourceConfig:
    enabled: bool = True
    pacing_seconds: float = 1.0
    # jobspy lanes: page delay, the remote copy of each query, the search
    # window, the result cap, and what the top-level `abroad` block gives it.
    page_delay_seconds: float = DEFAULT_PAGE_DELAY_SECONDS
    page_delay_band_seconds: float = DEFAULT_PAGE_DELAY_BAND_SECONDS
    remote_queries: bool = True
    hours_old: int = 336
    results_wanted: int = 100
    abroad_countries: list[str] = field(default_factory=list)
    abroad_verticals: list[str] = field(default_factory=list)
    # LinkedIn: each term searched within each company's LinkedIn id.
    company_terms: list[str] = field(default_factory=list)
    company_ids: dict[str, int] = field(default_factory=dict)
    # Board lanes: days between checks of one board. Workday/Eightfold: pages
    # per term, and how many of each lane's first search terms they search.
    every_days: int = 1
    max_pages: int = 6
    terms_per_lane: int = DEFAULT_TERMS_PER_LANE
    # Feed lanes: a snapshot older than this is skipped, not applied. A
    # posting whose JD read failed this many nights is read again only every
    # `page_read_retry_days`.
    max_snapshot_age_hours: int = 36
    page_read_max_failures: int = 3
    page_read_retry_days: int = 7


@dataclass
class LocationAllowlist:
    countries: list[str] = field(default_factory=list)

    def effective_countries(self) -> set[str]:
        from applyourself.shared.countries import canonical_country

        return {canonical_country(c) or c for c in self.countries}

    def unresolved(self) -> list[str]:
        from applyourself.shared.countries import canonical_country

        return [
            f"location_allowlist.countries: {c!r} is not a country name"
            for c in self.countries
            if canonical_country(c) is None
        ]


def _default_sources() -> dict[str, SourceConfig]:
    return {
        n: SourceConfig(pacing_seconds=_DEFAULT_PACING[n])
        if n in _DEFAULT_PACING
        else SourceConfig()
        for n in SOURCE_NAMES
    }


@dataclass
class DiscoveryConfig:
    deadline_hours: float = 4.0
    location_allowlist: LocationAllowlist = field(
        default_factory=lambda: LocationAllowlist(["United States"])
    )
    # Where the jobspy lanes search, as against what the view accepts.
    search_locations: list[str] = field(default_factory=list)
    sources: dict[str, SourceConfig] = field(default_factory=_default_sources)
    # The live view: seen within `live_days`; a search row also posted within
    # `search_max_age_days` when set (None: no posted-age cut). Retention
    # counts from first_seen, by score tier.
    live_days: int = 14
    search_max_age_days: int | None = None
    retention_score_threshold: float = 80.0
    retention_high_days: int = 60
    retention_low_days: int = 15
    # Retention's clock: a job's `first` posting, or its `newest` one that
    # passes the view (a repost restarts it).
    retention_from: str = "first"
    forget_after_days: int = 120
    raw_keep_days: int = 3
    log_keep_days: int = 30
    dead_every_days: int = 14
    # The top-level `abroad` block; its countries are the bank's, minus home.
    abroad_lanes_listed: list[str] = field(default_factory=list)
    abroad_sources: list[str] = field(default_factory=list)
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
                    f"search_locations to the countries to query."
                )
        problems += self._abroad_problems()
        for name, most in MAX_RESULTS_WANTED.items():
            src = self.sources.get(name)
            if src is not None and src.results_wanted > most:
                problems.append(
                    f"sources.{name}.results_wanted ({src.results_wanted}) is above {most}, "
                    f"the most one {name} query returns"
                )
        board_every = [
            s.every_days for n, s in self.sources.items() if n in ATS_SOURCE_NAMES and s.enabled
        ]
        if board_every and self.live_days <= max(board_every) + 1:
            problems.append(
                f"live_days ({self.live_days}) must be above every_days + 1 "
                f"({max(board_every) + 1}), or a board's jobs leave the view between checks"
            )
        return problems

    def _abroad_problems(self) -> list[str]:
        from applyourself.shared.countries import canonical_country

        problems: list[str] = []
        if not (self.abroad_sources or self.abroad_lanes_listed):
            return problems
        allowed = {c.lower() for c in self.location_allowlist.effective_countries()}
        for name in self.abroad_sources:
            if name not in JOBSPY_SOURCES:
                problems.append(f"abroad.sources: {name!r}: only {sorted(JOBSPY_SOURCES)} read it")
        if not self.abroad_sources or not self.abroad_lanes_listed:
            problems.append("abroad: needs both lanes and sources")
        countries = next(
            (s.abroad_countries for n, s in self.sources.items() if n in self.abroad_sources), []
        )
        if self.abroad_sources and not countries:
            problems.append(
                "abroad: application_answers.yaml work_authorization.countries lists "
                "no country but your own (or the bank is missing)"
            )
        for c in countries:
            canonical = canonical_country(c)
            if allowed and canonical is not None and canonical.lower() not in allowed:
                problems.append(
                    f"abroad: {c!r} is not in location_allowlist, "
                    f"so the view would drop every row it finds"
                )
        lanes = verticals.get_config().verticals
        for v in self.abroad_lanes_listed:
            if v not in lanes:
                problems.append(f"abroad.lanes: {v!r} is not a vertical")
            elif not lanes[v].abroad_terms:
                problems.append(
                    f"abroad.lanes: {v!r} has an empty abroad_terms in "
                    f"profile/verticals/{v}/lane.yaml (leave it out to use search_terms)"
                )
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
        page_delay_band_seconds=float(
            v.get("page_delay_band_seconds", DEFAULT_PAGE_DELAY_BAND_SECONDS)
        ),
        remote_queries=v.get("remote_queries", True),
        every_days=_int(v.get("every_days", 1), f"{where}.every_days", 1),
        max_pages=_int(v.get("max_pages", 6), f"{where}.max_pages", 1),
        terms_per_lane=_int(
            v.get("terms_per_lane", DEFAULT_TERMS_PER_LANE), f"{where}.terms_per_lane", 1
        ),
        hours_old=_int(v.get("hours_old", 336), f"{where}.hours_old", 1),
        results_wanted=_int(v.get("results_wanted", 100), f"{where}.results_wanted", 1),
        max_snapshot_age_hours=_int(
            v.get("max_snapshot_age_hours", base.max_snapshot_age_hours),
            f"{where}.max_snapshot_age_hours",
            1,
        ),
        page_read_max_failures=_int(
            v.get("page_read_max_failures", base.page_read_max_failures),
            f"{where}.page_read_max_failures",
            1,
        ),
        page_read_retry_days=_int(
            v.get("page_read_retry_days", base.page_read_retry_days),
            f"{where}.page_read_retry_days",
            1,
        ),
    )
    if out.page_delay_seconds < MIN_PAGE_DELAY_SECONDS:
        raise ValueError(
            f"{where}.page_delay_seconds must be >= {MIN_PAGE_DELAY_SECONDS}, "
            f"got {out.page_delay_seconds}"
        )
    if out.page_delay_band_seconds < 0:
        raise ValueError(f"{where}.page_delay_band_seconds must be >= 0")
    if not isinstance(out.remote_queries, bool):
        raise ValueError(
            f"{where}.remote_queries must be true or false, got {out.remote_queries!r}"
        )
    if "terms_per_lane" in v and name not in TERMS_PER_LANE_SOURCES:
        raise ValueError(
            f"{where}.terms_per_lane is read only by {', '.join(TERMS_PER_LANE_SOURCES)}"
        )
    for key in ("page_read_max_failures", "page_read_retry_days"):
        if key in v and name not in FEED_SOURCES:
            raise ValueError(f"{where}.{key} is read only by {', '.join(FEED_SOURCES)}")
    companies = v.get("companies") or {}
    if not isinstance(companies, dict):
        raise ValueError(f"{where}.companies must be a mapping")
    cfg.unknown_keys += [
        f"sources.{name}.companies.{k}" for k in companies if k not in _COMPANIES_KEYS
    ]
    terms, ids = companies.get("terms") or [], companies.get("ids") or {}
    if not isinstance(terms, list) or not all(isinstance(x, str) and x.strip() for x in terms):
        raise ValueError(f"{where}.companies.terms must be a list of search terms")
    if not isinstance(ids, dict) or not all(
        isinstance(k, str) and k.strip() and isinstance(i, int) and not isinstance(i, bool)
        for k, i in ids.items()
    ):
        raise ValueError(f"{where}.companies.ids must map a company name to its LinkedIn id")
    if ids and name != "linkedin":
        raise ValueError(f"{where}.companies is LinkedIn only")
    if ids and not terms:
        raise ValueError(f"{where}.companies.ids needs companies.terms")
    out.company_terms = [x.strip() for x in terms]
    out.company_ids = {k.strip(): i for k, i in ids.items()}
    return out


def load_config(path: Path | None = None) -> DiscoveryConfig:
    try:
        verticals.get_config()
    except FileNotFoundError as e:
        raise FileNotFoundError(f"profile/verticals.yaml missing: {e}") from e
    p = Path(path or paths.DISCOVERY_CONFIG)
    if not p.exists():
        log.info("profile/discovery.yaml not found, using defaults")
        return _keep_long(_countries(DiscoveryConfig(), p, {}))
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

    abroad = data.get("abroad") or {}
    if not isinstance(abroad, dict):
        raise ValueError(f"{p}: abroad must be a mapping")
    cfg.unknown_keys += [f"abroad.{k}" for k in abroad if k not in _ABROAD_KEYS]
    for key in _ABROAD_KEYS:
        values = abroad.get(key) or []
        if not isinstance(values, list) or not all(
            isinstance(x, str) and x.strip() for x in values
        ):
            raise ValueError(f"{p}: abroad.{key} must be a list of names")
    cfg.abroad_lanes_listed = [x.strip() for x in abroad.get("lanes") or []]
    cfg.abroad_sources = [x.strip() for x in abroad.get("sources") or []]
    _countries(cfg, p, allow)

    if "search_locations" in data:
        raw = data["search_locations"] or []
        if not isinstance(raw, list):
            raise ValueError(f"{p}: search_locations must be a list of location names")
        cfg.search_locations = [str(s).strip() for s in raw if str(s).strip()]

    cfg.deadline_hours = float(data.get("deadline_hours", cfg.deadline_hours))
    if data.get("search_max_age_days") is not None:
        cfg.search_max_age_days = _int(data["search_max_age_days"], f"{p}: search_max_age_days", 1)
    for key in (
        "live_days",
        "forget_after_days",
        "raw_keep_days",
        "log_keep_days",
        "dead_every_days",
    ):
        setattr(cfg, key, _int(data.get(key, getattr(cfg, key)), f"{p}: {key}", 1))
    if "retention_from" in data:
        if data["retention_from"] not in RETENTION_FROM:
            raise ValueError(
                f"{p}: retention_from must be one of {', '.join(RETENTION_FROM)}, "
                f"got {data['retention_from']!r}"
            )
        cfg.retention_from = data["retention_from"]
    retention = data.get("retention") or {}
    if not isinstance(retention, dict):
        raise ValueError(f"{p}: retention must be a mapping")
    cfg.unknown_keys += [f"retention.{k}" for k in retention if k not in _RETENTION_KEYS]
    for key in ("high_days", "low_days"):
        setattr(
            cfg,
            f"retention_{key}",
            _int(retention.get(key, getattr(cfg, f"retention_{key}")), f"{p}: retention.{key}", 1),
        )
    return _keep_long(cfg)


def bank_countries(discovery_path: Path) -> tuple[str, list[str]] | None:
    """(home, every other country the answer bank lists), or None with no bank.
    The example discovery file reads the example bank."""
    from applyourself.apply.bank_schema import (
        EXAMPLE_NAME,
        AnswersError,
        load_answers,
    )
    from applyourself.shared.countries import canonical_country

    bank = (
        paths.example(EXAMPLE_NAME)
        if discovery_path.name.startswith("discovery.example")
        else paths.ANSWER_BANK
    )
    if not bank.exists():
        return None
    try:
        answers = load_answers(bank)
    except AnswersError as exc:
        raise ValueError(str(exc)) from exc
    home = canonical_country(answers.home_country) or answers.home_country
    others = [canonical_country(c) for c in answers.countries if c != "default"]
    return home, [c for c in dict.fromkeys(others) if c and c != home]


def _countries(cfg: DiscoveryConfig, p: Path, allow: dict) -> DiscoveryConfig:
    """The bank's countries: the allowlist (unless `location_allowlist.countries`
    sets it) and, for each `abroad.sources` lane, the abroad countries."""
    found = bank_countries(p)
    if found is None:
        return cfg
    home, others = found
    if "countries" not in allow:
        cfg.location_allowlist = LocationAllowlist([home, *others])
    for name in cfg.abroad_sources:
        if name in cfg.sources:
            cfg.sources[name].abroad_countries = list(others)
            cfg.sources[name].abroad_verticals = list(cfg.abroad_lanes_listed)
    return cfg


def _keep_long(cfg: DiscoveryConfig) -> DiscoveryConfig:
    """Retention's score cutoff: `score_filter.retention_cutoff`, the lowest lane cutoff."""
    from applyourself import score_filter

    if not paths.SCORING.exists():
        raise FileNotFoundError(
            f"{paths.SCORING} not found (retention reads cutoffs.keep_long) — copy "
            "profile/scoring.example.yaml to profile/scoring.yaml (/onboarding does this)"
        )
    cfg.retention_score_threshold = float(score_filter.retention_cutoff())
    return cfg


def main() -> int:
    """`discovery-check`: what discovery.yaml resolves to, and every problem."""
    setup_logging()
    try:
        cfg = load_config()
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(
        f"countries: {', '.join(sorted(cfg.location_allowlist.effective_countries())) or '(any)'}"
    )
    print(
        f"search in: {', '.join(cfg.effective_search_locations())}"
        + ("" if cfg.search_locations else "  (from the allowlist)")
    )

    def line(name: str, s: SourceConfig) -> str:
        bits = [f"{s.pacing_seconds:g}s"]
        if name in JOBSPY_SOURCES:
            bits.append(f"{s.results_wanted} results/query")
            if not s.remote_queries:
                bits.append("no remote queries")
            if s.abroad_countries:
                bits.append(
                    f"abroad {'/'.join(s.abroad_verticals)} in {len(s.abroad_countries)} countries"
                )
            if s.company_ids:
                bits.append(f"{len(s.company_ids)} companies x {len(s.company_terms)} terms")
        elif name in FEED_SOURCES:
            bits = [
                f"snapshot within {s.max_snapshot_age_hours}h",
                f"a page failed {s.page_read_max_failures} nights is read every "
                f"{s.page_read_retry_days}d",
            ]
        else:
            bits.append(f"every {s.every_days}d")
        return f"{name} ({', '.join(bits)})"

    enabled = [(n, s) for n, s in cfg.sources.items() if s.enabled]
    print("sources:   " + (", ".join(line(n, s) for n, s in enabled) or "(none enabled)"))
    age = (
        "no posted-age cut"
        if cfg.search_max_age_days is None
        else f"search rows posted within {cfg.search_max_age_days}d"
    )
    print(
        f"live view: seen within {cfg.live_days}d; {age}; keep {cfg.retention_high_days}d at fit >= "
        f"{cfg.retention_score_threshold:g}, else {cfg.retention_low_days}d, "
        f"from the {cfg.retention_from} posting"
    )
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
