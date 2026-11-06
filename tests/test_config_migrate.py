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
