"""`config migrate`: an older profile layout moves onto the current one, once."""

import shutil
from argparse import Namespace

import pytest
import yaml

from src import config_migrate, score_filter
from src.shared import paths


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
    from src.discovery.config import load_config

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
    from src import apply_cli
    from src.apply.answers import load_answers

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
    from src.apply.answers import load_answers

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
    assert after["schema_version"] == 3 and "first_name" not in after["identity"]
    assert after["employment"] == {"only_when_required": True}
    got = load_answers(b)
    assert (got.identity, got.education, got.employment) == (
        before.identity,
        before.education,
        before.employment,
    )


def test_automation_yaml_becomes_settings_with_the_bank_browser(tmp_path, capsys):
    from src.apply.answers import AnswersError, load_answers
    from src.apply.browser import load_browser_config
    from src.shared.config_schema import load_settings

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
    from src.shared import verticals as V

    example = paths.example("verticals.example.yaml")
    top = yaml.safe_load(example.read_text(encoding="utf-8"))
    lanes = {
        n: V.merge_defaults(
            top["defaults"], yaml.safe_load(V.lane_file(example, n).read_text(encoding="utf-8"))
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
    assert new["lanes"] == list(lanes) and new["defaults"] == {"years": top["defaults"]["years"]}
    assert config_migrate._shape(V.load_verticals(v)) == config_migrate._shape(before)
    capsys.readouterr()
    assert _run(prof, apply=False) == 0
    assert "nothing to migrate" in capsys.readouterr().out
