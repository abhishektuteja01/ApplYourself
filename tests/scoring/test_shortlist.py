"""The shortlist is a pure read with two parts: In pipeline, then one section
per lane (src/shortlist.py)."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest
import yaml

from src import score_store, shortlist
from src.score_filter import load_scoring
from src.shared import paths
from tests.factories import write_clean, write_state

NOW = datetime(2026, 9, 27)


@pytest.fixture
def scfg():
    return load_scoring(paths.example("scoring.example.yaml"))


@pytest.fixture
def env(tmp_path):
    return {
        "clean": tmp_path / "clean.parquet",
        "scored": tmp_path / "scored.parquet",
        "pipeline": tmp_path / "pipeline",
        "shortlist": tmp_path / "shortlist",
    }


def _seed(env, rows, states=None):
    """rows: (job_id, vertical, fit, label, action, posted_date)."""
    clean, scored = [], []
    for jid, lane, fit, label, action, posted in rows:
        clean.append(
            {
                "job_id": jid,
                "company": f"Co {jid}",
                "title": f"Role {jid}",
                "location": "Austin, TX",
                "posted_date": posted,
                "source": "greenhouse",
                "url": "https://boards.greenhouse.io/acme/jobs/1",
                "vertical": lane,
            }
        )
        subs = {
            "title": min(fit, 30),
            "skills": min(max(fit - 30, 0), 30),
            "seniority": min(max(fit - 60, 0), 20),
            "domain": max(fit - 80, 0),
        }
        scored.append(
            score_store._row(jid, lane, subs, label, "", "why", ["kw1", "kw2"], action, "m", NOW)
        )
    write_clean(env["clean"], *clean)
    score_store.merge_rows(env["scored"], scored)
    for jid, state in (states or {}).items():
        write_state(env["pipeline"], jid, state)


def _compute(env, scfg):
    return shortlist.compute(env["scored"], env["clean"], env["pipeline"], scfg)


D = pd.Timestamp("2026-09-20")


def test_tracked_roles_leave_the_lane_sections(env, scfg):
    _seed(
        env,
        [
            ("aaaaaaaa", "example_primary", 90, "unknown", "tailor", D),
            ("bbbbbbbb", "example_primary", 85, "unknown", "tailor", D),
            ("cccccccc", "example_primary", 80, "unknown", "tailor", D),
            ("dddddddd", "example_primary", 75, "unknown", "tailor", D),
        ],
        states={"aaaaaaaa": "saved", "bbbbbbbb": "applied", "cccccccc": "skip"},
    )
    res = _compute(env, scfg)
    assert [r["job_id"] for r in res["pipeline"]] == ["aaaaaaaa"]
    assert [r["job_id"] for r in res["lanes"]["example_primary"]] == ["dddddddd"]


def test_exclusions_and_threshold(env, scfg):
    _seed(
        env,
        [
            ("aaaaaaaa", "example_primary", 90, "ineligible", "skip", D),
            ("bbbbbbbb", "example_primary", 70, "unknown", "skip", D),
            ("cccccccc", "example_primary", 49, "unknown", "skip", D),
            ("dddddddd", "example_primary", 55, "unknown", "manual-review", D),
        ],
    )
    assert [r["job_id"] for r in _compute(env, scfg)["lanes"]["example_primary"]] == ["dddddddd"]


def test_sort_fit_then_sponsorship_then_date_then_job_id(env, scfg):
    _seed(
        env,
        [
            ("aaaaaaaa", "example_primary", 80, "unknown", "tailor", D),
            ("bbbbbbbb", "example_primary", 80, "sponsors", "tailor", D),
            ("cccccccc", "example_primary", 80, "unknown", "tailor", pd.NaT),
            ("dddddddd", "example_primary", 80, "unknown", "tailor", D + pd.Timedelta(days=1)),
            ("eeeeeeee", "example_primary", 90, "unknown", "tailor", D),
            ("ffffffff", "example_primary", 80, "unknown", "tailor", D),
        ],
    )
    ids = [r["job_id"] for r in _compute(env, scfg)["lanes"]["example_primary"]]
    assert ids == ["eeeeeeee", "bbbbbbbb", "dddddddd", "aaaaaaaa", "ffffffff", "cccccccc"]


def test_top_n_per_lane(env, scfg):
    rows = [
        (f"{i:08x}", "example_primary", 60 + i % 30, "unknown", "tailor", D)
        for i in range(scfg.top_n + 5)
    ]
    _seed(env, rows + [("ffffffff", "example_secondary", 70, "unknown", "tailor", D)])
    res = _compute(env, scfg)
    assert len(res["lanes"]["example_primary"]) == scfg.top_n
    assert [r["job_id"] for r in res["lanes"]["example_secondary"]] == ["ffffffff"]


def test_scored_row_missing_from_clean_is_left_out(env, scfg):
    _seed(env, [("aaaaaaaa", "example_primary", 80, "unknown", "tailor", D)])
    score_store.merge_rows(
        env["scored"],
        [
            score_store._row(
                "bbbbbbbb",
                "example_primary",
                {"title": 30, "skills": 30, "seniority": 20, "domain": 10},
                "unknown",
                "",
                "why",
                [],
                "tailor",
                "m",
                NOW,
            )
        ],
    )
    assert [r["job_id"] for r in _compute(env, scfg)["lanes"]["example_primary"]] == ["aaaaaaaa"]


def test_render_and_new_marker(env, scfg):
    _seed(
        env,
        [
            ("aaaaaaaa", "example_primary", 80, "unknown", "tailor", pd.NaT),
            ("bbbbbbbb", "example_primary", 70, "sponsors", "manual-review", D),
        ],
        states={},
    )
    env["shortlist"].mkdir()
    (env["shortlist"] / "2026-09-26.md").write_text("- `aaaaaaaa`", encoding="utf-8")
    path, keepers = shortlist.write(
        env["scored"], env["clean"], env["pipeline"], env["shortlist"], scfg, "2026-09-27"
    )
    md = path.read_text(encoding="utf-8")
    assert keepers == 2
    assert "## In pipeline (0)" in md and "Nothing saved or tailored." in md
    assert "posted unknown" in md and "NaT" not in md
    assert "— Role aaaaaaaa\n" in md and "— Role bbbbbbbb ★" in md
    assert "suggested manual-review" in md and "sponsorship sponsors" in md
    assert "No keepers today." in md  # the other two lanes
    assert md.count(scfg.note) == 1


def test_pipeline_section_lists_only_the_top_few(env, scfg):
    rows = [(f"{i:08x}", "example_primary", 60 + i, "unknown", "tailor", D) for i in range(12)]
    _seed(env, rows, states={r[0]: "saved" for r in rows})
    cfg = type(scfg)(**{**scfg.__dict__, "pipeline_show": 3})
    path, _ = shortlist.write(
        env["scored"], env["clean"], env["pipeline"], env["shortlist"], cfg, "2026-09-27"
    )
    md = path.read_text(encoding="utf-8")
    assert "## In pipeline (12)" in md and "…and 9 more" in md
    assert "`0000000b` 71" in md and "`00000000`" not in md


def test_empty_scored(env, scfg):
    write_clean(env["clean"])
    res = _compute(env, scfg)
    assert res["pipeline"] == [] and all(v == [] for v in res["lanes"].values())


def test_referral_mark_on_lane_and_pipeline_rows(env, scfg, tmp_path):
    _seed(
        env,
        [
            ("aaaaaaaa", "example_primary", 80, "unknown", "tailor", D),
            ("bbbbbbbb", "example_primary", 75, "unknown", "tailor", D),
            ("cccccccc", "example_primary", 70, "unknown", "tailor", D),
        ],
        states={"cccccccc": "saved"},
    )
    book = tmp_path / "contacts.yaml"
    book.write_text(
        yaml.safe_dump(
            {
                "contacts": [
                    {
                        "id": "x",
                        "name": "Sam Okafor",
                        "company": "Co aaaaaaaa",
                        "relationship": "referral",
                    },
                    {
                        "id": "y",
                        "name": "Priya Raman",
                        "company": "Co cccccccc",
                        "relationship": "referral",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    path, _ = shortlist.write(
        env["scored"],
        env["clean"],
        env["pipeline"],
        env["shortlist"],
        scfg,
        "2026-09-27",
        contacts_path=book,
    )
    md = path.read_text(encoding="utf-8")
    assert "- referral: Sam Okafor" in md
    assert md.count("referral:") == 2
    assert "(saved) · referral: Priya Raman" in md
