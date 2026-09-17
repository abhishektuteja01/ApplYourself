import pytest
import yaml
from pathlib import Path
from src.discovery.config import load_config, DiscoveryConfig

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
    assert cfg.validate() == []
    assert "France" in cfg.location_allowlist.effective_countries()


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


def test_the_real_and_example_configs_validate(monkeypatch):
    from src import verticals
    monkeypatch.setattr(verticals, "get_config", lambda: None)
    root = Path(__file__).resolve().parents[2] / "profile"
    for name in ("discovery.example.yaml", "discovery.yaml"):
        p = root / name
        if p.exists():
            assert load_config(p).validate() == [], name
