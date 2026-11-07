"""The judge packet's profile-built parts (src/applyourself/score_packet.py)."""

from __future__ import annotations

from datetime import date

import pytest
import yaml

from applyourself import resume_model, score_packet
from applyourself.score_filter import load_scoring
from applyourself.shared import paths


@pytest.fixture
def scfg():
    return load_scoring(paths.example("scoring.example.yaml"))


def test_digest_holds_every_claim_and_skill_and_counts_present_to_the_day(scfg):
    resume = yaml.safe_load(paths.RESUME.read_text(encoding="utf-8"))
    resume["contexts"]["COG"]["dates"] = "Nov 2025 - Present"
    paths.RESUME.write_text(yaml.safe_dump(resume, sort_keys=False), encoding="utf-8")
    src = resume_model.load_sources()
    text = score_packet.digest(scfg, date(2026, 2, 10))
    assert all(" ".join(b["canonical"].split()) in text for b in src.bullets.values())
    assert all(s["name"] in text for s in src.skills.values())
    assert (
        "Widget Assembly Operations (widget assembly, assembly operations, line operations);"
        in text
    )
    assert "throughput reporting" not in text  # a bullet's rephrasing stays out
    assert "Cog Assistant, maintenance question answering, Nov 2025 - Present (0.3 years)" in text
    assert "- Work: Widget Operations Analyst, Widget Corp, Jan 2023 - Jun 2025 (2.5 years)" in text
    assert "- United States: authorized now, will need sponsorship later" in text


def test_the_profile_form_does_not_move_with_the_month(scfg):
    resume = yaml.safe_load(paths.RESUME.read_text(encoding="utf-8"))
    resume["contexts"]["COG"]["dates"] = "Nov 2025 - Present"
    paths.RESUME.write_text(yaml.safe_dump(resume, sort_keys=False), encoding="utf-8")
    feb, may = date(2026, 2, 10), date(2026, 5, 10)
    assert score_packet.digest(scfg, feb) != score_packet.digest(scfg, may)
    same = [score_packet.digest(scfg, d, open_lengths=False) for d in (feb, may)]
    assert same[0] == same[1] and "Nov 2025 - Present\n" in same[0]


def test_no_languages_listed_is_said_so(scfg):
    import dataclasses

    text = score_packet.digest(dataclasses.replace(scfg, languages=()), date(2026, 2, 10))
    assert "- Languages: not listed (a language requirement is not a blocker)" in text


def test_a_zeroed_old_row_is_never_an_out_of_lane_example(scfg, tmp_path):
    import json

    from tests.factories import scored_rows, write_clean

    jd = "Widget assembly reporting in SQL. " * 80
    write_clean(tmp_path / "clean.parquet", *({"job_id": j, "jd_text": jd} for j in "abc"))
    axes = {"title": 25, "skills": 25, "seniority": 18, "domain": 15}
    zero = dict.fromkeys(axes, 0)
    rows = scored_rows(
        # a v1 answer zeroed by a rule: in lane by its raw axes
        {"job_id": "a", "vertical": "example_primary", "fit_score": 0.0,
         "fit_subscores": json.dumps(zero), "judge_answer": json.dumps({"job_id": "a", **axes})},
        # a legacy row zeroed by a rule: every stored axis 0
        {"job_id": "b", "vertical": "example_primary", "fit_score": 0.0,
         "fit_subscores": json.dumps(zero)},
        # truly out of lane: title 0, the rest judged
        {"job_id": "c", "vertical": "example_primary", "fit_score": 30.0,
         "fit_subscores": json.dumps({**axes, "title": 0})},
    )  # fmt: skip
    got = score_packet.candidates(
        rows, "example_primary", scfg, tmp_path / "none.parquet", tmp_path / "clean.parquet"
    )
    assert [(c["job_id"], c["in_lane"]) for c in got] == [("c", False)]


def test_examples_pick_by_a_stable_rule_and_a_bad_file_fails_loud(tmp_path, monkeypatch):
    cands = [
        {"job_id": j, "in_lane": lane, "fit": fit}
        for j, lane, fit in [
            ("bbbbbbbb", True, 92),
            ("aaaaaaaa", True, 94),  # ties 92 on distance: the lower job_id wins
            ("cccccccc", True, 80),
            ("dddddddd", True, 66),
            ("eeeeeeee", False, 40),
            ("ffffffff", False, 55),
        ]
    ]
    assert [c["job_id"] for c in score_packet.choose(cands)] == [
        "aaaaaaaa",
        "cccccccc",
        "dddddddd",
        "ffffffff",
    ]
    monkeypatch.setitem(paths._overrides, "VERTICALS_DIR", tmp_path)
    (tmp_path / "lane").mkdir()
    score_packet.examples_path("lane").write_text(
        yaml.safe_dump({"examples": [{"title": "X", "in_lane": True, "fit": 101}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"examples\[0\] needs title, in_lane"):
        score_packet.examples_text("lane")
