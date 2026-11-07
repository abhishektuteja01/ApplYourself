"""/score's start -> judge -> finish round trip (src/applyourself/score_store.py, score_cli)."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime

import pandas as pd
import pytest

from applyourself import score_cli, score_store
from applyourself.score_filter import CountryRule, load_scoring
from applyourself.shared import paths
from tests.factories import scored_rows, write_clean, write_state

NOW = datetime(2026, 9, 27, 12, 0)
RULE = CountryRule(home=frozenset({"us"}), abroad={"germany": frozenset({"example_secondary"})})
JD = "Build widget assembly tooling in Python. " * 50  # > 300 words


@pytest.fixture
def scfg():
    return load_scoring(paths.example("scoring.example.yaml"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A scratch jobs/ + pipeline/ + profile prose, wired into score_store."""
    (tmp_path / "rubric.md").write_text("# shared rubric\n", encoding="utf-8")
    monkeypatch.setattr(score_store, "SHARED_RUBRIC", tmp_path / "rubric.md")
    return {
        "clean": tmp_path / "clean.parquet",
        "scored": tmp_path / "scored.parquet",
        "staging": tmp_path / "staging",
        "pipeline": tmp_path / "pipeline",
        "shortlist": tmp_path / "shortlist",
        "store": tmp_path / "jobs.parquet",
    }


def _clean(env, rows):
    base = {
        "source": "greenhouse",
        "company": "Acme",
        "title": "Widget Consultant",
        "location": "Austin, TX",
        "posted_date": pd.Timestamp("2026-09-20"),
        "url": "https://example.com/j",
        "jd_text": JD,
        "vertical": "example_primary",
        "employment_type": "fulltime",
        "seniority_raw": "",
        "salary_min": float("nan"),
        "salary_max": float("nan"),
        "salary_currency": "",
    }
    write_clean(env["clean"], *({**base, **r} for r in rows))


def _answer(jid, **kw):
    """A v2 answer; a field given as ... is left out."""
    a = {
        "job_id": jid,
        "in_lane": True,
        "level": "entry",
        "years": 0,
        "country": "United States",
        "sponsorship": ["silent"],
        "evidence": "",
        "blocker": None,
        "fit": 80,
        "keywords_to_mirror": ["python", "widgets"],
        "reasoning": "solid fit",
        **kw,
    }
    return {k: v for k, v in a.items() if v is not ...}


def _write_answers(env, run_slices, answers_by_id):
    for name, meta in run_slices.items():
        out = [answers_by_id[j] for j in meta["job_ids"]]
        (env["staging"] / f"out_{name}.json").write_text(json.dumps(out), encoding="utf-8")


def _start(env, scfg, **kw):
    return score_store.start(env["clean"], env["scored"], env["staging"], scfg, now=NOW, **kw)


def _finish(env, scfg):
    return score_store.finish(env["scored"], env["staging"], scfg, RULE, now=NOW)


def _scored(env):
    return pd.read_parquet(env["scored"]).set_index("job_id")


class TestStart:
    def test_drops_are_not_stored_and_drop_an_old_score(self, env, scfg):
        _clean(env, [{"job_id": "aaaaaaaa"}, {"job_id": "bbbbbbbb", "vertical": ""}])
        _start(env, scfg)
        _write_answers(
            env, score_store.load_run(env["staging"])["slices"], {"aaaaaaaa": _answer("aaaaaaaa")}
        )
        _finish(env, scfg)
        assert list(_scored(env).index) == ["aaaaaaaa"]
        _clean(env, [{"job_id": "aaaaaaaa", "jd_text": JD + " Must be a U.S. citizen."}])
        res = _start(env, scfg, rescore=True)
        assert res["dropped"] == {"ineligible": 1} and res["judge"] == 0
        assert _scored(env).empty

    def test_first_dropped_counts_only_the_latest_runs_new_jobs(self, env, scfg):
        """The night summary must not re-count the same drops every night."""
        _clean(env, [{"job_id": j, "vertical": ""} for j in ("aaaaaaaa", "bbbbbbbb")])
        night, before = pd.Timestamp("2026-09-27"), pd.Timestamp("2026-09-20")
        pd.DataFrame(
            {
                "job_id": ["aaaaaaaa", "bbbbbbbb", "bbbbbbbb"],
                "first_seen": [before, before, night],
                "last_seen": [night, night, night],
            }
        ).to_parquet(env["store"])
        res = _start(env, scfg, store_path=env["store"])
        assert res["dropped"] == {"no-lane": 2} and res["first_dropped"] == {}
        _clean(env, [{"job_id": "cccccccc", "vertical": ""}])
        pd.DataFrame(
            {"job_id": ["cccccccc"], "first_seen": [night], "last_seen": [night]}
        ).to_parquet(env["store"])
        assert _start(env, scfg, store_path=env["store"])["first_dropped"] == {"no-lane": 1}

    def test_stale_only_counts_changed_inputs_and_never_legacy_rows(self, env, scfg):
        _clean(env, [{"job_id": "aaaaaaaa"}, {"job_id": "bbbbbbbb", "company": "Beta"}])
        _start(env, scfg)
        _write_answers(
            env,
            score_store.load_run(env["staging"])["slices"],
            {j: _answer(j) for j in ("aaaaaaaa", "bbbbbbbb")},
        )
        _finish(env, scfg)
        assert _start(env, scfg, stale_only=True, count_only=True)["judge"] == 0
        legacy = pd.read_parquet(env["scored"])
        legacy.loc[legacy["job_id"] == "bbbbbbbb", "judge_input_hash"] = None
        legacy.to_parquet(env["scored"], index=False)
        _clean(
            env,
            [
                {"job_id": "aaaaaaaa", "jd_text": JD + " Now with Rust."},
                {"job_id": "bbbbbbbb", "company": "Beta", "jd_text": JD + " Now with Rust."},
            ],
        )
        assert _start(env, scfg, stale_only=True, count_only=True)["judge"] == 1
        assert not any(env["staging"].iterdir())
        assert _start(env, scfg)["judge"] == 0  # a plain run re-judges nothing
        res = _start(env, scfg, stale_only=True)
        assert res["judge"] == 1
        (meta,) = score_store.load_run(env["staging"])["slices"].values()
        assert meta["job_ids"] == ["aaaaaaaa"]

    def test_stale_only_skips_only_a_same_source_reformat_of_the_judged_jd(self, env, scfg):
        """A repost re-judges unless its JD is the judged one reformatted or
        reordered (same words), from the same source; any word change re-judges."""
        base = (
            JD
            + "Requires 3+ years of experience. Python required. Java a plus. Visa sponsorship is available."
        )
        stub = base[:300]
        judged = {"aaaaaaaa": base, "bbbbbbbb": base, "cccccccc": base, "dddddddd": base,
                  "eeeeeeee": stub, "ffffffff": base, "11111111": base,
                  "22222222": base}  # fmt: skip
        _clean(env, [{"job_id": j, "company": j, "jd_text": jd} for j, jd in judged.items()])
        _start(env, scfg)
        _write_answers(
            env, score_store.load_run(env["staging"])["slices"], {j: _answer(j) for j in judged}
        )
        _finish(env, scfg)
        day = pd.Timestamp("2026-09-20")
        pd.DataFrame(
            {"job_id": list(judged), "source": "greenhouse", "jd_text": list(judged.values())}
        ).assign(first_seen=day, last_seen=day).to_parquet(env["store"])
        reformat = "**" + "\n- ".join(reversed(base.upper().split(". ")))
        now = {
            "aaaaaaaa": reformat,
            "bbbbbbbb": base + " Must relocate to Austin.",  # add-only
            "cccccccc": base.replace("3+ years", "10+ years"),
            "dddddddd": base.replace("is available", "is not available"),
            "eeeeeeee": base,  # stub -> full JD
            "ffffffff": reformat,  # the same reformat, from another source
            "11111111": base.replace("Python required. Java", "Java required. Python"),
            "22222222": base.replace("3+ years", "3 years"),
        }
        _clean(
            env,
            [
                {"job_id": j, "company": j, "jd_text": jd}
                | ({"source": "linkedin"} if j == "ffffffff" else {})
                for j, jd in now.items()
            ],
        )
        res = _start(env, scfg, stale_only=True, store_path=env["store"])
        staged = sorted(j for m in score_store.load_run(env["staging"])["slices"].values()
                        for j in m["job_ids"])  # fmt: skip
        assert res["judge"] == 7 and staged == sorted(set(judged) - {"aaaaaaaa"})

    def test_nothing_to_judge_stages_nothing(self, env, scfg):
        _clean(env, [{"job_id": "aaaaaaaa", "vertical": ""}])
        res = _start(env, scfg)
        assert res["lines"] == [] and not env["staging"].exists()

    def test_stages_packet_slices_and_judge_lines(self, env, scfg):
        _clean(env, [{"job_id": f"{i:08x}", "company": f"Co{i}"} for i in range(23)])
        res = _start(env, scfg)
        run = score_store.load_run(env["staging"])
        assert list(run["slices"]) == [
            "example_primary_001",
            "example_primary_002",
            "example_primary_003",
        ]
        assert [len(m["job_ids"]) for m in run["slices"].values()] == [10, 10, 3]
        assert run["model"] == "score-judge:sonnet"
        packet = (env["staging"] / "packet_example_primary.md").read_text(encoding="utf-8")
        # shared rubric, digest of the example sources, lane, examples, schema: in this order
        heads = (
            "# shared rubric",
            "## Candidate profile digest",
            "Owned the daily widget assembly throughput report",
            "## Lane: Widget Engineering",
            "## Calibration examples",
            "## Answer schema",
        )
        assert [packet.index(h) for h in heads] == sorted(packet.index(h) for h in heads)
        assert "skill_weights" not in packet and "**WORK EXPERIENCE**" not in packet
        assert len(res["lines"]) == 1 and res["lines"][0].count("slice_") == 3
        text = (env["staging"] / "slice_example_primary_001.md").read_text(encoding="utf-8")
        assert "out_example_primary_001.json" in text
        assert "job_ids, exactly these: " in text
        assert max(len(line) for line in text.splitlines()) <= 200

    def test_stages_newest_first_and_a_role_not_in_the_store_last(self, env, scfg):
        rows = [{"job_id": f"{i:08x}", "company": f"Co{59 - i:02d}"} for i in range(12)]
        _clean(env, [{"job_id": "ffffffff", "company": "Aaa"}, *rows])
        seen = [pd.Timestamp("2026-09-01") + pd.Timedelta(days=i) for i in range(12)]
        pd.DataFrame(  # 00000000 is the newest and last by name; ffffffff is not in the store
            {"job_id": [r["job_id"] for r in rows], "first_seen": seen[::-1], "last_seen": seen}
        ).to_parquet(env["store"])
        _start(env, scfg, store_path=env["store"])
        slices = score_store.load_run(env["staging"])["slices"]
        assert slices["example_primary_001"]["job_ids"][:2] == ["00000000", "00000001"]
        assert slices["example_primary_002"]["job_ids"] == ["0000000a", "0000000b", "ffffffff"]

    def test_job_id_skips_the_drops(self, env, scfg):
        _clean(env, [{"job_id": "aaaaaaaa", "jd_text": JD + " 9+ years of experience"}])
        assert _start(env, scfg, job_ids=["aaaaaaaa"])["judge"] == 1

    def test_several_job_ids(self, env, scfg):
        _clean(env, [{"job_id": "aaaaaaaa"}, {"job_id": "bbbbbbbb"}, {"job_id": "cccccccc"}])
        assert _start(env, scfg, job_ids=["aaaaaaaa", "cccccccc"])["judge"] == 2

    def test_open_run_resumes_and_refuses_new_selectors(self, env, scfg):
        _clean(env, [{"job_id": "aaaaaaaa"}])
        _start(env, scfg)
        res = _start(env, scfg)
        assert res["resumed"] and len(res["lines"]) == 1
        with pytest.raises(score_store.RunOpen):
            _start(env, scfg, rescore=True)

    def test_already_scored_rows_skipped_unless_all(self, env, scfg):
        _clean(env, [{"job_id": "aaaaaaaa"}])
        _start(env, scfg)
        _write_answers(
            env, score_store.load_run(env["staging"])["slices"], {"aaaaaaaa": _answer("aaaaaaaa")}
        )
        _finish(env, scfg)
        assert _start(env, scfg)["judge"] == 0
        assert _start(env, scfg, rescore=True)["judge"] == 1


class TestFinish:
    def test_round_trip_derives_score_and_action(self, env, scfg):
        _clean(env, [{"job_id": "aaaaaaaa"}, {"job_id": "bbbbbbbb", "company": "Beta"}])
        _start(env, scfg)
        run = score_store.load_run(env["staging"])
        _write_answers(
            env,
            run["slices"],
            {
                "aaaaaaaa": _answer("aaaaaaaa"),
                "bbbbbbbb": _answer("bbbbbbbb", blocker="requires fluent German"),
            },
        )
        res = _finish(env, scfg)
        assert res["merged"] == 2 and res["lines"] == [] and res["bad"] == {}
        s = _scored(env)
        assert s.loc["aaaaaaaa", "fit_score"] == 80
        assert s.loc["aaaaaaaa", "suggested_action"] == "tailor"
        assert s.loc["bbbbbbbb", "fit_score"] == 0
        assert s.loc["bbbbbbbb", "suggested_action"] == "skip"
        assert s.loc["bbbbbbbb", "reasoning"].endswith("[blocked: requires fluent German]")
        assert s.loc["aaaaaaaa", "vertical"] == "example_primary"
        assert s.loc["aaaaaaaa", "scored_by_model"] == "score-judge:sonnet"
        assert list(env["staging"].iterdir()) == []

    def test_caps_and_tags(self, env, scfg):
        _clean(
            env,
            [
                {
                    "job_id": "aaaaaaaa",
                    "jd_text": "Short JD. 4+ years of experience.",
                    "employment_type": "contract",
                }
            ],
        )
        _start(env, scfg)
        _write_answers(
            env,
            score_store.load_run(env["staging"])["slices"],
            {"aaaaaaaa": _answer("aaaaaaaa", fit=95, years=4)},
        )
        _finish(env, scfg)
        row = _scored(env).loc["aaaaaaaa"]
        subs = json.loads(row["fit_subscores"])
        assert scfg.fit_cap is None  # no cap by default: the judge's fit stands
        assert subs == {"fit": 95, "penalty": 10, "level": "entry", "years": 4}
        assert row["fit_score"] == 85
        assert row["reasoning"].endswith("[short JD] [contract role]")
        capped = score_store.view(env["scored"], dataclasses.replace(scfg, fit_cap=70), RULE)
        assert capped.iloc[0]["fit_score"] == 60

    def test_ineligible_label_zeroes_and_skips(self, env, scfg):
        _clean(
            env,
            [{"job_id": "aaaaaaaa", "jd_text": JD + " We cannot sponsor now or in the future."}],
        )
        _start(env, scfg)
        _write_answers(
            env,
            score_store.load_run(env["staging"])["slices"],
            {
                "aaaaaaaa": _answer(
                    "aaaaaaaa",
                    sponsorship=["no_future_sponsorship"],
                    evidence="cannot sponsor now or in the future",
                )
            },
        )
        _finish(env, scfg)
        row = _scored(env).loc["aaaaaaaa"]
        assert row["fit_score"] == 0 and row["suggested_action"] == "skip"

    @pytest.mark.parametrize(
        "bad",
        [
            {"fit": True},
            {"fit": 101},
            {"fit": "80"},
            {"in_lane": "yes"},
            {"level": "junior"},
            {"blocker": ""},
            {"blocker": ...},  # facts first: the blocker is always answered
            {"title": 25},  # the old judge's four axes
            {"sponsorship": "maybe"},
            {"sponsorship": "offers", "evidence": ""},
            {"sponsorship": "offers", "evidence": "we love sponsoring"},
            {"years": -1},
            {"years": "5"},
            {"country": None},
            {"keywords_to_mirror": [1]},
            {"reasoning": ""},
            {"action": "skip"},
            {"job_id": "zzzzzzzz"},
        ],
    )
    def test_bad_answer_is_not_merged_and_is_owed_again(self, env, scfg, bad):
        _clean(env, [{"job_id": "aaaaaaaa"}])
        _start(env, scfg)
        run = score_store.load_run(env["staging"])
        _write_answers(env, run["slices"], {"aaaaaaaa": _answer("aaaaaaaa", **bad)})
        res = _finish(env, scfg)
        assert res["merged"] == 0 and len(res["lines"]) == 1 and res["bad"]
        assert not env["scored"].exists() or "aaaaaaaa" not in _scored(env).index
        assert score_store.load_run(env["staging"]) is not None
        assert (env["staging"] / "rejected_example_primary_001.json").exists()

    def test_good_slices_merge_while_a_bad_one_waits(self, env, scfg):
        _clean(env, [{"job_id": f"{i:08x}", "company": f"Co{i:02d}"} for i in range(12)])
        _start(env, scfg)
        run = score_store.load_run(env["staging"])
        answers = {j: _answer(j) for m in run["slices"].values() for j in m["job_ids"]}
        _first, second = list(run["slices"])
        answers[run["slices"][second]["job_ids"][0]]["fit"] = 999
        _write_answers(env, run["slices"], answers)
        res = _finish(env, scfg)
        assert res["merged"] == 10 and list(res["bad"]) == [second]
        assert list(score_store.load_run(env["staging"])["slices"]) == [second]
        assert res["lines"] == [f"judge {env['staging'] / f'slice_{second}.md'}"]

    def test_evidence_with_ellipsis_checks_each_part(self, env, scfg):
        _clean(
            env,
            [
                {
                    "job_id": "aaaaaaaa",
                    "jd_text": JD + " Visa sponsorship is available for this role.",
                }
            ],
        )
        _start(env, scfg)
        _write_answers(
            env,
            score_store.load_run(env["staging"])["slices"],
            {
                "aaaaaaaa": _answer(
                    "aaaaaaaa",
                    sponsorship=["offers"],
                    evidence="Visa sponsorship ... for this role",
                )
            },
        )
        assert _finish(env, scfg)["merged"] == 1


def test_all_skips_applied_and_closed_roles(env, scfg):
    _clean(env, [{"job_id": j} for j in ("aaaaaaaa", "bbbbbbbb", "cccccccc")])
    for jid, state in (("aaaaaaaa", "saved"), ("bbbbbbbb", "applied")):
        write_state(env["pipeline"], jid, state)
    res = _start(env, scfg, rescore=True, pipeline_dir=env["pipeline"])
    ids = {j for m in score_store.load_run(env["staging"])["slices"].values() for j in m["job_ids"]}
    assert res["judge"] == 2 and ids == {"aaaaaaaa", "cccccccc"}


def test_extra_keywords_are_trimmed_not_rejected(env, scfg):
    _clean(env, [{"job_id": "aaaaaaaa"}])
    _start(env, scfg)
    kws = [f"k{i}" for i in range(scfg.keywords_max + 3)]
    _write_answers(
        env,
        score_store.load_run(env["staging"])["slices"],
        {"aaaaaaaa": _answer("aaaaaaaa", keywords_to_mirror=kws)},
    )
    assert _finish(env, scfg)["merged"] == 1
    assert list(_scored(env).loc["aaaaaaaa", "keywords_to_mirror"]) == kws[: scfg.keywords_max]


def test_packet_states_the_keyword_limit(env, scfg):
    _clean(env, [{"job_id": "aaaaaaaa"}])
    _start(env, scfg)
    text = (env["staging"] / "packet_example_primary.md").read_text(encoding="utf-8")
    assert f"up to {scfg.keywords_max}" in text


def test_packet_states_the_languages(env, scfg):
    _clean(env, [{"job_id": "aaaaaaaa"}])
    _start(env, dataclasses.replace(scfg, languages=("English", "Hindi")))
    text = (env["staging"] / "packet_example_primary.md").read_text(encoding="utf-8")
    assert "English, Hindi" in text


@pytest.mark.parametrize(
    "jd_tail, quote",
    [
        (
            "an employment visa****at this time****for this role.",
            "an employment visa at this time for this role",
        ),
        ("extension support\n.", "extension support."),
        ("long-\nterm sponsorship offered", "long-term sponsorship offered"),
    ],
)
def test_quote_check_ignores_spacing_and_markdown(env, scfg, jd_tail, quote):
    _clean(env, [{"job_id": "aaaaaaaa", "jd_text": JD + " " + jd_tail}])
    _start(env, scfg)
    _write_answers(
        env,
        score_store.load_run(env["staging"])["slices"],
        {"aaaaaaaa": _answer("aaaaaaaa", sponsorship=["offers"], evidence=quote)},
    )
    assert _finish(env, scfg)["merged"] == 1


def test_quote_from_the_title_counts(env, scfg):
    _clean(env, [{"job_id": "aaaaaaaa", "title": "Widget Consultant - USC and GC's only"}])
    _start(env, scfg, job_ids=["aaaaaaaa"])
    _write_answers(
        env,
        score_store.load_run(env["staging"])["slices"],
        {"aaaaaaaa": _answer("aaaaaaaa", sponsorship=["status_bar"], evidence="USC and GC's only")},
    )
    assert _finish(env, scfg)["merged"] == 1


def test_slices_partition_the_rows(env, scfg):
    _clean(env, [{"job_id": f"{i:08x}", "company": f"Co{i % 3}"} for i in range(57)])
    _start(env, scfg)
    ids = [j for m in score_store.load_run(env["staging"])["slices"].values() for j in m["job_ids"]]
    assert len(ids) == len(set(ids)) == 57


class TestFactsDriveTheRules:
    def _one(self, env, scfg, jd=JD, **answer):
        _clean(env, [{"job_id": "aaaaaaaa", "jd_text": jd}])
        _start(env, scfg)
        _write_answers(
            env,
            score_store.load_run(env["staging"])["slices"],
            {"aaaaaaaa": _answer("aaaaaaaa", **answer)},
        )
        assert _finish(env, scfg)["merged"] == 1
        return _scored(env).loc["aaaaaaaa"]

    def test_years_regex_only_reports_a_gap(self, env, scfg):
        _clean(env, [{"job_id": "aaaaaaaa", "jd_text": JD + " 6+ years of experience."}])
        assert _start(env, scfg)["judge"] == 1
        _write_answers(
            env,
            score_store.load_run(env["staging"])["slices"],
            {"aaaaaaaa": _answer("aaaaaaaa", years=2)},
        )
        res = _finish(env, scfg)
        assert res["years_gaps"] == [("aaaaaaaa", 6, 2)]
        assert _scored(env).loc["aaaaaaaa", "suggested_action"] == "tailor"

    def test_judge_years_at_the_lane_zero_line_zero(self, env, scfg):
        row = self._one(env, scfg, years=8)
        assert row["suggested_action"] == "skip" and "[8+ yrs]" in row["reasoning"]
        assert row["fit_score"] == 0

    @pytest.mark.parametrize(
        "level, years, score",
        [("entry", 3, 75), ("senior", 3, 65), ("lead", 0, 55), ("mid", 7, 65)],
    )
    def test_the_larger_of_the_level_and_years_penalty_comes_off(
        self, env, scfg, level, years, score
    ):
        assert self._one(env, scfg, level=level, years=years)["fit_score"] == score

    def test_out_of_lane_zeroes_whatever_the_fit(self, env, scfg):
        row = self._one(env, scfg, in_lane=False, fit=95)
        assert row["fit_score"] == 0 and row["reasoning"].endswith("[out of lane]")

    def test_country_outside_lane_skips(self, env, scfg):
        row = self._one(env, scfg, country="Switzerland")
        assert row["suggested_action"] == "skip" and "out of scope: Switzerland" in row["reasoning"]
        assert row["fit_score"] == 0

    def test_abroad_country_for_another_lane_skips(self, env, scfg):
        assert self._one(env, scfg, country="Germany")["suggested_action"] == "skip"

    def test_plain_no_sponsorship_is_opt_ok_for_later(self, env, scfg):
        row = self._one(
            env,
            scfg,
            jd=JD + " We do not sponsor visas.",
            sponsorship=["no_sponsorship"],
            evidence="we do not sponsor visas",
        )
        assert row["sponsorship_label"] == "opt_ok" and row["suggested_action"] == "tailor"


class TestNeverPrune:
    def test_finish_keeps_scores_of_roles_that_left_the_view(self, env, scfg):
        _clean(env, [{"job_id": "aaaaaaaa"}])
        rows = [
            score_store._row(
                j,
                "example_primary",
                0,
                {"title": 0, "skills": 0, "seniority": 0, "domain": 0},
                "unknown",
                "",
                "r",
                [],
                "skip",
                "m",
                NOW,
            )
            for j in ("aaaaaaaa", "bbbbbbbb", "cccccccc")
        ]
        score_store.merge_rows(env["scored"], rows)
        write_state(env["pipeline"], "cccccccc", "skip")
        _finish(env, scfg)
        assert set(_scored(env).index) == {"aaaaaaaa", "bbbbbbbb", "cccccccc"}


class TestView:
    def test_legacy_rows_read_as_stored_and_filter_rows_are_not_scores(self, env):
        legacy = scored_rows(
            {
                "job_id": "aaaaaaaa",
                "fit_score": 77.0,
                "suggested_action": "tailor",
                "vertical": "example_primary",
                "scored_by_model": "score-judge:sonnet",
            },
            {"job_id": "bbbbbbbb", "scored_by_model": "filter:years"},
        ).drop(columns=["judge_answer", "judge_facts"])
        legacy.to_parquet(env["scored"], index=False)
        v = score_store.view(env["scored"]).set_index("job_id")
        old = pd.read_parquet(env["scored"]).set_index("job_id").loc[["aaaaaaaa"]]
        assert list(v.index) == ["aaaaaaaa"] and v.loc["aaaaaaaa", "basis"] == "legacy"
        cols = [c for c in old.columns if c in v.columns]
        pd.testing.assert_frame_equal(v[cols], old[cols], check_dtype=False)

    def test_judged_rows_follow_todays_rules(self, env, scfg):
        _clean(env, [{"job_id": "aaaaaaaa"}])
        _start(env, scfg)
        _write_answers(
            env, score_store.load_run(env["staging"])["slices"], {"aaaaaaaa": _answer("aaaaaaaa")}
        )
        _finish(env, scfg)
        stricter = dataclasses.replace(scfg, tailor_min=90)
        row = score_store.view(env["scored"], stricter, RULE).iloc[0]
        assert row["basis"] == "judged" and row["fit_score"] == 80
        assert row["suggested_action"] == "manual-review"
        assert _scored(env).loc["aaaaaaaa", "suggested_action"] == "tailor"

    def test_an_old_judges_answer_keeps_its_stored_score(self, env, scfg):
        old = {"job_id": "aaaaaaaa", "title": 30, "skills": 30, "seniority": 20, "domain": 20}
        scored_rows(
            {
                "job_id": "aaaaaaaa",
                "fit_score": 77.0,
                "suggested_action": "tailor",
                "vertical": "example_primary",
                "scored_by_model": "score-judge:sonnet",
                "judge_answer": json.dumps(old),
                "judge_facts": json.dumps({"years": 0, "words": 500, "contract": False}),
            }
        ).to_parquet(env["scored"], index=False)
        row = score_store.view(env["scored"], scfg, RULE).iloc[0]
        assert (row["basis"], row["fit_score"], row["suggested_action"]) == ("v1", 77, "tailor")


class TestCli:
    @pytest.fixture
    def wired(self, env, monkeypatch):
        for name in ("clean", "scored", "staging", "shortlist", "store"):
            monkeypatch.setitem(paths._overrides, name.upper(), env[name])
        return env

    def test_start_then_finish(self, wired, capsys):
        _clean(wired, [{"job_id": "aaaaaaaa"}])
        assert score_cli.main(["start"]) == 0
        out = capsys.readouterr().out.splitlines()
        assert out[0] == "judge=1"
        assert out[1].startswith("judge ") and out[1].endswith("slice_example_primary_001.md")
        _write_answers(
            wired,
            score_store.load_run(wired["staging"])["slices"],
            {"aaaaaaaa": _answer("aaaaaaaa")},
        )
        assert score_cli.main(["finish"]) == 0
        line = capsys.readouterr().out.strip()
        assert line.startswith("merged=1 shortlist=") and line.endswith("keepers=1")
        assert score_cli.main(["show", "aaaaaaaa"]) == 0
        out = capsys.readouterr().out.splitlines()
        assert out[0] == "fit_score=80 action=tailor sponsorship=unknown basis=judged"
        assert out[1] == "reasoning: solid fit"
        assert out[2] == "subscores: fit 80 / penalty 0 / level entry / years 0"
        assert score_cli.main(["show", "bbbbbbbb"]) == 1
        capsys.readouterr()
        assert score_cli.main(["lane", "example_primary"]) == 0
        out = capsys.readouterr().out
        assert "== judged scores: 1" in out and "== cutoff 80: 1 at or above" in out

    def test_finish_exit_1_prints_owed_judges(self, wired, capsys):
        _clean(wired, [{"job_id": "aaaaaaaa"}])
        score_cli.main(["start"])
        capsys.readouterr()
        assert score_cli.main(["finish"]) == 1
        out = capsys.readouterr().out
        assert "bad example_primary_001: no answer file" in out
        assert "\njudge " in out

    def test_nothing_to_do_prints_done(self, wired, capsys):
        _clean(wired, [{"job_id": "aaaaaaaa", "vertical": ""}])
        assert score_cli.main(["start"]) == 0
        out = capsys.readouterr().out
        assert "dropped_no-lane=1" in out and "\ndone shortlist=" in out

    def test_unknown_vertical(self, wired, capsys):
        _clean(wired, [{"job_id": "aaaaaaaa"}])
        assert score_cli.main(["start", "--vertical", "nope"]) == 1

    def test_open_run_blocks_all(self, wired, capsys):
        _clean(wired, [{"job_id": "aaaaaaaa"}])
        score_cli.main(["start"])
        assert score_cli.main(["start", "--all"]) == 1
        assert "still open" in capsys.readouterr().out
