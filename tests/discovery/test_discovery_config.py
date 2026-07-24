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
