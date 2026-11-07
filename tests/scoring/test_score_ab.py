"""`score ab-stage` / `ab-compare`: both packets over the same rows, compared."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from applyourself import score_ab, score_store
from applyourself.score_filter import CountryRule, load_scoring
from applyourself.shared import paths
from tests.conftest import FIXTURE_PATH
from tests.factories import write_clean

JD = "Build widget assembly tooling in Python. " * 50


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setitem(paths._overrides, "CLEAN", tmp_path / "clean.parquet")
    monkeypatch.setitem(paths._overrides, "SCORED", tmp_path / "scored.parquet")
    monkeypatch.setitem(paths._overrides, "VERTICALS_CONFIG", FIXTURE_PATH)
    write_clean(
        tmp_path / "clean.parquet",
        *(
            {
                "job_id": f"{i:08x}",
                "company": f"Co{i}",
                "title": "Widget Consultant",
                "jd_text": JD,
                "vertical": "example_primary" if i % 2 else "example_secondary",
            }
            for i in range(12)
        ),
    )
    return tmp_path


def _answers(out, arm, fit):
    run = score_store.load_run(out / arm)
    for name, meta in run["slices"].items():
        common = {"years": 0, "country": "", "sponsorship": ["silent"], "evidence": ""}
        common |= {"keywords_to_mirror": [], "reasoning": "r"}
        rows = [
            {
                "job_id": j,
                **common,
                "title": 20,
                "skills": fit(j) - 60,
                "seniority": 20,
                "domain": 10,
            }
            if arm == "v1"
            else {"job_id": j, **common, "in_lane": True, "level": "entry", "blocker": None}
            | {"fit": fit(j)}
            for j in meta["job_ids"]
        ]
        (out / arm / f"out_{name}.json").write_text(json.dumps(rows), encoding="utf-8")


def test_both_arms_stage_the_same_rows_and_compare(world):
    cfg = load_scoring(paths.example("scoring.example.yaml"))
    out = world / "ab"
    now = datetime(2026, 11, 6, 12, 0)
    res = score_ab.stage(out, cfg, rows=10, top=2, repeat=4, seed=3, now=now)
    sample = json.loads((out / "sample.json").read_text(encoding="utf-8"))
    ids = {
        a: {j for m in score_store.load_run(out / a)["slices"].values() for j in m["job_ids"]}
        for a in score_ab.ARMS
    }
    assert res["rows"] == len(sample["lanes"]) == 10 and ids["v1"] == ids["v2"] == set(
        sample["lanes"]
    )
    assert ids["v2_repeat"] == set(sample["repeat"]) and len(ids["v2_repeat"]) == 4
    v1 = (out / "v1" / "packet_example_primary.md").read_text(encoding="utf-8")
    v2 = (out / "v2" / "packet_example_primary.md").read_text(encoding="utf-8")
    assert "widget_assembly: 10" in v1 and "## Resume" in v1  # the old keys and resume
    assert "## Candidate profile digest" in v2 and "## Skill weights" not in v2
    with pytest.raises(ValueError, match="not empty"):
        score_ab.stage(out, cfg, rows=10, top=2, repeat=4, seed=3, now=now)

    fit = {j: 60 + int(j, 16) * 2 for j in sample["lanes"]}
    _answers(out, "v1", fit.__getitem__)
    _answers(out, "v2", fit.__getitem__)
    _answers(out, "v2_repeat", lambda j: fit[j] + (5 if j == sample["repeat"][0] else 0))
    rule = CountryRule(home=frozenset({"us"}), abroad={})
    rep = score_ab.compare(out, cfg, rule)
    assert rep["problems"] == []
    overall = rep["lanes"][0]
    assert overall["lane"] == "all" and overall["n"] == 10 and overall["rho_v1_v2"] == 1.0
    c = rep["consistency"]
    assert (c["n"], c["fit_max"], c["fit_over_3"], c["fit_mean"]) == (4, 5, 1, 1.25)
    line = {"example_primary": 80, "example_secondary": 78}  # v1 scores fit - 10 here
    crossing = {j for j, lane in sample["lanes"].items() if fit[j] - 10 < line[lane] <= fit[j]}
    assert {x["job_id"] for x in rep["crossing"]} == crossing and crossing
