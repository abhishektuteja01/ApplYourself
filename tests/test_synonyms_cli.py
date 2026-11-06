"""The deterministic side of /suggest-synonyms: what may be added, what is
remembered, what is asked."""

from __future__ import annotations

import json

import pytest
import yaml

from src import synonyms_cli as S
from src.resume_model import parse_entries
from src.shared import paths
from tests.factories import scored_rows, write_clean

BULLETS = """# header comment
## B-AAA-01
canonical: Built the widget report.
tags: [widgets]
default_for: [example_primary]
evidence: production
allowable_synonyms: ["widget reporting"]

## B-AAA-02
canonical: Reviewed sprocket defects.
tags: [sprockets]
default_for: [example_primary]
evidence: production
"""
SKILLS = """## SKILL-SQL
name: SQL
evidence: B-AAA-01
allowable_synonyms: ["relational databases"]
show: {example_primary: "Programming"}
"""


@pytest.fixture
def world(tmp_path, monkeypatch):
    prof, jobs = tmp_path / "profile", tmp_path / "jobs"
    prof.mkdir()
    monkeypatch.delitem(paths._overrides, "BULLETS")
    monkeypatch.delitem(paths._overrides, "SKILLS")
    jobs.mkdir()
    (prof / "bullets.md").write_text(BULLETS, encoding="utf-8")
    (prof / "skills_master.md").write_text(SKILLS, encoding="utf-8")
    (prof / "settings.yaml").write_text(
        yaml.safe_dump({"auto_add_synonyms": True, "unlock_max_open": 2}), encoding="utf-8"
    )
    return tmp_path


def _propose(**sections) -> None:
    paths.SYNONYM_PROPOSALS.write_text(yaml.safe_dump(sections), encoding="utf-8")


def _syn(path, entry):
    return parse_entries(path.read_text(encoding="utf-8"))[entry].get("allowable_synonyms")


def test_ready_phrases_are_added_to_a_bullet_and_a_skill(world):
    _propose(
        ready=[
            {"target": "B-AAA-01", "phrase": "widget dashboards", "keyword": "dashboards"},
            {"target": "SKILL-SQL", "phrase": "RDBMS", "keyword": "rdbms"},
        ]
    )
    out = S.apply()
    assert out[0] == "added=2 declined=0 unlock=0"
    assert _syn(paths.BULLETS, "B-AAA-01") == ["widget reporting", "widget dashboards"]
    assert _syn(paths.SKILLS, "SKILL-SQL") == ["relational databases", "RDBMS"]
    log = [json.loads(x) for x in paths.SYNONYM_LOG.read_text(encoding="utf-8").splitlines()]
    assert [r["target"] for r in log] == ["B-AAA-01", "SKILL-SQL"]
    assert not paths.SYNONYM_PROPOSALS.exists()


def test_an_entry_with_no_synonym_line_gets_one_and_the_rest_is_untouched(world):
    _propose(ready=[{"target": "B-AAA-02", "phrase": "sprocket triage", "keyword": "triage"}])
    S.apply()
    assert _syn(paths.BULLETS, "B-AAA-02") == ["sprocket triage"]
    text = paths.BULLETS.read_text(encoding="utf-8")
    assert text.startswith("# header comment") and "## B-AAA-01" in text
    assert _syn(paths.BULLETS, "B-AAA-01") == ["widget reporting"]


@pytest.mark.parametrize(
    "item,why",
    [
        ({"target": "B-ZZZ-09", "phrase": "x y"}, "does not exist"),
        ({"target": "B-AAA-01", "phrase": "Widget Reporting"}, "already there"),
        ({"target": "B-AAA-01", "phrase": "Built the widget report."}, "already there"),
        ({"target": "B-AAA-01", "phrase": "one two three four five six seven eight nine"}, "words"),
        ({"target": "B-AAA-01", "phrase": "  "}, "empty"),
    ],
)
def test_a_proposal_that_fails_a_check_is_skipped_and_named(world, item, why):
    before = paths.BULLETS.read_text(encoding="utf-8")
    _propose(ready=[item])
    out = S.apply()
    assert out[0].startswith("added=0") and any(why in line for line in out)
    assert paths.BULLETS.read_text(encoding="utf-8") == before
    assert not paths.SYNONYM_HISTORY.exists()


def test_the_same_phrase_twice_in_one_run_is_added_once(world):
    item = {"target": "B-AAA-01", "phrase": "widget dashboards"}
    _propose(ready=[item, {**item, "phrase": "Widget  Dashboards"}])
    assert S.apply()[0].startswith("added=1")
    assert _syn(paths.BULLETS, "B-AAA-01").count("widget dashboards") == 1


def test_each_write_keeps_a_backup_and_only_the_last_twenty(world, monkeypatch):
    monkeypatch.setattr(S, "HISTORY_KEEP", 3)
    for i in range(5):
        _propose(ready=[{"target": "B-AAA-01", "phrase": f"phrase {i}"}])
        S.apply()
    assert len(list(paths.SYNONYM_HISTORY.glob("bullets.md.*"))) == 3


def test_with_the_switch_off_nothing_is_added_but_the_rest_is_recorded(world):
    paths.SETTINGS.write_text(
        yaml.safe_dump({"auto_add_synonyms": False, "unlock_max_open": 2}), encoding="utf-8"
    )
    before = paths.BULLETS.read_text(encoding="utf-8")
    _propose(
        ready=[{"target": "B-AAA-01", "phrase": "widget dashboards"}],
        declined=[{"keyword": "Kubernetes", "reason": "no bullet"}],
    )
    out = S.apply()
    assert "auto_add_synonyms is off" in out[1] and out[0].startswith("added=0 declined=1")
    assert paths.BULLETS.read_text(encoding="utf-8") == before


def test_a_declined_keyword_is_recorded_once_then_approved_then_skipped_by_keywords(world):
    _propose(declined=[{"keyword": "Kubernetes", "reason": "no bullet"}])
    assert S.apply()[0] == "added=0 declined=1 unlock=0"
    _propose(declined=[{"keyword": " kubernetes ", "reason": "again"}])
    assert S.apply()[0] == "added=0 declined=0 unlock=0"
    rows = yaml.safe_load(paths.SYNONYM_DECLINED.read_text(encoding="utf-8"))
    assert rows[0].pop("at")
    assert rows == [
        {
            "keyword": "Kubernetes",
            "reason": "no bullet",
            "first_seen": S._today(),
            "approved": False,
        }
    ]
    assert S.approve(["KUBERNETES", "nothing"]) == [
        "approved 'KUBERNETES'",
        "no declined keyword 'nothing'",
    ]
    assert yaml.safe_load(paths.SYNONYM_DECLINED.read_text(encoding="utf-8"))[0]["approved"] is True


def test_unlock_questions_are_deduped_capped_and_closed_for_good(world):
    q = lambda t: {"kind": "skill", "thing": t, "context": "Acme", "via": "the pipeline"}  # noqa: E731
    _propose(unlock=[q("Airflow"), q("airflow"), q("dbt"), q("Spark"), {"kind": "x", "thing": "y"}])
    out = S.apply()
    assert out[0] == "added=0 declined=0 unlock=2" and "queue full" in out[1]
    state = yaml.safe_load(paths.SYNONYM_OPEN.read_text(encoding="utf-8"))
    assert [i["thing"] for i in state["open"]] == ["Airflow", "dbt"]
    first = state["open"][0]["id"]
    assert S.answer(first, "no") == f"{first}: no"
    assert S.answer(first, "no") == f"no open question {first}"
    _propose(unlock=[q("Airflow")])
    assert S.apply()[0] == "added=0 declined=0 unlock=0"


def test_undo_removes_one_phrase_only(world):
    _propose(ready=[{"target": "B-AAA-01", "phrase": "widget dashboards"}])
    S.apply()
    assert S.undo("B-AAA-01", "Widget Dashboards") == "removed 'Widget Dashboards' from B-AAA-01"
    assert _syn(paths.BULLETS, "B-AAA-01") == ["widget reporting"]
    assert "has no" in S.undo("B-AAA-01", "widget dashboards")


def test_keywords_count_only_roles_without_a_resume_and_skip_declined(world, monkeypatch):
    monkeypatch.setattr(S, "_new_job_ids", lambda: ["a1", "b2"])
    scored_rows(
        {"job_id": "a1", "fit_score": 90, "keywords_to_mirror": ["RAG", "Airflow"]},
        {"job_id": "b2", "fit_score": 80, "keywords_to_mirror": ["rag", "Kubernetes"]},
        {"job_id": "c3", "fit_score": 99, "keywords_to_mirror": ["RAG", "Spark"]},
    ).to_parquet(paths.SCORED)
    write_clean(
        paths.CLEAN,
        *(
            {"job_id": j, "company": c}
            for j, c in (("a1", "Acme"), ("b2", "Beta"), ("c3", "Gamma"))
        ),
    )
    paths.SYNONYM_DECLINED.parent.mkdir(parents=True, exist_ok=True)
    paths.SYNONYM_DECLINED.write_text(
        yaml.safe_dump(
            [{"keyword": "kubernetes", "reason": "", "first_seen": "2026-09-01", "approved": False}]
        ),
        encoding="utf-8",
    )
    got = S.keywords()
    assert got["job_ids"] == ["a1", "b2"]
    assert [(k["keyword"], k["frequency"]) for k in got["keywords"]] == [("RAG", 2), ("Airflow", 1)]
    assert got["keywords"][0]["company"] == "Acme"


def test_keywords_with_no_new_roles_is_empty(world, monkeypatch):
    monkeypatch.setattr(S, "_new_job_ids", lambda: [])
    assert S.keywords() == {"job_ids": [], "keywords": []}


def test_the_cli_reports_a_missing_config_key(world, capsys):
    paths.SETTINGS.write_text(yaml.safe_dump({"auto_add_synonyms": True}), encoding="utf-8")
    _propose()
    assert S.main(["apply"]) == 1
    assert "unlock_max_open" in capsys.readouterr().err
