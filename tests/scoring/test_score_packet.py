"""The judge packet's profile-built parts (src/applyourself/score_packet.py)."""

from __future__ import annotations

import pytest
import yaml

from applyourself import resume_model, score_packet
from applyourself.score_filter import load_scoring
from applyourself.shared import paths


@pytest.fixture
def scfg():
    return load_scoring(paths.example("scoring.example.yaml"))


def test_digest_holds_every_claim_and_skill_and_never_reads_the_date(scfg):
    resume = yaml.safe_load(paths.RESUME.read_text(encoding="utf-8"))
    resume["contexts"]["COG"]["dates"] = "Nov 2025 - Present"
    paths.RESUME.write_text(yaml.safe_dump(resume, sort_keys=False), encoding="utf-8")
    src = resume_model.load_sources()
    text = score_packet.digest(scfg)
    assert all(" ".join(b["canonical"].split()) in text for b in src.bullets.values())
    assert all(s["name"] in text for s in src.skills.values())
    assert (
        "Widget Assembly Operations (widget assembly, assembly operations, line operations);"
        in text
    )
    assert "throughput reporting" not in text  # a bullet's rephrasing stays out
    assert "- Project: Cog Assistant, maintenance question answering, Nov 2025 - Present\n" in text
    assert "- Work: Widget Operations Analyst, Widget Corp, Jan 2023 - Jun 2025 (2.5 years)" in text
    assert "- United States: authorized now, will need sponsorship later" in text


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
