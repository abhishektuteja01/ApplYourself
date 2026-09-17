from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
import yaml
from src import verticals
from src import paths

log = logging.getLogger(__name__)

REPO_ROOT = paths.REPO_ROOT
DEFAULT_CONFIG_PATH = REPO_ROOT / "profile" / "discovery.yaml"
_SCHEMA_VERSION = 1

# The loader's whole surface. A key outside these sets never applies, so it is
# collected as an unknown key rather than ignored -- see DiscoveryConfig.validate.
_TOP_LEVEL_KEYS = frozenset({
    "schema_version", "deadline_hours", "location_allowlist", "sources",
    "raw_retention_days", "board_max_age_days", "search_locations",
})
_ALLOWLIST_KEYS = frozenset({"countries", "states", "cities", "continents"})
_SOURCE_KEYS = frozenset({"enabled", "pacing_seconds", "cadence"})

# Ceiling on the locations the jobspy lanes may query when `search_locations`
# is absent and the allowlist is the fallback. Each location multiplies the
# query count, and the pacing sleep alone can outrun `deadline_hours`.
MAX_SEARCH_LOCATIONS = 5

# The pacing floor config cannot go below, per source. Default 1.0s; the three
# lanes below tolerate 0.5s. One home for what used to be four copies of the
# same two numbers -- the runtime sleeps in `ats/base.py`, `jobspy_source.py`
# and `workday.py`, plus the cost estimator here, which must model the same
# floor the lanes will actually sleep.
DEFAULT_MIN_PACING_SECONDS = 1.0
MIN_PACING_SECONDS = {"greenhouse": 0.5, "linkedin": 0.5, "indeed": 0.5}


def pacing_floor(source_name: str) -> float:
    return MIN_PACING_SECONDS.get(source_name, DEFAULT_MIN_PACING_SECONDS)


# How often a source runs. `daily` is the historical behaviour and the
# default; the rest exist because board novelty is 2-3% a night and near zero
# at the weekend, so polling every board every night buys almost nothing.
# `weekly` anchors on Monday: the weekend's backlog lands on the first
# productive night rather than being spread across two dead ones.
CADENCE_DAILY = "daily"
CADENCE_WEEKDAYS = "weekdays"
CADENCE_WEEKLY = "weekly"
_EVERY_N_DAYS = "every_n_days:"
_WEEKLY_ANCHOR = 0  # Monday, as datetime.weekday() numbers it.


def parse_cadence(value: str) -> str:
    """The normalized cadence, or raise ValueError naming what was wrong.

    Kept separate from `due_today` so a bad value fails at config load, next
    to `Unknown source key`, rather than silently never running a lane.
    """
    v = str(value).strip().lower()
    if v in (CADENCE_DAILY, CADENCE_WEEKDAYS, CADENCE_WEEKLY):
        return v
    if v.startswith(_EVERY_N_DAYS):
        n = v[len(_EVERY_N_DAYS):].strip()
        if n.isdigit() and int(n) >= 1:
            return f"{_EVERY_N_DAYS}{int(n)}"
        raise ValueError(
            f"cadence {value!r}: every_n_days needs a whole number >= 1, got {n!r}")
    raise ValueError(
        f"unknown cadence {value!r}. One of: {CADENCE_DAILY}, {CADENCE_WEEKDAYS}, "
        f"{CADENCE_WEEKLY}, {_EVERY_N_DAYS}N")


def due_today(cadence: str, run_date) -> bool:
    """Whether a source on `cadence` runs on `run_date` (a date or datetime).

    `every_n_days:N` keys off the proleptic ordinal rather than a stored
    last-run date: no state file to lose, and a missed night does not shift
    the whole schedule.
    """
    if cadence == CADENCE_DAILY:
        return True
    if cadence == CADENCE_WEEKDAYS:
        return run_date.weekday() < 5
    if cadence == CADENCE_WEEKLY:
        return run_date.weekday() == _WEEKLY_ANCHOR
    if cadence.startswith(_EVERY_N_DAYS):
        return run_date.toordinal() % int(cadence[len(_EVERY_N_DAYS):]) == 0
    # parse_cadence rejects everything else at load; an unparsed value here
    # means a hand-built SourceConfig, and running is the safe reading.
    return True


@dataclass
class SourceConfig:
    enabled: bool
    pacing_seconds: float
    cadence: str = CADENCE_DAILY

def _continent_countries(continent: str) -> list[str]:
    """Countries on `continent`, matched case- and whitespace-insensitively.
    `CONTINENT_TO_COUNTRIES` is keyed by display name ("Europe"), so a raw
    `.get()` made `continents: ["europe"]` a silent no-op -- which emptied the
    allowlist and turned location filtering off entirely."""
    from src.discovery import location

    by_fold = {location._fold(k): v for k, v in location.CONTINENT_TO_COUNTRIES.items()}
    return by_fold.get(location._fold(continent), [])


def _subdivisions_for(state: str) -> list:
    """Subdivisions a configured `states:` entry names, by full name first and
    bare ISO 3166-2 code second. A bare code is not globally unique, so the
    code branch applies `location._disambiguate_subdivisions`' conventional
    tie-break: "CA" means California, not Burundi's Cayanza, because a job
    posting written in English that abbreviates a subdivision to two letters
    is using one of the countries that are conventionally written that way."""
    from src.discovery import location

    subs = location.SUBDIVISIONS_BY_NAME.get(location._fold(state), [])
    if subs:
        return subs
    subs = location.SUBDIVISIONS_BY_CODE.get(state.strip().upper(), [])
    conventional = [s for s in subs if s.country_code in location._COMMONLY_ABBREVIATED_CC]
    return conventional or subs


@dataclass
class LocationAllowlist:
    countries: list[str] = field(default_factory=list)
    states: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    # Optional shorthand: "Europe" expands to every European country at
    # compare time instead of the user typing ~44 country names by hand.
    # Purely additive -- existing countries-only configs are unaffected.
    continents: list[str] = field(default_factory=list)

    def effective_countries(self) -> set[str]:
        """`countries` plus whatever `continents` expands to, normalized to
        the canonical names `location.parse_location` returns."""
        from src.discovery import location  # local import: avoid a hard
        # dependency on libpostal's system library for callers that never
        # touch location filtering (e.g. pure config validation/tests).

        result = {location.COUNTRY_NAMES.get(location._fold(c), c) for c in self.countries}
        for continent in self.continents:
            result |= set(_continent_countries(continent))

        if not result and self.states:
            # A states-only allowlist ("just TX") with no countries/continents
            # configured still needs a country floor: without one, a row that
            # only resolves to a bare country ("Canada", no state text) has
            # no `parsed.state` for the states check to test and slips through
            # unfiltered. Scope it to whichever countries the configured
            # states actually belong to.
            for s in self.states:
                result |= {location.CC_TO_COUNTRY.get(sub.country_code, "")
                           for sub in _subdivisions_for(s)}
            result.discard("")

        return result

    def unresolved(self) -> list[str]:
        """Configured entries that name nothing the parser can ever produce.
        Each one is silent at runtime -- a typo'd country drops every row, a
        typo'd continent disables filtering -- so they are reported, not
        guessed at."""
        from src.discovery import location

        problems = []
        for c in self.countries:
            if location._fold(c) not in location.COUNTRY_NAMES:
                problems.append(f"location_allowlist.countries: {c!r} is not a country name")
        for c in self.continents:
            if not _continent_countries(c):
                names = ", ".join(sorted(location.CONTINENT_TO_COUNTRIES))
                problems.append(
                    f"location_allowlist.continents: {c!r} is not a continent (one of: {names})")
        for s in self.states:
            if not _subdivisions_for(s):
                problems.append(
                    f"location_allowlist.states: {s!r} is not a state/province name or code")
        for city in self.cities:
            if location._city_lookup(city) is None:
                problems.append(f"location_allowlist.cities: {city!r} is not a known city")
        return problems

    def configured(self) -> bool:
        return any((self.countries, self.states, self.cities, self.continents))

    def effective_states(self) -> set[str]:
        """`states` normalized to the 2-letter subdivision codes
        `location.parse_location` returns, accepting either a full name
        ("Texas") or a code ("TX") -- full names used to be a silent no-op
        because the parser only ever produced codes."""
        from src.discovery import location

        result = set()
        for s in self.states:
            subs = location.SUBDIVISIONS_BY_NAME.get(location._fold(s), [])
            if subs:
                # A name that collides across countries (e.g. "Santa Cruz")
                # can't be narrowed here -- there's no sibling country to
                # check against, unlike the parser's own use of this table.
                # Accept any of them; membership is still exact-code matched
                # per row downstream, so this only ever widens the allowlist
                # to cover every country that name could mean, never the
                # wrong single one.
                result.update(sub.code.split("-", 1)[-1] for sub in subs)
            else:
                result.add(s.strip())
        return result

def _fmt_duration(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    if seconds >= 60:
        return f"{seconds / 60:.0f}m"
    return f"{seconds:.0f}s"


def _jobspy_query_cost(cfg: "DiscoveryConfig", n_locations: int) -> tuple[int, float]:
    """(queries, pacing seconds) the enabled jobspy lanes would spend on
    `n_locations`. Each lane runs terms x locations x (on-site, remote) and
    sleeps `pacing_seconds` between queries -- see jobspy_source.fetch."""
    try:
        vcfg = verticals.get_config()
    except (FileNotFoundError, ValueError):
        # load_config() resolves verticals.yaml before any caller can reach
        # validate(), and raises there. Reaching this means a hand-built
        # config: report the cap without a cost estimate rather than raising
        # a second time from inside a problem-reporting method.
        vcfg = None
    lanes = getattr(vcfg, "verticals", None) or {}

    queries = 0
    seconds = 0.0
    for name, attr in (("linkedin", "linkedin_terms"), ("indeed", "search_terms")):
        source = cfg.sources.get(name)
        if source is None or not source.enabled:
            continue
        terms = sum(len(getattr(v, attr, ()) or ()) for v in lanes.values())
        n = terms * n_locations * 2
        queries += n
        seconds += n * max(pacing_floor(name), source.pacing_seconds)
    return queries, seconds


@dataclass
class DiscoveryConfig:
    schema_version: int = _SCHEMA_VERSION
    deadline_hours: float = 4.0
    location_allowlist: LocationAllowlist = field(default_factory=lambda: LocationAllowlist(["United States"]))
    sources: dict[str, SourceConfig] = field(default_factory=lambda: {
        "linkedin": SourceConfig(True, 3.0),
        "indeed": SourceConfig(True, 2.0),
        "greenhouse": SourceConfig(True, 1.0),
        "lever": SourceConfig(True, 1.0),
        "ashby": SourceConfig(True, 2.0),
        "workday": SourceConfig(True, 2.0),
    })
    raw_retention_days: int = 16
    # Age cap for the staleness-exempt board sources (step 3). 0 = off.
    board_max_age_days: int = 0
    # Where the jobspy lanes *search*, as against the allowlist, which is what
    # cleaning *accepts*. Empty = fall back to the allowlist's countries.
    search_locations: list[str] = field(default_factory=list)
    # Keys the loader did not recognise, recorded rather than dropped so
    # validate() can name them. A typo'd key is otherwise indistinguishable
    # from a setting that silently never applied.
    unknown_keys: list[str] = field(default_factory=list)

    def effective_search_locations(self) -> list[str]:
        """The `location=` values the jobspy lanes query, in query order.
        `search_locations` when set, else the allowlist's effective countries.
        The ATS sources are unaffected: they crawl globally and filter at
        cleaning, so a wide allowlist costs them nothing."""
        if self.search_locations:
            return sorted(self.search_locations)
        allow = self.location_allowlist
        countries = sorted(allow.effective_countries()) if allow is not None else []
        return countries or ["United States"]

    def validate(self) -> list[str]:
        """Every problem with this config, in human-readable form. Empty list
        means the config is usable. Reads what the user configured and reports
        it back; it holds no opinion about what they *should* have configured."""
        problems = [f"unknown config key: {k}" for k in self.unknown_keys]

        allow = self.location_allowlist
        if allow is not None:
            problems.extend(allow.unresolved())
            effective = (allow.effective_countries() | allow.effective_states()
                         | {c for c in allow.cities if c.strip()})
            if allow.configured() and not effective:
                problems.append(
                    "location_allowlist resolves to nothing: every scraped row would "
                    "be dropped, or location filtering skipped entirely")

        if not self.search_locations:
            n = len(self.effective_search_locations())
            if n > MAX_SEARCH_LOCATIONS:
                queries, seconds = _jobspy_query_cost(self, n)
                cost = (f"the jobspy lanes would run ~{queries} queries, "
                        f"~{_fmt_duration(seconds)} of pacing sleep alone. "
                        if queries else "")
                problems.append(
                    f"location_allowlist expands to {n} search locations "
                    f"(max {MAX_SEARCH_LOCATIONS} without an explicit search_locations): "
                    f"{cost}Set search_locations to the countries to query.")
        return problems

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

    cfg.unknown_keys = [k for k in data if k not in _TOP_LEVEL_KEYS]
    if isinstance(data.get("location_allowlist"), dict):
        cfg.unknown_keys += [f"location_allowlist.{k}"
                             for k in data["location_allowlist"]
                             if k not in _ALLOWLIST_KEYS]

    version = data.get("schema_version", _SCHEMA_VERSION)
    if version != _SCHEMA_VERSION:
        raise ValueError(f"{p}: schema_version must be {_SCHEMA_VERSION}, got {version!r}")
    cfg.schema_version = _SCHEMA_VERSION

    if "sources" in data:
        allowed_sources = {"linkedin", "indeed", "greenhouse", "lever", "ashby", "workday"}
        for k, v in data["sources"].items():
            if k not in allowed_sources:
                raise ValueError(f"Unknown source key: {k}")
            cfg.unknown_keys += [f"sources.{k}.{sub}"
                                 for sub in v if sub not in _SOURCE_KEYS]
            try:
                cadence = parse_cadence(v.get("cadence", CADENCE_DAILY))
            except ValueError as e:
                raise ValueError(f"sources.{k}: {e}") from None
            cfg.sources[k] = SourceConfig(
                enabled=bool(v.get("enabled", True)),
                pacing_seconds=float(v.get("pacing_seconds", 1.0)),
                cadence=cadence,
            )

    if "location_allowlist" in data:
        loc = data["location_allowlist"]
        cfg.location_allowlist = LocationAllowlist(
            countries=loc.get("countries", []),
            states=loc.get("states", []),
            cities=loc.get("cities", []),
            continents=loc.get("continents", []),
        )

    if "search_locations" in data:
        raw_locations = data["search_locations"] or []
        if not isinstance(raw_locations, list):
            raise ValueError(f"{p}: search_locations must be a list of location names")
        cfg.search_locations = [str(s).strip() for s in raw_locations if str(s).strip()]

    cfg.deadline_hours = float(data.get("deadline_hours", cfg.deadline_hours))
    cfg.raw_retention_days = int(data.get("raw_retention_days", cfg.raw_retention_days))

    cfg.board_max_age_days = int(data.get("board_max_age_days", cfg.board_max_age_days))
    if cfg.board_max_age_days < 0:
        raise ValueError(f"{p}: board_max_age_days must be >= 0, got {cfg.board_max_age_days}")

    return cfg


def main() -> int:
    """`discovery-check` -- print what discovery.yaml actually resolves to and
    every problem with it. The counterpart to `verticals-check`."""
    try:
        cfg = load_config()
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1

    allow = cfg.location_allowlist
    if allow is not None:
        def _fmt(values):
            return ", ".join(sorted(values)) if values else "(any)"
        print(f"countries: {_fmt(allow.effective_countries())}")
        print(f"states:    {_fmt(allow.effective_states())}")
        print(f"cities:    {_fmt(allow.cities)}")

    searched = cfg.effective_search_locations()
    print(f"search in: {', '.join(searched)}"
          + ("" if cfg.search_locations else "  (from the allowlist)"))

    enabled = [(n, s) for n, s in cfg.sources.items() if s.enabled]

    def _source(name, src):
        # Cadence is shown only when it is not the default, so the common line
        # stays readable and an unusual schedule stands out.
        suffix = "" if src.cadence == CADENCE_DAILY else f", {src.cadence}"
        return f"{name} ({src.pacing_seconds:g}s{suffix})"

    print("sources:   " + (", ".join(_source(n, s) for n, s in enabled)
                           if enabled else "(none enabled)"))
    today = date.today()
    not_due = [n for n, s in enabled if not due_today(s.cadence, today)]
    if not_due:
        print(f"           not due today ({today:%a}): {', '.join(not_due)}")
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
