import re

import pytest
import yaml
from pathlib import Path
from src.discovery.config import MAX_SEARCH_LOCATIONS, load_config, DiscoveryConfig

def test_missing_file(tmp_path, monkeypatch):
    # Mock verticals.get_config so it doesn't fail
    from src import verticals
    monkeypatch.setattr(verticals, "get_config", lambda: None)

    config = load_config(tmp_path / "nonexistent.yaml")
    assert config.deadline_hours == 4.0
    assert "linkedin" in config.sources

def test_every_default_source_is_live_and_enabled(tmp_path, monkeypatch):
    """zip_recruiter and google were removed, so there is no longer such a
    thing as a configured-but-dead source: every default is enabled, and a
    config naming one of the removed keys is an unknown-source error."""
    from src import verticals
    monkeypatch.setattr(verticals, "get_config", lambda: None)

    config = load_config(tmp_path / "nonexistent.yaml")
    assert set(config.sources) == {"linkedin", "indeed", "greenhouse", "lever", "ashby", "workday"}
    assert all(s.enabled for s in config.sources.values())


@pytest.mark.parametrize("removed", ["zip_recruiter", "google"])
def test_a_removed_source_key_is_rejected(tmp_path, monkeypatch, removed):
    from src import verticals
    monkeypatch.setattr(verticals, "get_config", lambda: None)

    p = tmp_path / "old.yaml"
    p.write_text(yaml.dump({"sources": {removed: {"enabled": False}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown source key"):
        load_config(p)


def test_example_config_covers_every_allowed_source(monkeypatch):
    """The template must list every source the loader accepts."""
    from src import verticals
    monkeypatch.setattr(verticals, "get_config", lambda: None)

    example = Path(__file__).resolve().parents[2] / "profile" / "discovery.example.yaml"
    listed = set(yaml.safe_load(example.read_text(encoding="utf-8"))["sources"])
    assert listed == set(DiscoveryConfig().sources)
    # And it must parse under the real loader, not just as YAML.
    assert all(s.enabled for s in load_config(example).sources.values())


def test_malformed_yaml(tmp_path, monkeypatch):
    from src import verticals
    monkeypatch.setattr(verticals, "get_config", lambda: None)

    p = tmp_path / "bad.yaml"
    p.write_text("unbalanced: [", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(p)

def test_unknown_source_key(tmp_path, monkeypatch):
    from src import verticals
    monkeypatch.setattr(verticals, "get_config", lambda: None)

    p = tmp_path / "bad_source.yaml"
    p.write_text(yaml.dump({"sources": {"bad_site": {"enabled": True}}}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(p)

@pytest.mark.parametrize("version", [2, 0, "1"])
def test_unsupported_schema_version_rejected(tmp_path, monkeypatch, version):
    from src import verticals
    monkeypatch.setattr(verticals, "get_config", lambda: None)

    p = tmp_path / "v2.yaml"
    p.write_text(yaml.dump({"schema_version": version, "deadline_hours": 3}), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version must be 1"):
        load_config(p)

def test_schema_version_1_and_absent_both_accepted(tmp_path, monkeypatch):
    from src import verticals
    monkeypatch.setattr(verticals, "get_config", lambda: None)

    explicit = tmp_path / "v1.yaml"
    explicit.write_text(yaml.dump({"schema_version": 1, "deadline_hours": 3}), encoding="utf-8")
    absent = tmp_path / "no_version.yaml"
    absent.write_text(yaml.dump({"deadline_hours": 3}), encoding="utf-8")
    for p in (explicit, absent):
        cfg = load_config(p)
        assert cfg.schema_version == 1
        assert cfg.deadline_hours == 3.0

def test_example_config_declares_the_supported_schema_version(monkeypatch):
    from src import verticals
    monkeypatch.setattr(verticals, "get_config", lambda: None)

    example = Path(__file__).resolve().parents[2] / "profile" / "discovery.example.yaml"
    assert example.exists(), "the template this test is about is missing"
    cfg = load_config(example)
    assert cfg.schema_version == 1

def test_missing_verticals_yaml_raises_rather_than_exiting(tmp_path, monkeypatch):
    """load_config is a library function: it must raise so a non-CLI caller can
    handle it. orchestrator.main is what turns this into a message and an exit."""
    from src import verticals
    def mock_get_config():
        raise FileNotFoundError("profile/verticals.yaml not found")
    monkeypatch.setattr(verticals, "get_config", mock_get_config)

    with pytest.raises(FileNotFoundError, match="profile/verticals.yaml missing"):
        load_config(tmp_path / "nonexistent.yaml")


def test_the_cli_turns_a_bad_config_into_a_message_and_an_exit(tmp_path, monkeypatch):
    """The UX the sys.exit used to provide, now at the boundary where it belongs."""
    from src.discovery import orchestrator
    monkeypatch.setattr(orchestrator, "load_config",
                        lambda: (_ for _ in ()).throw(FileNotFoundError("boom")))
    with pytest.raises(SystemExit) as e:
        orchestrator.main([])
    assert "ERROR: boom" in str(e.value)


def test_board_max_age_days_defaults_off(tmp_path, monkeypatch):
    from src import verticals
    monkeypatch.setattr(verticals, "get_config", lambda: None)

    assert load_config(tmp_path / "nonexistent.yaml").board_max_age_days == 0

    p = tmp_path / "d.yaml"
    p.write_text(yaml.dump({"schema_version": 1, "board_max_age_days": 365}), encoding="utf-8")
    assert load_config(p).board_max_age_days == 365


def test_board_max_age_days_rejects_negative(tmp_path, monkeypatch):
    from src import verticals
    monkeypatch.setattr(verticals, "get_config", lambda: None)

    p = tmp_path / "d.yaml"
    p.write_text(yaml.dump({"schema_version": 1, "board_max_age_days": -1}), encoding="utf-8")
    with pytest.raises(ValueError, match="board_max_age_days"):
        load_config(p)


# ---------------------------------------------------------------------
# validate() -- the silent-failure modes (D0.2 + D0.5)
# ---------------------------------------------------------------------

def _load(tmp_path, monkeypatch, data):
    from src import verticals
    monkeypatch.setattr(verticals, "get_config", lambda: None)
    p = tmp_path / "d.yaml"
    p.write_text(yaml.dump({"schema_version": 1, **data}), encoding="utf-8")
    return load_config(p)


def test_a_typod_country_is_flagged(tmp_path, monkeypatch):
    """Silently dropped every row before: the typo became its own allowlist
    entry, which nothing ever matches."""
    cfg = _load(tmp_path, monkeypatch,
                {"location_allowlist": {"countries": ["Untied States"]}})
    problems = cfg.validate()
    assert any("Untied States" in p for p in problems), problems


def test_a_lowercase_continent_resolves_instead_of_disabling_the_filter(tmp_path, monkeypatch):
    """`continents: ["europe"]` used to miss the display-name key, empty the
    allowlist and turn location filtering off entirely."""
    cfg = _load(tmp_path, monkeypatch,
                {"location_allowlist": {"continents": ["europe"]}})
    # The only remaining problem is the search-location cap, which is about
    # query cost, not about the continent failing to resolve.
    assert [p for p in cfg.validate() if "search locations" not in p] == []
    # What this test exists to prove: the continent resolved, so the allowlist
    # is non-empty and location filtering is still on.
    assert "France" in cfg.location_allowlist.effective_countries()
    assert cfg.location_allowlist.configured()


def test_a_wide_continent_with_explicit_search_locations_is_clean(tmp_path, cfg):
    """The pair of the test above: a continent-wide allowlist is only a
    problem while it is also the search list."""
    loaded = _load_costed(tmp_path, {
        "location_allowlist": {"continents": ["europe"]},
        "search_locations": ["Germany", "Netherlands"],
    })
    assert loaded.validate() == []
    assert "France" in loaded.location_allowlist.effective_countries()


def test_an_unknown_continent_is_flagged(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch,
                {"location_allowlist": {"continents": ["Eurpoe"]}})
    assert any("Eurpoe" in p for p in cfg.validate())


def test_a_states_only_allowlist_floors_to_the_conventional_country(tmp_path, monkeypatch):
    """Bare ISO 3166-2 codes collide worldwide; "CA" alone used to float the
    country floor up to 12 countries."""
    cfg = _load(tmp_path, monkeypatch,
                {"location_allowlist": {"states": ["CA", "NY", "TX"]}})
    assert cfg.validate() == []
    assert cfg.location_allowlist.effective_countries() == {"United States"}


def test_a_typod_state_is_flagged(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch,
                {"location_allowlist": {"states": ["Texsa"]}})
    assert any("Texsa" in p for p in cfg.validate())


@pytest.mark.parametrize("data,needle", [
    ({"deadlne_hours": 4}, "deadlne_hours"),
    ({"location_allowlist": {"countires": ["United States"]}}, "location_allowlist.countires"),
])
def test_an_unknown_key_is_flagged(tmp_path, monkeypatch, data, needle):
    """D0.5: a misspelled key never applies, so it must not pass silently."""
    cfg = _load(tmp_path, monkeypatch, data)
    assert any(needle in p for p in cfg.validate())


def test_an_allowlist_that_resolves_to_nothing_is_flagged(tmp_path, monkeypatch):
    """A continents-only allowlist that expands to no country leaves
    `allow_countries` empty, which is "filter off", not "filter nothing"."""
    cfg = _load(tmp_path, monkeypatch,
                {"location_allowlist": {"continents": ["Eurpoe"]}})
    assert any("resolves to nothing" in p for p in cfg.validate())


def test_a_good_config_has_no_problems(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch, {
        "location_allowlist": {"countries": ["United States"], "states": ["Texas"],
                               "cities": ["Austin"]},
        "sources": {"linkedin": {"enabled": True, "pacing_seconds": 3}},
    })
    assert cfg.validate() == []


# ---------------------------------------------------------------------
# search_locations -- where we query, as against what we accept (D0.3)
# ---------------------------------------------------------------------

SIX_COUNTRIES = ["United States", "Canada", "France", "Germany", "Spain", "Italy"]


def _load_costed(tmp_path, data):
    """Like _load, but leaves verticals.get_config alone: the cap's query
    estimate reads the fixture lanes' term counts."""
    p = tmp_path / "search.yaml"
    p.write_text(yaml.dump({"schema_version": 1, **data}), encoding="utf-8")
    return load_config(p)


def test_a_wide_allowlist_with_no_search_locations_is_an_error(tmp_path, cfg):
    """The failure mode was silent: the pacing sleep alone outruns
    deadline_hours and the shard lands near-empty."""
    loaded = _load_costed(tmp_path, {"location_allowlist": {"countries": SIX_COUNTRIES}})
    problems = [p for p in loaded.validate() if "search locations" in p]
    assert len(problems) == 1, loaded.validate()
    message = problems[0]

    linkedin_terms = sum(len(v.linkedin_terms) for v in cfg.verticals.values())
    indeed_terms = sum(len(v.search_terms) for v in cfg.verticals.values())
    queries = (linkedin_terms + indeed_terms) * len(SIX_COUNTRIES) * 2
    assert queries > 0
    assert f"{len(SIX_COUNTRIES)} search locations" in message
    assert f"~{queries} queries" in message
    # and a sleep estimate, so the failure explains its own cost
    assert re.search(r"~[\d.]+[hms] of pacing sleep", message), message


def test_at_the_cap_there_is_no_problem(tmp_path, cfg):
    loaded = _load_costed(
        tmp_path, {"location_allowlist": {"countries": SIX_COUNTRIES[:MAX_SEARCH_LOCATIONS]}})
    assert loaded.validate() == []
    assert len(loaded.effective_search_locations()) == MAX_SEARCH_LOCATIONS


def test_an_explicit_search_locations_passes_however_wide_the_allowlist(tmp_path, cfg):
    """The allowlist is what cleaning accepts; search_locations is what jobspy
    queries. A continent-wide allowlist is free once they are decoupled."""
    loaded = _load_costed(tmp_path, {
        "location_allowlist": {"continents": ["Europe"]},
        "search_locations": ["Germany", "Netherlands", "United Kingdom"],
    })
    assert loaded.validate() == []
    assert loaded.effective_search_locations() == [
        "Germany", "Netherlands", "United Kingdom"]
    # ...and the allowlist still resolves wide, for cleaning to filter on.
    assert len(loaded.location_allowlist.effective_countries()) > MAX_SEARCH_LOCATIONS


def test_absent_search_locations_falls_back_to_the_allowlist(tmp_path, cfg):
    loaded = _load_costed(tmp_path, {"location_allowlist": {"countries": ["Canada"]}})
    assert loaded.search_locations == []
    assert loaded.effective_search_locations() == ["Canada"]


def test_a_non_list_search_locations_is_rejected(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="search_locations must be a list"):
        _load(tmp_path, monkeypatch, {"search_locations": "Germany"})


def test_the_real_and_example_configs_validate(monkeypatch):
    from src import verticals
    monkeypatch.setattr(verticals, "get_config", lambda: None)
    root = Path(__file__).resolve().parents[2] / "profile"
    for name in ("discovery.example.yaml", "discovery.yaml"):
        p = root / name
        if p.exists():
            assert load_config(p).validate() == [], name


def test_pacing_floor_is_the_one_home_for_every_lane_floor():
    """The four copies this replaced: ats/base.py's dict + 1.0 default,
    jobspy_source.py's 0.5, workday.py's 1.0, and the estimator's 0.5."""
    from src.discovery.config import pacing_floor

    assert pacing_floor("greenhouse") == 0.5
    assert pacing_floor("linkedin") == 0.5
    assert pacing_floor("indeed") == 0.5
    assert pacing_floor("lever") == 1.0
    assert pacing_floor("ashby") == 1.0
    assert pacing_floor("workday") == 1.0
    assert pacing_floor("a source that does not exist") == 1.0


def test_every_runtime_sleep_reads_the_shared_floor():
    """A fifth copy would not fail any behavioural test, since config sits
    above every floor today. Assert on the source text instead."""
    root = Path(__file__).resolve().parents[2] / "src" / "discovery"
    for rel in ("sources/ats/base.py", "sources/ats/workday.py",
                "sources/jobspy_source.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "pacing_floor(self.name)" in text, rel
        assert "max(0.5," not in text, rel
        assert "max(1.0," not in text, rel


# --- cadence ---------------------------------------------------------------

def test_cadence_defaults_to_daily_and_round_trips(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch, {"sources": {
        "linkedin": {"enabled": True},
        "lever": {"enabled": True, "cadence": "weekly"},
        "workday": {"enabled": True, "cadence": "EVERY_N_DAYS: 3"},
    }})
    assert cfg.sources["linkedin"].cadence == "daily"
    assert cfg.sources["lever"].cadence == "weekly"
    # Normalized: case folded, whitespace stripped, N re-rendered as an int.
    assert cfg.sources["workday"].cadence == "every_n_days:3"


@pytest.mark.parametrize("bad", ["hourly", "every_n_days:", "every_n_days:0",
                                 "every_n_days:-2", "every_n_days:1.5", ""])
def test_a_bad_cadence_fails_at_load_not_silently(tmp_path, monkeypatch, bad):
    """It has to raise. A cadence that silently fell back to daily would be
    invisible, and one that fell back to never would stop a lane for good."""
    with pytest.raises(ValueError, match="lever"):
        _load(tmp_path, monkeypatch, {"sources": {"lever": {"cadence": bad}}})


def test_an_unknown_key_inside_a_source_block_is_flagged(tmp_path, monkeypatch):
    """The trap `cadence` itself nearly fell into: source blocks read only the
    keys they know, so a misspelling used to be a silent no-op."""
    cfg = _load(tmp_path, monkeypatch, {"sources": {"lever": {"cadance": "weekly"}}})
    assert "sources.lever.cadance" in cfg.unknown_keys
    assert any("sources.lever.cadance" in p for p in cfg.validate())


def test_due_today_truth_table():
    from datetime import date

    from src.discovery.config import due_today

    monday, friday, saturday, sunday = (date(2026, 9, 14), date(2026, 9, 18),
                                        date(2026, 9, 19), date(2026, 9, 20))
    for day in (monday, friday, saturday, sunday):
        assert due_today("daily", day) is True
    assert [due_today("weekdays", d) for d in (monday, friday, saturday, sunday)] \
        == [True, True, False, False]
    # Monday is the anchor: weekend novelty is near zero, so the weekly sweep
    # lands on the first productive night.
    assert [due_today("weekly", d) for d in (monday, friday, saturday, sunday)] \
        == [True, False, False, False]


def test_every_n_days_is_periodic_and_never_stalls():
    from datetime import date, timedelta

    from src.discovery.config import due_today

    for n in (1, 2, 3, 7, 30):
        days = [date(2026, 1, 1) + timedelta(days=i) for i in range(90)]
        due = [d for d in days if due_today(f"every_n_days:{n}", d)]
        # Phase depends on where the window starts, so the count is the
        # floor or the ceiling; the gap below is the real invariant.
        assert len(due) in (90 // n, 90 // n + 1)
        gaps = {(b - a).days for a, b in zip(due, due[1:])}
        assert gaps <= {n}, (n, gaps)


def test_an_unparsed_cadence_runs_rather_than_never_running():
    from datetime import date

    from src.discovery.config import due_today

    assert due_today("nonsense that load_config would have rejected", date(2026, 9, 19))
