import pytest
import yaml
from pathlib import Path
from src.discovery.config import load_config, DiscoveryConfig

def test_missing_file(tmp_path, monkeypatch):
    # Mock verticals.get_config so it doesn't fail
    from src import verticals
    monkeypatch.setattr(verticals, "get_config", lambda: None)
    
    config = load_config(tmp_path / "nonexistent.yaml")
    assert config.deadline_hours == 6.0
    assert "linkedin" in config.sources

def test_dead_sources_default_disabled(tmp_path, monkeypatch):
    """zip_recruiter and google are dead upstream. load_config only overrides
    keys physically present in the YAML, so a config that omits them must not
    scrape them — a fresh clone from discovery.example.yaml depends on this."""
    from src import verticals
    monkeypatch.setattr(verticals, "get_config", lambda: None)

    config = load_config(tmp_path / "nonexistent.yaml")
    assert config.sources["zip_recruiter"].enabled is False
    assert config.sources["google"].enabled is False
    for live in ("linkedin", "indeed", "greenhouse", "lever", "ashby"):
        assert config.sources[live].enabled is True


def test_example_config_covers_every_allowed_source(monkeypatch):
    """The onboarding template must list all 7 sources the loader accepts, so
    copying it never leaves a source on an implicit code default."""
    from src import verticals
    monkeypatch.setattr(verticals, "get_config", lambda: None)

    example = Path(__file__).resolve().parents[2] / "profile" / "discovery.example.yaml"
    listed = set(yaml.safe_load(example.read_text())["sources"])
    assert listed == set(DiscoveryConfig().sources)
    # And it must parse under the real loader, not just as YAML.
    assert load_config(example).sources["google"].enabled is False


def test_malformed_yaml(tmp_path, monkeypatch):
    from src import verticals
    monkeypatch.setattr(verticals, "get_config", lambda: None)

    p = tmp_path / "bad.yaml"
    p.write_text("unbalanced: [")
    with pytest.raises(ValueError):
        load_config(p)

def test_unknown_source_key(tmp_path, monkeypatch):
    from src import verticals
    monkeypatch.setattr(verticals, "get_config", lambda: None)

    p = tmp_path / "bad_source.yaml"
    p.write_text(yaml.dump({"sources": {"bad_site": {"enabled": True}}}))
    with pytest.raises(ValueError):
        load_config(p)

def test_missing_verticals_yaml(tmp_path, monkeypatch):
    from src import verticals
    def mock_get_config():
        raise FileNotFoundError("profile/verticals.yaml not found")
    monkeypatch.setattr(verticals, "get_config", mock_get_config)
    
    with pytest.raises(SystemExit) as e:
        load_config(tmp_path / "nonexistent.yaml")
    assert "profile/verticals.yaml missing" in str(e.value)
