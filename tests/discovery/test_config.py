"""config.py: loading, key checks and validate()."""

from __future__ import annotations

import pytest
import yaml

from applyourself.discovery import config as C
from applyourself.shared import paths


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


@pytest.mark.parametrize(
    "data,needle",
    [
        ({"raw_retention_days": 16}, "raw_retention_days (removed; use raw_keep_days)"),
        (
            {"sources": {"workday": {"cold_rotation_runs": 3}}},
            "sources.workday.cold_rotation_runs (removed; use every_days)",
        ),
        (
            {"location_allowlist": {"countries": ["United States"], "states": ["TX"]}},
            "location_allowlist.states",
        ),
        ({"retention": {"highdays": 60}}, "retention.highdays"),
        (
            {"sources": {"linkedin": {"abroad": {"countries": ["India"]}}}},
            "sources.linkedin.abroad (removed; use the top-level abroad:",
        ),
        ({"abroad": {"countries": ["India"]}}, "abroad.countries"),
        (
            {"sources": {"linkedin": {"companies": {"term": ["AI"]}}}},
            "sources.linkedin.companies.term",
        ),
        ({"typo": 1}, "unknown config key: typo"),
    ],
)
def test_unknown_and_retired_keys_are_named(tmp_path, data, needle):
    assert any(needle in p for p in load(tmp_path, data).validate())


def test_every_value_is_read(tmp_path, monkeypatch):

    scoring = tmp_path / "scoring.yaml"
    cutoffs = {"review": 50, "tailor": 60, "keep_long": 75, "sheet": 85}
    scoring.write_text(yaml.safe_dump({"cutoffs": cutoffs}), encoding="utf-8")
    monkeypatch.setitem(paths._overrides, "SCORING", scoring)
    cfg = load(
        tmp_path,
        {
            "deadline_hours": 6,
            "live_days": 10,
            "search_max_age_days": 7,
            "forget_after_days": 90,
            "raw_keep_days": 2,
            "log_keep_days": 9,
            "dead_every_days": 7,
            "retention": {"high_days": 45, "low_days": 10},
            "sources": {
                "workday": {"every_days": 3, "max_pages": 4, "pacing_seconds": 0.5},
                "linkedin": {"hours_old": 72, "results_wanted": 50, "remote_queries": False},
                "stapply": {"max_snapshot_age_hours": 30},
            },
        },
    )
    assert (cfg.deadline_hours, cfg.live_days, cfg.search_max_age_days) == (6, 10, 7)
    assert (cfg.raw_keep_days, cfg.log_keep_days, cfg.dead_every_days) == (2, 9, 7)
    assert (cfg.retention_score_threshold, cfg.retention_high_days, cfg.retention_low_days) == (
        75,
        45,
        10,
    )
    w, li = cfg.sources["workday"], cfg.sources["linkedin"]
    assert (w.every_days, w.max_pages, li.hours_old, li.results_wanted, li.remote_queries) == (
        3,
        4,
        72,
        50,
        False,
    )
    assert cfg.sources["stapply"].max_snapshot_age_hours == 30
    assert cfg.validate() == []


def test_results_wanted_is_per_source_and_linkedin_stops_at_its_paging_limit(tmp_path):
    cfg = load(tmp_path, {"sources": {"indeed": {"results_wanted": 1500}}})
    assert (cfg.sources["linkedin"].results_wanted, cfg.sources["indeed"].results_wanted) == (
        100,
        1500,
    )
    assert cfg.validate() == []
    assert load(tmp_path, {"sources": {"linkedin": {"results_wanted": 140}}}).validate() == []
    cfg = load(tmp_path, {"sources": {"linkedin": {"results_wanted": 141}}})
    assert any("sources.linkedin.results_wanted (141)" in p for p in cfg.validate())


def test_terms_per_lane_is_refused_where_nothing_reads_it(tmp_path):
    assert load(tmp_path, {"sources": {"eightfold": {"terms_per_lane": 1}}}).validate() == []
    with pytest.raises(ValueError, match="terms_per_lane is read only by"):
        load(tmp_path, {"sources": {"greenhouse": {"terms_per_lane": 2}}})


def test_retention_from_defaults_to_first_and_rejects_anything_else(tmp_path):
    assert load(tmp_path, {}).retention_from == "first"
    assert load(tmp_path, {"retention_from": "newest"}).retention_from == "newest"
    with pytest.raises(ValueError, match="retention_from must be one of first, newest"):
        load(tmp_path, {"retention_from": "latest"})


@pytest.mark.parametrize("data", [{}, {"search_max_age_days": None}])
def test_search_max_age_days_absent_or_null_is_no_cut(tmp_path, data):
    assert load(tmp_path, data).search_max_age_days is None


@pytest.mark.parametrize(
    "data",
    [
        {"live_days": 0},
        {"retention": {"low_days": -1}},
        {"sources": {"workday": {"every_days": 0}}},
        {"sources": {"linkedin": {"page_delay_seconds": 0.1}}},
        {"sources": {"linkedin": {"remote_queries": "no"}}},
        {"abroad": {"lanes": "x"}},
        {"search_locations": "United States"},
        {"sources": {"stapply": {"max_snapshot_age_hours": 0}}},
        {"sources": {"linkedin": {"companies": {"terms": ["AI"], "ids": {"Acme": "123"}}}}},
        {"sources": {"linkedin": {"companies": {"ids": {"Acme": 123}}}}},
        {"sources": {"indeed": {"companies": {"terms": ["AI"], "ids": {"Acme": 123}}}}},
    ],
)
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
    assert any(
        "search_locations" in p
        for p in load(tmp_path, {"location_allowlist": {"countries": many}}).validate()
    )
    assert (
        load(
            tmp_path,
            {"location_allowlist": {"countries": many}, "search_locations": ["United States"]},
        ).validate()
        == []
    )


@pytest.fixture
def abroad_lane(cfg):
    from dataclasses import replace

    from applyourself.shared import verticals

    name = cfg.default_vertical
    lanes = {**cfg.verticals, name: replace(cfg.verticals[name], abroad_terms=("Widget Analyst",))}
    verticals.set_config(replace(cfg, verticals=lanes))
    return name


class TestAbroad:
    """Abroad searches every country the answer bank lists beyond your own."""

    @pytest.fixture(autouse=True)
    def bank(self):
        data = yaml.safe_load(
            paths.example("application_answers.example.yaml").read_text(encoding="utf-8")
        )
        rows = data["work_authorization"]["countries"]
        rows["India"] = rows["Germany"] = {"authorized": False, "requires_sponsorship": True}
        paths.ANSWER_BANK.parent.mkdir(parents=True, exist_ok=True)
        paths.ANSWER_BANK.write_text(yaml.safe_dump(data), encoding="utf-8")

    def base(self, lanes, sources=("linkedin",), **extra):
        return {"abroad": {"lanes": list(lanes), "sources": list(sources)}, **extra}

    def test_the_bank_is_the_country_list(self, tmp_path, abroad_lane):
        c = load(tmp_path, self.base([abroad_lane]))
        assert c.location_allowlist.effective_countries() == {"United States", "India", "Germany"}
        assert c.abroad_lanes() == {"india": {abroad_lane}, "germany": {abroad_lane}}
        assert c.sources["indeed"].abroad_countries == [] and c.validate() == []

    def test_a_country_outside_the_allowlist_is_flagged(self, tmp_path, abroad_lane):
        allow = {"countries": ["United States", "India"]}
        c = load(tmp_path, self.base([abroad_lane], location_allowlist=allow))
        assert any("'Germany' is not in location_allowlist" in p for p in c.validate())

    def test_an_unknown_lane_is_flagged(self, tmp_path):
        c = load(tmp_path, self.base(["nope"]))
        assert any("'nope' is not a vertical" in p for p in c.validate())

    def test_only_jobspy_lanes_take_it(self, tmp_path, abroad_lane):
        c = load(tmp_path, self.base([abroad_lane], sources=["lever"]))
        assert any("only" in p for p in c.validate())


def test_pacing_floor():
    assert C.pacing_floor("workday") == 0.5 and C.pacing_floor("lever") == 1.0


def test_the_example_config_validates():
    cfg = C.load_config(paths.REPO_ROOT / "profile" / "discovery.example.yaml")
    assert cfg.validate() == []
    assert set(
        yaml.safe_load(
            (paths.REPO_ROOT / "profile" / "discovery.example.yaml").read_text(encoding="utf-8")
        )["sources"]
    ) == set(C.SOURCE_NAMES)


def test_the_cli_reports_a_bad_config(monkeypatch, tmp_path, capsys):
    p = tmp_path / "d.yaml"
    p.write_text("schema_version: 9\n", encoding="utf-8")
    monkeypatch.setitem(paths._overrides, "DISCOVERY_CONFIG", p)
    assert C.main() == 1 and "schema_version" in capsys.readouterr().out


def test_linkedin_companies_are_read(tmp_path):
    li = load(
        tmp_path,
        {"sources": {"linkedin": {"companies": {"terms": ["widget"], "ids": {"Acme": 123}}}}},
    ).sources["linkedin"]
    assert (li.company_terms, li.company_ids) == (["widget"], {"Acme": 123})


def test_no_scoring_yaml_fails_naming_it_instead_of_a_default_cutoff(tmp_path, monkeypatch):
    monkeypatch.setitem(paths._overrides, "SCORING", tmp_path / "scoring.yaml")
    with pytest.raises(FileNotFoundError, match=r"scoring\.yaml not found"):
        C.load_config(tmp_path / "nope.yaml")
