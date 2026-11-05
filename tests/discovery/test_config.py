"""config.py: loading, key checks and validate()."""
from __future__ import annotations

import pytest
import yaml

from src.discovery import config as C
from src.shared import paths


def load(tmp_path, data):
    p = tmp_path / "discovery.yaml"
    p.write_text(yaml.safe_dump(data) if isinstance(data, dict) else data, encoding="utf-8")
    return C.load_config(p)


def test_missing_file_gives_defaults(tmp_path):
    cfg = C.load_config(tmp_path / "nope.yaml")
    assert set(cfg.sources) == set(C.SOURCE_NAMES) and all(s.enabled for s in cfg.sources.values())
    assert cfg.validate() == []


def test_malformed_yaml(tmp_path):
    with pytest.raises(ValueError, match="Malformed"):
        load(tmp_path, "a: [")


@pytest.mark.parametrize("version", [1, 3, "2"])
def test_only_the_current_schema_version_loads(tmp_path, version):
    with pytest.raises(ValueError, match="schema_version"):
        load(tmp_path, {"schema_version": version})


def test_unknown_source_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="Unknown source key"):
        load(tmp_path, {"sources": {"monster": {}}})


@pytest.mark.parametrize("data,needle", [
    ({"raw_retention_days": 16}, "raw_retention_days (removed; use raw_keep_days)"),
    ({"sources": {"workday": {"cold_rotation_runs": 3}}}, "sources.workday.cold_rotation_runs (removed; use every_days)"),
    ({"location_allowlist": {"countries": ["United States"], "states": ["TX"]}}, "location_allowlist.states"),
    ({"retention": {"highdays": 60}}, "retention.highdays"),
    ({"sources": {"linkedin": {"abroad": {"countrys": ["India"]}}}}, "sources.linkedin.abroad.countrys"),
    ({"sources": {"linkedin": {"companies": {"term": ["AI"]}}}}, "sources.linkedin.companies.term"),
    ({"typo": 1}, "unknown config key: typo"),
])
def test_unknown_and_retired_keys_are_named(tmp_path, data, needle):
    assert any(needle in p for p in load(tmp_path, data).validate())


def test_every_value_is_read(tmp_path):
    cfg = load(tmp_path, {
        "deadline_hours": 6, "live_days": 10, "search_max_age_days": 7, "forget_after_days": 90,
        "raw_keep_days": 2, "log_keep_days": 9, "dead_every_days": 7,
        "retention": {"score_threshold": 75, "high_days": 45, "low_days": 10},
        "sources": {"workday": {"every_days": 3, "max_pages": 4, "pacing_seconds": 0.5},
                    "linkedin": {"hours_old": 72, "results_wanted": 50, "remote_queries": False},
                    "stapply": {"max_snapshot_age_hours": 30}},
    })
    assert (cfg.deadline_hours, cfg.live_days, cfg.search_max_age_days) == (6, 10, 7)
    assert (cfg.raw_keep_days, cfg.log_keep_days, cfg.dead_every_days) == (2, 9, 7)
    assert (cfg.retention_score_threshold, cfg.retention_high_days, cfg.retention_low_days) == (75, 45, 10)
    w, li = cfg.sources["workday"], cfg.sources["linkedin"]
    assert (w.every_days, w.max_pages, li.hours_old, li.results_wanted, li.remote_queries) == (3, 4, 72, 50, False)
    assert cfg.sources["stapply"].max_snapshot_age_hours == 30
    assert cfg.validate() == []


@pytest.mark.parametrize("data", [
    {"live_days": 0}, {"retention": {"low_days": -1}},
    {"sources": {"workday": {"every_days": 0}}},
    {"sources": {"linkedin": {"page_delay_seconds": 0.1}}},
    {"sources": {"linkedin": {"remote_queries": "no"}}},
    {"sources": {"linkedin": {"abroad": {"countries": "India"}}}},
    {"search_locations": "United States"},
    {"sources": {"stapply": {"max_snapshot_age_hours": 0}}},
    {"sources": {"linkedin": {"companies": {"terms": ["AI"], "ids": {"Acme": "123"}}}}},
    {"sources": {"linkedin": {"companies": {"ids": {"Acme": 123}}}}},
    {"sources": {"indeed": {"companies": {"terms": ["AI"], "ids": {"Acme": 123}}}}},
])
def test_bad_values_fail_at_load(tmp_path, data):
    with pytest.raises(ValueError):
        load(tmp_path, data)


def test_live_days_must_outlast_the_slowest_board_schedule(tmp_path):
    cfg = load(tmp_path, {"live_days": 4, "sources": {"workday": {"every_days": 3}}})
    assert any("live_days" in p for p in cfg.validate())


def test_a_typod_country_is_flagged(tmp_path):
    cfg = load(tmp_path, {"location_allowlist": {"countries": ["Untied States"]}})
    assert any("'Untied States' is not a country name" in p for p in cfg.validate())


def test_a_wide_allowlist_needs_explicit_search_locations(tmp_path):
    many = ["United States", "India", "Germany", "France", "Spain", "Italy"]
    assert any("search_locations" in p
               for p in load(tmp_path, {"location_allowlist": {"countries": many}}).validate())
    assert load(tmp_path, {"location_allowlist": {"countries": many},
                           "search_locations": ["United States"]}).validate() == []


@pytest.fixture
def abroad_lane(cfg):
    from dataclasses import replace

    from src.shared import verticals
    name = cfg.default_vertical
    lanes = {**cfg.verticals, name: replace(cfg.verticals[name], abroad_terms=("Widget Analyst",))}
    verticals.set_config(replace(cfg, verticals=lanes))
    return name


class TestAbroad:
    def base(self, abroad, countries=("United States", "India")):
        return {"location_allowlist": {"countries": list(countries)},
                "sources": {"linkedin": {"abroad": abroad}}}

    def test_a_country_outside_the_allowlist_is_flagged(self, tmp_path, abroad_lane):
        lane = abroad_lane
        c = load(tmp_path, self.base({"countries": ["Germany"], "verticals": [lane]}))
        assert any("not in location_allowlist" in p for p in c.validate())

    def test_an_unknown_lane_is_flagged(self, tmp_path):
        c = load(tmp_path, self.base({"countries": ["India"], "verticals": ["nope"]}))
        assert any("'nope' is not a vertical" in p for p in c.validate())

    def test_only_jobspy_lanes_take_it(self, tmp_path):
        c = load(tmp_path, {"sources": {"lever": {"abroad": {"countries": ["India"], "verticals": ["x"]}}}})
        assert any("only" in p for p in c.validate())

    def test_abroad_lanes(self, tmp_path, abroad_lane):
        lane = abroad_lane
        c = load(tmp_path, self.base({"countries": ["India"], "verticals": [lane]}))
        assert c.abroad_lanes() == {"india": {lane}} and c.validate() == []


def test_pacing_floor():
    assert C.pacing_floor("workday") == 0.5 and C.pacing_floor("lever") == 1.0


def test_the_example_config_validates():
    cfg = C.load_config(paths.REPO_ROOT / "profile" / "discovery.example.yaml")
    assert cfg.validate() == []
    assert set(yaml.safe_load((paths.REPO_ROOT / "profile" / "discovery.example.yaml")
                              .read_text(encoding="utf-8"))["sources"]) == set(C.SOURCE_NAMES)


def test_the_cli_reports_a_bad_config(monkeypatch, tmp_path, capsys):
    p = tmp_path / "d.yaml"
    p.write_text("schema_version: 9\n", encoding="utf-8")
    monkeypatch.setattr(C, "DEFAULT_CONFIG_PATH", p)
    assert C.main() == 1 and "schema_version" in capsys.readouterr().out


def test_linkedin_companies_are_read(tmp_path):
    li = load(tmp_path, {"sources": {"linkedin": {"companies": {
        "terms": ["widget"], "ids": {"Acme": 123}}}}}).sources["linkedin"]
    assert (li.company_terms, li.company_ids) == (["widget"], {"Acme": 123})
