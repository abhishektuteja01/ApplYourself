"""`config migrate`: an older profile layout moves onto the current one, once."""

import copy
import shutil
from argparse import Namespace

import pytest
import yaml

from applyourself import config_migrate, score_filter
from applyourself.shared import paths


def _profile(tmp_path):
    """The example files under their real names, in a tmp profile/."""
    prof = tmp_path / "prof"
    prof.mkdir()
    for name in ("scoring", "application_answers"):
        shutil.copyfile(paths.example(f"{name}.example.yaml"), prof / f"{name}.yaml")
    return prof


def _run(prof, apply):
    return config_migrate.main(Namespace(profile=str(prof), apply=apply))


def test_old_languages_key_fails_with_where_it_moved(tmp_path):
    prof = _profile(tmp_path)
    s = prof / "scoring.yaml"
    s.write_text(s.read_text(encoding="utf-8") + "languages: [English]\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"moved to application_answers\.yaml questions"):
        score_filter.load_scoring(s, prof / "application_answers.yaml")


def test_languages_move_to_the_bank_once(tmp_path, capsys):
    prof = _profile(tmp_path)
    bank = yaml.safe_load((prof / "application_answers.yaml").read_text(encoding="utf-8"))
    del bank["questions"]["languages_fluent"]
    (prof / "application_answers.yaml").write_text(
        yaml.safe_dump(bank, sort_keys=False), encoding="utf-8"
    )
    s = prof / "scoring.yaml"
    s.write_text(
        s.read_text(encoding="utf-8") + "\n# Languages you speak.\nlanguages: [English, Hindi]\n",
        encoding="utf-8",
    )
    before = s.read_text(encoding="utf-8")

    assert _run(prof, apply=False) == 0
    assert "-languages: [English, Hindi]" in capsys.readouterr().out
    assert s.read_text(encoding="utf-8") == before
    assert _run(prof, apply=True) == 0
    assert "Languages you speak" not in s.read_text(encoding="utf-8")
    assert score_filter.load_scoring(s, prof / "application_answers.yaml").languages == (
        "English",
        "Hindi",
    )
    capsys.readouterr()
    assert _run(prof, apply=False) == 0
    assert "nothing to migrate" in capsys.readouterr().out


def test_languages_that_disagree_with_the_bank_stop(tmp_path, capsys):
    prof = _profile(tmp_path)
    s = prof / "scoring.yaml"
    s.write_text(s.read_text(encoding="utf-8") + "languages: [German]\n", encoding="utf-8")
    assert _run(prof, apply=True) == 1
    assert "differ" in capsys.readouterr().out
    assert "languages: [German]" in s.read_text(encoding="utf-8")


def test_source_abroad_blocks_become_one_top_level_block(tmp_path, cfg, monkeypatch, capsys):
    from applyourself.discovery.config import load_config

    prof = _profile(tmp_path)
    b = prof / "application_answers.yaml"
    bank = yaml.safe_load(b.read_text(encoding="utf-8"))
    no = {"authorized": False, "requires_sponsorship": True}
    bank["work_authorization"]["countries"]["India"] = no
    b.write_text(yaml.safe_dump(bank, sort_keys=False), encoding="utf-8")
    lane = cfg.default_vertical
    d = prof / "discovery.yaml"
    d.write_text(
        "schema_version: 2\n"
        'location_allowlist:\n  countries: ["United States",\n              "India"]\n'
        "# Every source.\n"
        "sources:\n"
        "  linkedin: {enabled: true, pacing_seconds: 3,\n"
        f'             abroad: &abroad {{countries: ["India"], verticals: [{lane}]}},\n'
        "             remote_queries: false}\n"
        "  indeed:   {pacing_seconds: 2, abroad: *abroad}\n",
        encoding="utf-8",
    )
    assert _run(prof, apply=True) == 0
    text = d.read_text(encoding="utf-8")
    assert "location_allowlist" not in text and "&abroad" not in text
    assert f"abroad: {{lanes: [{lane}], sources: [linkedin, indeed]}}" in text
    monkeypatch.setitem(paths._overrides, "ANSWER_BANK", b)
    got = load_config(d)
    assert got.sources["linkedin"].remote_queries is False
    assert got.abroad_lanes() == {"india": {lane}}
    assert got.sources["indeed"].abroad_countries == ["India"]
    capsys.readouterr()
    assert _run(prof, apply=False) == 0
    assert "nothing to migrate" in capsys.readouterr().out


def test_lane_salary_moves_onto_the_bank_rows(tmp_path):
    from applyourself import apply_cli
    from applyourself.apply.bank_schema import load_answers

    prof = _profile(tmp_path)
    v = prof / "verticals.yaml"
    v.write_text(
        "verticals:\n"
        "  widgets:\n"
        "    display_name: Widgets\n"
        "    salary_expectation:\n"
        "      markup_pct: 10\n"
        "      fallback:\n"
        "        United States: 110000\n"
        "\n"
        "  gizmos:\n"
        "    display_name: Gizmos\n",
        encoding="utf-8",
    )
    assert _run(prof, apply=True) == 0
    assert v.read_text(encoding="utf-8") == (
        "verticals:\n  widgets:\n    display_name: Widgets\n\n  gizmos:\n    display_name: Gizmos\n"
    )
    bank = load_answers(prof / "application_answers.yaml")
    assert bank.facts_for("United States").salary.lanes == ("widgets",)
    assert apply_cli.salary_for("x", "widgets", "United States", bank) == 110000.0
    assert apply_cli.salary_for("x", "gizmos", "United States", bank) is None


def test_schema_2_bank_drops_what_the_resume_says(tmp_path, capsys):
    from applyourself.apply.bank_schema import load_answers

    prof = _profile(tmp_path)
    shutil.copyfile(paths.example("resume.example.yaml"), prof / "resume.yaml")
    b = prof / "application_answers.yaml"
    before = load_answers(b)
    bank = yaml.safe_load(b.read_text(encoding="utf-8"))
    bank["schema_version"] = 2
    bank["identity"] = dict(before.identity)
    bank["education"] = dict(before.education, start_year=2019)
    bank["employment"] = {**before.employment, "start_year": 2023}
    b.write_text(yaml.safe_dump(bank, sort_keys=False), encoding="utf-8")

    assert _run(prof, apply=True) == 0
    assert "identity.location kept" in capsys.readouterr().out
    after = yaml.safe_load(b.read_text(encoding="utf-8"))
    assert after["schema_version"] == 4 and "first_name" not in after["identity"]
    assert after["employment"] == {"only_when_required": True}
    got = load_answers(b)
    assert (got.identity, got.education, got.employment) == (
        before.identity,
        before.education,
        before.employment,
    )


def test_schema_3_bank_keeps_every_answer_and_its_comments(tmp_path, capsys):
    from applyourself.apply.bank_schema import load_answers

    prof = _profile(tmp_path)
    b = prof / "application_answers.yaml"
    bank = yaml.safe_load(b.read_text(encoding="utf-8"))
    bank["schema_version"] = 3
    bank["claims"] = {"I have OPT": {"authorized": True}}
    bank["fact_wordings"] = {
        "Are you authorized to work?": {"fact": "authorized", "yes_when": True}
    }
    for qid, spec in bank["questions"].items():
        spec["wordings"] = [f"{qid} wording", "two"]
        spec["keywords"] = [qid]
    bank["questions"]["linkedin"]["not"] = ["never this"]
    text = yaml.safe_dump(bank, sort_keys=False).replace(
        "questions:\n", "# my questions\nquestions:\n  # the profile links\n"
    )
    b.write_text(text, encoding="utf-8")

    assert _run(prof, apply=True) == 0
    assert "questions and" in capsys.readouterr().out
    after = b.read_text(encoding="utf-8")
    assert "# my questions" in after and "# the profile links" in after
    got = yaml.safe_load(after)
    assert got["schema_version"] == 4 and "claims" not in got and "fact_wordings" not in got
    assert {q: s["answer"] for q, s in got["questions"].items()} == {
        q: s["answer"] for q, s in bank["questions"].items()
    }
    assert got["facts"] == bank["facts"]
    load_answers(b)
    history = list((prof / ".state" / "bank_history").glob("application_answers.*.yaml"))
    assert [yaml.safe_load(h.read_text(encoding="utf-8"))["schema_version"] for h in history] == [3]


def test_automation_yaml_becomes_settings_with_the_bank_browser(tmp_path, capsys):
    from applyourself.apply.bank_schema import (
        AnswersError,
        load_answers,
    )
    from applyourself.apply.browser import load_browser_config
    from applyourself.shared.config_schema import load_settings

    prof = _profile(tmp_path)
    shutil.copyfile(paths.example("resume.example.yaml"), prof / "resume.yaml")
    settings = paths.example("settings.example.yaml").read_text(encoding="utf-8")
    old = settings[: settings.index("# Which browser")].rstrip("\n") + "\n"
    (prof / "automation.yaml").write_text(old, encoding="utf-8")
    b = prof / "application_answers.yaml"
    bank = yaml.safe_load(b.read_text(encoding="utf-8"))
    bank["browser"] = {"driver": "patchright", "stealth": True}
    b.write_text(yaml.safe_dump(bank, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match=r"automation\.yaml moved to .*settings\.yaml"):
        load_settings(prof / "settings.yaml")
    with pytest.raises(AnswersError, match=r"browser: moved to profile/settings\.yaml"):
        load_answers(b)

    assert _run(prof, apply=True) == 0
    assert not (prof / "automation.yaml").exists()
    assert load_browser_config(prof / "settings.yaml") == ("patchright", True)
    load_answers(b)
    capsys.readouterr()
    assert _run(prof, apply=False) == 0
    assert "nothing to migrate" in capsys.readouterr().out


@pytest.mark.parametrize("edited", [False, True])
def test_tool_state_moves_under_dot_state(tmp_path, capsys, edited):
    prof = _profile(tmp_path)
    (prof / ".bank_history").mkdir()
    (prof / ".bank_history" / "application_answers.2026-01-01.yaml").write_text(
        "x", encoding="utf-8"
    )
    (prof / ".synonym_history").mkdir()
    (prof / ".synonym_history" / "bullets.md.1").write_text("y", encoding="utf-8")
    (prof / "synonyms_declined.yaml").write_text("[]\n", encoding="utf-8")
    rules = paths.default("de_ai_rules.yaml").read_text(encoding="utf-8")
    (prof / "de_ai_rules.yaml").write_text(rules + ("# mine\n" if edited else ""), encoding="utf-8")

    assert _run(prof, apply=True) == 0
    assert (prof / ".state" / "bank_history" / "application_answers.2026-01-01.yaml").is_file()
    assert (prof / ".state" / "synonym_history" / "bullets.md.1").is_file()
    assert (prof / ".state" / "synonyms_declined.yaml").is_file()
    assert not (prof / ".bank_history").exists() and not (prof / "synonyms_declined.yaml").exists()
    assert (prof / "de_ai_rules.yaml").exists() is edited
    capsys.readouterr()
    assert _run(prof, apply=False) == 0
    assert "nothing to migrate" in capsys.readouterr().out


def test_inline_lanes_split_into_lane_files_with_the_same_config(tmp_path, capsys):
    from applyourself.shared import verticals as V

    example = paths.example("verticals.example.yaml")
    top = yaml.safe_load(example.read_text(encoding="utf-8"))
    lanes = {
        n: copy.deepcopy(
            V.merge_defaults(
                top["defaults"],
                yaml.safe_load(V.lane_file(example, n).read_text(encoding="utf-8")),
            )
        )
        for n in top["lanes"]
    }
    old = {
        "schema_version": 1,
        "default_vertical": top["default_vertical"],
        "verticals": lanes,
        "classifier_rules": top["classifier_rules"],
    }
    prof = tmp_path / "prof"
    prof.mkdir()
    v = prof / "verticals.yaml"
    v.write_text(yaml.safe_dump(old, sort_keys=False, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="config migrate"):
        V.load_verticals(v)
    before = V.build(old, {n: (raw, v) for n, raw in lanes.items()}, v)

    assert _run(prof, apply=True) == 0
    new = yaml.safe_load(v.read_text(encoding="utf-8"))
    assert new["lanes"] == list(lanes) and new["defaults"] == top["defaults"]
    assert config_migrate._shape(V.load_verticals(v)) == config_migrate._shape(before)
    capsys.readouterr()
    assert _run(prof, apply=False) == 0
    assert "nothing to migrate" in capsys.readouterr().out


def test_lane_title_lists_move_out_and_nothing_that_passed_drops(tmp_path, capsys):
    from applyourself.shared import title_gate
    from applyourself.shared import verticals as V
    from tests.conftest import FIXTURE_PATH

    prof = tmp_path / "prof"
    prof.mkdir()
    shutil.copyfile(FIXTURE_PATH, prof / "verticals.yaml")
    shutil.copytree(FIXTURE_PATH.parent / "verticals", prof / "verticals")
    old = V.load_verticals(prof / "verticals.yaml")
    titles = [
        "Widget Platform Analyst",
        "Doohickey Analyst",
        "Quantitative Sprocket Analyst",
        "Sprocket Risk Management Operations",
        "Flywheel Cog Engineer",
    ]
    passed = [
        (t, n)
        for t in titles
        for n in old.names
        if title_gate.lane_for(t, n, "linkedin", old).passes
    ]

    assert _run(prof, apply=True) == 0
    capsys.readouterr()
    new = V.load_verticals(prof / "verticals.yaml")
    for lane in new.verticals.values():
        assert not (lane.title_include_terms or lane.title_strong_keep_terms)
    assert ("example_secondary", True) in {
        (v, isinstance(r, V.TermsRule)) for v, r in new.classifier_rules
    }
    assert [p for p in passed if not title_gate.lane_for(*p, "linkedin", new).passes] == []
    assert _run(prof, apply=False) == 0
    assert "nothing to migrate" in capsys.readouterr().out


TWO_LANES = """schema_version: 2
default_vertical: alpha
lanes:
  - alpha
  - beta
classifier_rules:
  - vertical: alpha
    pattern: '\\bwidget\\b'
  - vertical: beta
    pattern: '\\bgizmo\\b'
"""
LANE = """display_name: {name}
search_terms: [{search}]
linkedin_terms: [{search}]
title_include_terms: {include}
title_exclude_terms: [thesis, sales]
title_strong_keep_terms: {strong}
"""


def _two_lanes(tmp_path, raw_titles=()):
    """A block-list `lanes:` and no `defaults:`; a ban both lanes share that a
    strong-keep title holds (sales) and one it does not (thesis); a single word
    in one lane (agent) beside a longer term in the other (agent risk)."""
    import pandas as pd

    prof = tmp_path / "profile"
    for name, search, include, strong in (
        ("alpha", "Data Platform", "[agent]", "[sales widget]"),
        ("beta", "Gizmo Analyst", "[agent risk, data platform risk]", "[]"),
    ):
        lane = prof / "verticals" / name / "lane.yaml"
        lane.parent.mkdir(parents=True)
        lane.write_text(
            LANE.format(name=name, search=search, include=include, strong=strong),
            encoding="utf-8",
        )
    (prof / "verticals.yaml").write_text(TWO_LANES, encoding="utf-8")
    raw = tmp_path / "jobs" / "raw"
    raw.mkdir(parents=True)
    rows = [{"source": "greenhouse", "title": t, "vertical": "alpha", "passed": True,
             "posting_key": t} for t in ("Sales Widget Lead", *raw_titles)]  # fmt: skip
    pd.DataFrame(rows).to_parquet(raw / "2026-11-04_2100.parquet")
    return prof


def test_title_lists_migration_keeps_strong_keep_and_routes_specific_first(tmp_path, capsys):
    from applyourself.shared import title_gate
    from applyourself.shared import verticals as V

    prof = _two_lanes(tmp_path)
    assert _run(prof, apply=True) == 0, capsys.readouterr().out
    new = V.load_verticals(prof / "verticals.yaml")
    ban = new.verticals["beta"].title_exclude_terms
    assert "thesis" in ban and "sales" in ban  # every ban both lanes share
    assert title_gate.lane_for("Thesis: Sales Widget", "", "greenhouse", new).passes  # keep
    assert title_gate.lane_for("Gizmo Sales", "", "greenhouse", new).reason == "banned: sales"
    assert "lane decisions NOT checked" not in capsys.readouterr().out
    route = {t: title_gate.lane_for(t, "", "greenhouse", new).lane for t in (
        "Agent Risk Analyst", "Insurance Agent", "Data Platform Risk Lead")}  # fmt: skip
    assert route == {
        "Agent Risk Analyst": "beta",
        "Insurance Agent": "",
        "Data Platform Risk Lead": "beta",
    }
    assert title_gate.lane_for("Agent Ops", "alpha", "linkedin", new).lane == "alpha"


def test_title_lists_migration_says_so_when_there_is_nothing_to_replay(tmp_path, capsys):
    import shutil as sh

    prof = _two_lanes(tmp_path)
    sh.rmtree(tmp_path / "jobs")
    assert _run(prof, apply=False) == 0
    assert "lane decisions NOT checked" in capsys.readouterr().out


def test_title_lists_migration_refuses_when_a_title_would_change_lane(tmp_path, capsys):
    prof = _two_lanes(tmp_path, raw_titles=("Data Platform Risk Lead",))
    before = (prof / "verticals.yaml").read_text(encoding="utf-8")
    assert _run(prof, apply=True) == 1
    out = capsys.readouterr().out
    assert "would drop or move 1 title" in out and "alpha -> beta" in out
    assert (prof / "verticals.yaml").read_text(encoding="utf-8") == before


def test_scoring_v2_moves_the_sheet_line_to_the_lanes_and_drops_the_old_judge_keys(
    tmp_path, capsys
):
    """An old profile: defaults `years`, a lane's skill_weights and focus,
    scoring.yaml's cutoffs.sheet and skills_cap. The sheet and retention
    lines stay where they were."""
    from applyourself.shared import verticals as V

    prof = _profile(tmp_path)
    shutil.copytree(paths.example("verticals"), prof / "verticals")
    top = paths.example("verticals.example.yaml").read_text(encoding="utf-8")
    i, j = top.index("  # /score:"), top.index("    cutoff: 76\n") + len("    cutoff: 76\n")
    (prof / "verticals.yaml").write_text(
        top[:i] + "  years: {3: 14, 4: 10}\n" + top[j:], encoding="utf-8"
    )
    lane = prof / "verticals" / "example_primary" / "lane.yaml"
    lane.write_text(
        lane.read_text(encoding="utf-8")
        + "# Skill weights.\nskill_weights:\n  widgets: {widget_assembly: 10}\n"
        + "focus:\n  question: Widgets?\n  yes_min: 18\n  no_max: 17\n",
        encoding="utf-8",
    )
    s = prof / "scoring.yaml"
    s.write_text(
        s.read_text(encoding="utf-8")
        .replace("  min_words: 300\n", "  min_words: 300\n  skills_cap: 12\n")
        .replace("  tailor: 60\n", "  tailor: 60\n  keep_long: 80\n  sheet: 85\n"),
        encoding="utf-8",
    )
    assert _run(prof, apply=True) == 0, capsys.readouterr().out
    cfg = V.load_verticals(prof / "verticals.yaml")
    assert {v.score.cutoff for v in cfg.verticals.values()} == {85}
    assert cfg.verticals["example_primary"].score.level == V.score_defaults()["level"]
    data = yaml.safe_load(s.read_text(encoding="utf-8"))
    assert data["cutoffs"] == {"review": 50, "tailor": 60, "keep_long": 80}
    assert data["jd_quality"] == {"min_words": 300}  # no fit_cap: no cap
    text = lane.read_text(encoding="utf-8")
    assert "skill_weights" not in text and "focus" not in text and "Skill weights" not in text
    v = yaml.safe_load((prof / "verticals.yaml").read_text(encoding="utf-8"))
    assert "years" not in v["defaults"]
    capsys.readouterr()
    assert _run(prof, apply=False) == 0
    assert "nothing to migrate" in capsys.readouterr().out


def test_scoring_v2_waits_for_an_old_judges_open_run(tmp_path, capsys):
    prof = _profile(tmp_path)
    s = prof / "scoring.yaml"
    s.write_text(
        s.read_text(encoding="utf-8").replace("  tailor: 60\n", "  tailor: 60\n  sheet: 85\n"),
        encoding="utf-8",
    )
    shutil.copytree(paths.example("verticals"), prof / "verticals")
    shutil.copyfile(paths.example("verticals.example.yaml"), prof / "verticals.yaml")
    staging = tmp_path / "jobs" / "scored.staging"
    staging.mkdir(parents=True)
    (staging / "run.json").write_text('{"schema_version": 1, "slices": {}}', encoding="utf-8")
    assert _run(prof, apply=True) == 1
    assert "run `score start` first" in capsys.readouterr().out
    assert "sheet: 85" in s.read_text(encoding="utf-8")
