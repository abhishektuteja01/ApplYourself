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
    """load_config only overrides keys present in the YAML, so a config that
    omits these dead sources must not enable them."""
    from src import verticals
    monkeypatch.setattr(verticals, "get_config", lambda: None)

    config = load_config(tmp_path / "nonexistent.yaml")
    assert config.sources["zip_recruiter"].enabled is False
    assert config.sources["google"].enabled is False
    for live in ("linkedin", "indeed", "greenhouse", "lever", "ashby"):
        assert config.sources[live].enabled is True


def test_example_config_covers_every_allowed_source(monkeypatch):
    """The template must list every source the loader accepts."""
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

@pytest.mark.parametrize("version", [2, 0, "1"])
def test_unsupported_schema_version_rejected(tmp_path, monkeypatch, version):
    from src import verticals
    monkeypatch.setattr(verticals, "get_config", lambda: None)

    p = tmp_path / "v2.yaml"
    p.write_text(yaml.dump({"schema_version": version, "deadline_hours": 3}))
    with pytest.raises(ValueError, match="schema_version must be 1"):
        load_config(p)

def test_schema_version_1_and_absent_both_accepted(tmp_path, monkeypatch):
    from src import verticals
    monkeypatch.setattr(verticals, "get_config", lambda: None)

    explicit = tmp_path / "v1.yaml"
    explicit.write_text(yaml.dump({"schema_version": 1, "deadline_hours": 3}))
    absent = tmp_path / "no_version.yaml"
    absent.write_text(yaml.dump({"deadline_hours": 3}))
    for p in (explicit, absent):
        cfg = load_config(p)
        assert cfg.schema_version == 1
        assert cfg.deadline_hours == 3.0

def test_example_config_declares_the_supported_schema_version(monkeypatch):
    from src import verticals
    monkeypatch.setattr(verticals, "get_config", lambda: None)

    cfg = load_config(Path("profile/discovery.example.yaml"))
    assert cfg.schema_version == 1

def test_missing_verticals_yaml(tmp_path, monkeypatch):
    from src import verticals
    def mock_get_config():
        raise FileNotFoundError("profile/verticals.yaml not found")
    monkeypatch.setattr(verticals, "get_config", mock_get_config)
    
    with pytest.raises(SystemExit) as e:
        load_config(tmp_path / "nonexistent.yaml")
    assert "profile/verticals.yaml missing" in str(e.value)
