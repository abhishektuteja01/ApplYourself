"""Tests for src/scoring_io.py — deterministic parquet plumbing for /score.

Per the slice-4 plan: scoring_io is in src/ and a bug here could silently drop
or duplicate scored rows, so it gets pytest coverage even though the scoring
prompt itself doesn't (deterministic src/ logic gets tested)."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
import yaml

from src.cleaning import CLEAN_COLUMNS
from src.scoring_io import (
    AUTO_SKIP_SCORED_BY,
    SCORED_COLUMNS,
    auto_score_disqualified,
    auto_score_ineligible,
    auto_score_out_of_lane,
    compute_shortlist,
    disqualify_reason,
    dump_shortlist_input,
    dump_unscored,
    hard_ineligible_phrase,
    load_hard_ineligible,
    max_years_required,
    merge_scores,
    merge_scores_from_dir,
    prune_scored,
    select_unscored,
    validate_scores,
)


# ---------- helpers ----------

def _split_score(total: int) -> dict:
    """Distribute total across the 4 axes respecting maxima (30/30/20/20)."""
    maxima = [("title", 30), ("skills", 30), ("seniority", 20), ("domain", 20)]
    remaining = total
    out = {}
    for axis, m in maxima:
        take = min(remaining, m)
        out[axis] = take
        remaining -= take
    if remaining > 0:
        raise ValueError(f"Cannot allocate {total} across axes (max 100)")
    return out


def _valid_score(**overrides) -> dict:
    base = {
        "job_id": "aaaaaaaa",
        "fit_score": 75,
        "fit_subscores": _split_score(75),
        "vertical": "sap",
        "sponsorship_label": "opt_ok",
        "sponsorship_evidence": "no visa sponsorship",
        "reasoning": "ACM commodity lifecycle fit; senior stretch flagged.",
        "keywords_to_mirror": ["ACM", "commodity contract"],
        "suggested_action": "tailor",
    }
    base.update(overrides)
    if "fit_score" in overrides and "fit_subscores" not in overrides:
        base["fit_subscores"] = _split_score(overrides["fit_score"])
    return base


def _make_clean(tmp_path: Path, rows: list[dict]) -> Path:
    """Write a clean.parquet with the §7.1 schema. Defaults are sensible;
    callers override only what they need to vary."""
    base = {c: "" for c in CLEAN_COLUMNS}
    base.update({
        "remote_flag": False,
        "posted_date": pd.Timestamp("2026-06-01"),
        "posted_date_missing": False,
        "scraped_date": pd.Timestamp("2026-06-06"),
        "salary_min": float("nan"),
        "salary_max": float("nan"),
        "already_seen": False,
        "fit_score": float("nan"),
        "shortlist_rank": float("nan"),
        "sponsorship_label": "unknown",
    })
    data = [{**base, **r} for r in rows]
    df = pd.DataFrame(data, columns=CLEAN_COLUMNS)
    p = tmp_path / "clean.parquet"
    df.to_parquet(p, index=False)
    return p


# ---------- select_unscored ----------

def test_select_unscored_no_scored_yet(tmp_path):
    clean_p = _make_clean(tmp_path, [{"job_id": "aaaaaaaa"}, {"job_id": "bbbbbbbb"}])
    scored_p = tmp_path / "scored.parquet"
    out = select_unscored(clean_p, scored_p)
    assert set(out["job_id"]) == {"aaaaaaaa", "bbbbbbbb"}


def test_select_unscored_incremental(tmp_path):
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa"}, {"job_id": "bbbbbbbb"}, {"job_id": "cccccccc"},
    ])
    scored_p = tmp_path / "scored.parquet"
    merge_scores(scored_p, [_valid_score(job_id="aaaaaaaa")], scored_by_model="test")
    out = select_unscored(clean_p, scored_p)
    assert set(out["job_id"]) == {"bbbbbbbb", "cccccccc"}


# ---------- dump_unscored ----------

def test_dump_unscored_writes_jsonl(tmp_path):
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa", "company": "Acme", "title": "SAP ACM",
         "jd_text": "hi" * 200, "url": "https://x", "vertical": "sap"},
    ])
    scored_p = tmp_path / "scored.parquet"
    out_path = tmp_path / "unscored.jsonl"
    n = dump_unscored(clean_p, scored_p, out_path)
    assert n == 1
    line = out_path.read_text().strip()
    obj = json.loads(line)
    assert obj["job_id"] == "aaaaaaaa"
    assert obj["company"] == "Acme"
    assert obj["title"] == "SAP ACM"
    assert "jd_text" in obj
    assert "url" in obj


def test_dump_unscored_force_all_ignores_scored(tmp_path):
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa", "title": "SAP Functional Analyst", "vertical": "sap"},
        {"job_id": "bbbbbbbb", "title": "SAP SD Consultant", "vertical": "sap"},
    ])
    scored_p = tmp_path / "scored.parquet"
    merge_scores(scored_p, [_valid_score(job_id="aaaaaaaa")], scored_by_model="t")
    out_path = tmp_path / "unscored.jsonl"
    n = dump_unscored(clean_p, scored_p, out_path, force_all=True)
    assert n == 2  # both, despite aaaaaaaa already scored


# ---------- out-of-lane pre-screen (vertical="" auto-skip) ----------

def test_dump_unscored_splits_in_lane_vs_out_of_lane(tmp_path):
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa", "title": "SAP Functional Analyst", "vertical": "sap"},
        {"job_id": "bbbbbbbb", "title": "Public Health Data Analyst", "vertical": ""},
        {"job_id": "cccccccc", "title": "Trade Surveillance Analyst", "vertical": "sap"},
        {"job_id": "dddddddd", "title": "Senior Platform Engineer", "vertical": ""},
    ])
    scored_p = tmp_path / "scored.parquet"
    out_path = tmp_path / "unscored.jsonl"
    n_judge = dump_unscored(clean_p, scored_p, out_path)
    assert n_judge == 2
    judge_ids = {json.loads(l)["job_id"] for l in out_path.read_text().splitlines() if l.strip()}
    assert judge_ids == {"aaaaaaaa", "cccccccc"}
    skip_path = tmp_path / "auto_skip.jsonl"
    assert skip_path.exists()
    skip_ids = {json.loads(l)["job_id"] for l in skip_path.read_text().splitlines() if l.strip()}
    assert skip_ids == {"bbbbbbbb", "dddddddd"}


def test_dump_unscored_only_vertical_filters_to_one_vertical(tmp_path):
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa", "title": "SAP Functional Analyst", "vertical": "sap"},
        {"job_id": "bbbbbbbb", "title": "Model Risk Analyst", "vertical": "risk_ai"},
        {"job_id": "cccccccc", "title": "Public Health Data Analyst", "vertical": ""},
    ])
    scored_p = tmp_path / "scored.parquet"
    out_path = tmp_path / "unscored.jsonl"
    n_judge = dump_unscored(clean_p, scored_p, out_path, only_vertical="risk_ai")
    assert n_judge == 1
    judge_ids = {json.loads(l)["job_id"] for l in out_path.read_text().splitlines() if l.strip()}
    assert judge_ids == {"bbbbbbbb"}
    # sap and out-of-lane rows are left entirely untouched (not even auto-skipped)
    skip_path = tmp_path / "auto_skip.jsonl"
    skip_ids = {json.loads(l)["job_id"] for l in skip_path.read_text().splitlines() if l.strip()} if skip_path.exists() else set()
    assert skip_ids == set()


# ---------- risk_ai vertical: JD disqualifier pre-screen ----------

def test_risk_ai_disqualify_reason_matches_phrases(cfg):
    risk_ai = cfg.verticals["risk_ai"]
    assert disqualify_reason(risk_ai, "Candidates must have a PhD required for this role.") == "phrase"
    assert disqualify_reason(risk_ai, "CFA required; 5+ years quantitative finance.") == "phrase"
    assert disqualify_reason(risk_ai, "Sanctions screening coverage for the compliance desk.") == "phrase"
    assert disqualify_reason(risk_ai, "FRM certification required.") == "phrase"


def test_risk_ai_disqualify_reason_case_insensitive(cfg):
    assert disqualify_reason(cfg.verticals["risk_ai"], "Ph.D. Required in Mathematics or Statistics") == "phrase"


def test_risk_ai_disqualify_reason_none_on_preferred_not_required(cfg):
    # "preferred" must NOT trip the disqualifier — only hard "required" wording does.
    risk_ai = cfg.verticals["risk_ai"]
    assert disqualify_reason(risk_ai, "PhD preferred but not required. CS background welcome.") is None
    assert disqualify_reason(risk_ai, "") is None
    assert disqualify_reason(risk_ai, None) is None


# ---------- risk_ai vertical: explicit 5+ years experience disqualifier ----------

def test_max_years_required_simple_and_plus_forms():
    assert max_years_required("5+ years of experience in governance, risk, compliance") == 5
    assert max_years_required("2 years of experience required") == 2
    assert max_years_required("no year mention here") == 0
    assert max_years_required("") == 0
    assert max_years_required(None) == 0


def test_max_years_required_takes_lower_bound_of_a_range():
    # "3-6 years of experience" means the true minimum is 3, not 6 — taking
    # the upper bound would wrongly read a 3-year role as a 6-year one.
    assert max_years_required("3-6 years of professional experience required") == 3
    assert max_years_required("3–6 years of professional experience required") == 3  # en-dash


def test_max_years_required_ignores_unrelated_year_mentions():
    # "150-year legacy" must never be read as a 150-year experience requirement.
    assert max_years_required(
        "Frost has dedicated their expertise... over 150-year legacy of providing service."
    ) == 0


def test_max_years_required_handles_markdown_escaped_text():
    # Real scraped JDs carry markdown-escaped punctuation: "5\+ years", "**5\+ years**".
    assert max_years_required("* 5\\+ years of experience in governance and risk") == 5
    assert max_years_required("* **5\\+ years** \n of experience in one or more of the following") == 5


def test_max_years_required_takes_max_across_multiple_clauses():
    text = (
        "5+ years of experience in governance, risk, compliance, privacy, "
        "information security, technology risk, third-party risk, model risk, "
        "audit, or a related field. 2+ years of direct experience in AI governance."
    )
    assert max_years_required(text) == 5


def test_risk_ai_disqualify_reason_years_over_threshold(cfg):
    risk_ai = cfg.verticals["risk_ai"]
    assert disqualify_reason(risk_ai, "5+ years of experience in governance and risk") == "years"
    assert disqualify_reason(risk_ai, "6+ years of experience in model risk") == "years"
    assert disqualify_reason(risk_ai, "4+ years of experience in model risk") is None
    assert disqualify_reason(risk_ai, "2+ years of experience, 3-4 years preferred") is None


def test_risk_ai_disqualify_reason_real_jds_with_5plus_year_requirement(cfg):
    # Real JD text (markdown-escaped) from the Mom Project / Robert Half AI Risk
    # & Compliance Analyst postings that originally over-scored seniority=18
    # despite an explicit 5+ year requirement (capped at 4/20).
    mom_project_jd = (
        "**Skills And Qualifications**\n"
        "* 5\\+ years of experience in governance, risk, compliance, privacy, "
        "information security, technology risk, third\\-party risk, model risk, "
        "audit, or a related field.\n"
        "* 2\\+ years of direct experience in AI governance, responsible AI, "
        "AI risk assessment, AI compliance, model risk management, machine "
        "learning governance, or emerging technology risk."
    )
    assert disqualify_reason(cfg.verticals["risk_ai"], mom_project_jd) == "years"

    robert_half_jd = (
        "**Required Qualifications** \n\n\n\n* **5\\+ years** \n of experience "
        "in one or more of the following:\n"
        "* Governance, Risk \\& Compliance (GRC), Privacy, Information Security, "
        "Technology Risk, Third\\-Party Risk, Model Risk, or Audit\n"
        "* **2\\+ years** of hands\\-on experience in:\n"
        "* AI governance, Responsible AI, AI risk assessment, AI compliance, "
        "or model risk management"
    )
    assert disqualify_reason(cfg.verticals["risk_ai"], robert_half_jd) == "years"


def test_dump_unscored_routes_risk_ai_disqualified_to_separate_skip_file(tmp_path):
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa", "title": "Model Risk Analyst", "vertical": "risk_ai",
         "jd_text": "PhD required. " + "x" * 200},
        {"job_id": "bbbbbbbb", "title": "Model Validation Analyst", "vertical": "risk_ai",
         "jd_text": "CS or Engineering background welcome. " + "x" * 200},
        {"job_id": "cccccccc", "title": "SAP Functional Analyst", "vertical": "sap",
         "jd_text": "x" * 200},
    ])
    scored_p = tmp_path / "scored.parquet"
    out_path = tmp_path / "unscored.jsonl"
    n_judge = dump_unscored(clean_p, scored_p, out_path)
    assert n_judge == 2  # bbbbbbbb (risk_ai, clean JD) + cccccccc (sap)
    judge_ids = {json.loads(l)["job_id"] for l in out_path.read_text().splitlines() if l.strip()}
    assert judge_ids == {"bbbbbbbb", "cccccccc"}
    # judged risk_ai rows carry their vertical through for the LLM's rubric choice
    judged = {json.loads(l)["job_id"]: json.loads(l)["vertical"]
              for l in out_path.read_text().splitlines() if l.strip()}
    assert judged["bbbbbbbb"] == "risk_ai"
    assert judged["cccccccc"] == "sap"
    risk_ai_skip_path = tmp_path / "auto_skip_risk_ai.jsonl"
    assert risk_ai_skip_path.exists()
    skip_ids = {json.loads(l)["job_id"] for l in risk_ai_skip_path.read_text().splitlines() if l.strip()}
    assert skip_ids == {"aaaaaaaa"}
    # the plain (title-out-of-lane) auto_skip.jsonl exists but is empty here
    assert (tmp_path / "auto_skip.jsonl").exists()


def test_auto_score_disqualified_materializes_risk_ai_skip_rows(tmp_path, cfg):
    skip_path = tmp_path / "auto_skip_risk_ai.jsonl"
    skip_path.write_text(
        json.dumps({"job_id": "aaaaaaaa", "title": "Model Risk Analyst"}) + "\n"
    )
    scored_p = tmp_path / "scored.parquet"
    n = auto_score_disqualified(cfg.verticals["risk_ai"], skip_path, scored_p)
    assert n == 1
    df = pd.read_parquet(scored_p)
    assert df.iloc[0]["job_id"] == "aaaaaaaa"
    assert df.iloc[0]["fit_score"] == 0
    assert df.iloc[0]["vertical"] == "risk_ai"
    assert df.iloc[0]["suggested_action"] == "skip"
    assert df.iloc[0]["scored_by_model"] == "rubric:risk-ai-jd-disqualifier"


def test_auto_score_disqualified_noop_on_missing_file(tmp_path, cfg):
    scored_p = tmp_path / "scored.parquet"
    assert auto_score_disqualified(cfg.verticals["risk_ai"], tmp_path / "nope.jsonl", scored_p) == 0
    assert not scored_p.exists()


# ---------- sap vertical: explicit 5+ years experience disqualifier (extended 2026-06-17) ----------

def test_sap_disqualify_reason_years_over_threshold(cfg):
    sap = cfg.verticals["sap"]
    assert disqualify_reason(sap, "5+ years of experience in SAP ACM commodity management") == "years"
    assert disqualify_reason(sap, "6+ years of experience as an SAP functional analyst") == "years"
    assert disqualify_reason(sap, "4+ years of experience as an SAP functional analyst") is None
    assert disqualify_reason(sap, "2+ years of experience, 3-4 years preferred") is None
    assert disqualify_reason(sap, "") is None
    assert disqualify_reason(sap, None) is None
    # sap has no phrase disqualifier — PhD/CFA/FRM wording alone never trips it
    assert disqualify_reason(sap, "PhD required for this role.") is None


def test_dump_unscored_routes_sap_disqualified_to_separate_skip_file(tmp_path):
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa", "title": "SAP Functional Analyst", "vertical": "sap",
         "jd_text": "5+ years of experience required. " + "x" * 200},
        {"job_id": "bbbbbbbb", "title": "SAP ACM Analyst", "vertical": "sap",
         "jd_text": "2+ years of experience preferred. " + "x" * 200},
        {"job_id": "cccccccc", "title": "Model Risk Analyst", "vertical": "risk_ai",
         "jd_text": "x" * 200},
    ])
    scored_p = tmp_path / "scored.parquet"
    out_path = tmp_path / "unscored.jsonl"
    n_judge = dump_unscored(clean_p, scored_p, out_path)
    assert n_judge == 2  # bbbbbbbb (sap, under threshold) + cccccccc (risk_ai)
    judge_ids = {json.loads(l)["job_id"] for l in out_path.read_text().splitlines() if l.strip()}
    assert judge_ids == {"bbbbbbbb", "cccccccc"}
    sap_skip_path = tmp_path / "auto_skip_sap.jsonl"
    assert sap_skip_path.exists()
    skip_ids = {json.loads(l)["job_id"] for l in sap_skip_path.read_text().splitlines() if l.strip()}
    assert skip_ids == {"aaaaaaaa"}


def test_auto_score_disqualified_materializes_sap_skip_rows(tmp_path, cfg):
    skip_path = tmp_path / "auto_skip_sap.jsonl"
    skip_path.write_text(
        json.dumps({"job_id": "aaaaaaaa", "title": "SAP Functional Analyst",
                    "_disqualify_reason": "years"}) + "\n"
    )
    scored_p = tmp_path / "scored.parquet"
    n = auto_score_disqualified(cfg.verticals["sap"], skip_path, scored_p)
    assert n == 1
    df = pd.read_parquet(scored_p)
    assert df.iloc[0]["job_id"] == "aaaaaaaa"
    assert df.iloc[0]["fit_score"] == 0
    assert df.iloc[0]["vertical"] == "sap"
    assert df.iloc[0]["suggested_action"] == "skip"
    assert df.iloc[0]["scored_by_model"] == "rubric:sap-jd-years-disqualifier"
    # a "years" skip carries the vertical's reasoning_years text
    assert df.iloc[0]["reasoning"] == cfg.verticals["sap"].reasoning_years


# ---------- hard-ineligible pre-label (added 2026-07-14, carve-out) ----------

def test_load_hard_ineligible_reads_and_lowercases(tmp_path):
    p = tmp_path / "rules.yaml"
    p.write_text("hard_ineligible:\n  - 'Active Security Clearance'\n  - 'green card required'\n")
    assert load_hard_ineligible(p) == ("active security clearance", "green card required")


def test_load_hard_ineligible_missing_key_is_empty(tmp_path):
    p = tmp_path / "rules.yaml"
    p.write_text("ineligible:\n  - 'US citizen'\n")
    assert load_hard_ineligible(p) == ()


def test_load_hard_ineligible_live_file_loads():
    # the committed profile/sponsorship_rules.yaml must stay parseable
    phrases = load_hard_ineligible()
    assert "active security clearance" in phrases
    # the application-question wording family must NEVER be in the hard list
    # (measured 84-scoring false positive)
    assert not any("now or in the future" in p for p in phrases)


def test_hard_ineligible_phrase_matches_case_insensitive():
    phrases = ("active security clearance", "green card required")
    assert hard_ineligible_phrase(phrases, "Must hold an ACTIVE SECURITY CLEARANCE.") == "active security clearance"
    assert hard_ineligible_phrase(phrases, "Great benefits and a green card required.") == "green card required"
    assert hard_ineligible_phrase(phrases, "Standard authorization wording only.") is None
    assert hard_ineligible_phrase(phrases, "") is None
    assert hard_ineligible_phrase(phrases, None) is None


def test_dump_unscored_routes_hard_ineligible_before_disqualifiers(tmp_path):
    clean_p = _make_clean(tmp_path, [
        # clearance phrase AND a disqualifying years requirement -> ineligible wins
        {"job_id": "aaaaaaaa", "title": "SAP Functional Analyst", "vertical": "sap",
         "jd_text": "Active security clearance. 8+ years of experience. " + "x" * 200},
        {"job_id": "bbbbbbbb", "title": "SAP ACM Analyst", "vertical": "sap",
         "jd_text": "2+ years of experience preferred. " + "x" * 200},
        # out-of-lane row with a clearance phrase -> stays out-of-lane
        {"job_id": "cccccccc", "title": "Chef", "vertical": "",
         "jd_text": "Active security clearance. " + "x" * 200},
    ])
    scored_p = tmp_path / "scored.parquet"
    out_path = tmp_path / "unscored.jsonl"
    n_judge = dump_unscored(clean_p, scored_p, out_path,
                            hard_ineligible=("active security clearance",))
    assert n_judge == 1
    inel_rows = [json.loads(l) for l in
                 (tmp_path / "auto_skip_ineligible.jsonl").read_text().splitlines() if l.strip()]
    assert [r["job_id"] for r in inel_rows] == ["aaaaaaaa"]
    assert inel_rows[0]["_ineligible_phrase"] == "active security clearance"
    # the years disqualifier never saw the row
    assert (tmp_path / "auto_skip_sap.jsonl").read_text().strip() == ""
    # out-of-lane row stayed in auto_skip.jsonl
    ool = [json.loads(l)["job_id"] for l in
           (tmp_path / "auto_skip.jsonl").read_text().splitlines() if l.strip()]
    assert ool == ["cccccccc"]


def test_auto_score_ineligible_materializes_labeled_rows(tmp_path):
    skip_path = tmp_path / "auto_skip_ineligible.jsonl"
    skip_path.write_text(json.dumps({
        "job_id": "aaaaaaaa", "title": "SAP Functional Analyst",
        "vertical": "sap", "_ineligible_phrase": "active security clearance",
    }) + "\n")
    scored_p = tmp_path / "scored.parquet"
    n = auto_score_ineligible(skip_path, scored_p)
    assert n == 1
    df = pd.read_parquet(scored_p)
    r = df.iloc[0]
    assert r["sponsorship_label"] == "ineligible"
    assert r["sponsorship_evidence"] == "active security clearance"
    assert r["fit_score"] == 0
    assert r["vertical"] == "sap"
    assert r["scored_by_model"] == "rubric:hard-ineligible-pre-screen"
    assert r["reasoning"].startswith("Auto-labeled ineligible by deterministic pre-screen")


def test_auto_score_ineligible_noop_on_missing_file(tmp_path):
    scored_p = tmp_path / "scored.parquet"
    assert auto_score_ineligible(tmp_path / "nope.jsonl", scored_p) == 0
    assert not scored_p.exists()


# ---------- title-level disqualifier (added 2026-07-14) ----------

def test_disqualify_reason_title_phrase_trips(cfg):
    sap = cfg.verticals["sap"]
    clean_jd = "2+ years of experience preferred. " + "x" * 200
    assert disqualify_reason(sap, clean_jd, "SAP Solution Architect") == "title"
    assert disqualify_reason(sap, clean_jd, "Director, SAP Programs") == "title"
    assert disqualify_reason(sap, clean_jd, "SAP Functional Analyst") is None


def test_disqualify_reason_title_case_insensitive_and_optional(cfg):
    sap = cfg.verticals["sap"]
    assert disqualify_reason(sap, "x" * 50, "sap SOLUTION ARCHITECT") == "title"
    # title omitted / None / empty — jd-side checks still run, no crash
    assert disqualify_reason(sap, "x" * 50) is None
    assert disqualify_reason(sap, "x" * 50, None) is None
    assert disqualify_reason(sap, "x" * 50, "") is None


def test_disqualify_reason_title_takes_priority_over_years(cfg):
    sap = cfg.verticals["sap"]
    jd = "8+ years of experience required. " + "x" * 200
    assert disqualify_reason(sap, jd, "SAP Solution Architect") == "title"
    assert disqualify_reason(sap, jd, "SAP Functional Analyst") == "years"


def test_disqualify_reason_title_not_checked_on_vertical_without_title_phrases(cfg):
    risk_ai = cfg.verticals["risk_ai"]
    assert disqualify_reason(risk_ai, "x" * 50, "Solution Architect") is None


def test_dump_unscored_routes_title_disqualified_to_skip_file(tmp_path):
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa", "title": "SAP Solution Architect", "vertical": "sap",
         "jd_text": "2+ years of experience preferred. " + "x" * 200},
        {"job_id": "bbbbbbbb", "title": "SAP ACM Analyst", "vertical": "sap",
         "jd_text": "2+ years of experience preferred. " + "x" * 200},
    ])
    scored_p = tmp_path / "scored.parquet"
    out_path = tmp_path / "unscored.jsonl"
    n_judge = dump_unscored(clean_p, scored_p, out_path)
    assert n_judge == 1
    skip_rows = [json.loads(l) for l in
                 (tmp_path / "auto_skip_sap.jsonl").read_text().splitlines() if l.strip()]
    assert [r["job_id"] for r in skip_rows] == ["aaaaaaaa"]
    assert skip_rows[0]["_disqualify_reason"] == "title"


def test_auto_score_disqualified_uses_title_reasoning(tmp_path, cfg):
    skip_path = tmp_path / "auto_skip_sap.jsonl"
    skip_path.write_text(json.dumps({
        "job_id": "aaaaaaaa", "title": "SAP Solution Architect",
        "_disqualify_reason": "title",
    }) + "\n")
    scored_p = tmp_path / "scored.parquet"
    n = auto_score_disqualified(cfg.verticals["sap"], skip_path, scored_p)
    assert n == 1
    df = pd.read_parquet(scored_p)
    assert df.iloc[0]["reasoning"] == cfg.verticals["sap"].reasoning_title
    assert df.iloc[0]["fit_score"] == 0
    assert df.iloc[0]["suggested_action"] == "skip"


def test_auto_score_out_of_lane_materializes_skip_rows(tmp_path):
    skip_path = tmp_path / "auto_skip.jsonl"
    skip_path.write_text(
        json.dumps({"job_id": "aaaaaaaa", "title": "Plumber"}) + "\n"
        + json.dumps({"job_id": "bbbbbbbb", "title": "Chef"}) + "\n"
    )
    scored_p = tmp_path / "scored.parquet"
    n = auto_score_out_of_lane(skip_path, scored_p)
    assert n == 2
    df = pd.read_parquet(scored_p)
    assert set(df["job_id"]) == {"aaaaaaaa", "bbbbbbbb"}
    assert (df["fit_score"] == 0).all()
    assert (df["suggested_action"] == "skip").all()
    assert (df["sponsorship_label"] == "unknown").all()
    assert (df["scored_by_model"] == AUTO_SKIP_SCORED_BY).all()


def test_auto_score_out_of_lane_noop_on_missing_file(tmp_path):
    scored_p = tmp_path / "scored.parquet"
    assert auto_score_out_of_lane(tmp_path / "nope.jsonl", scored_p) == 0
    assert not scored_p.exists()


def test_auto_score_out_of_lane_noop_on_empty_file(tmp_path):
    skip_path = tmp_path / "auto_skip.jsonl"
    skip_path.write_text("")
    scored_p = tmp_path / "scored.parquet"
    assert auto_score_out_of_lane(skip_path, scored_p) == 0


def test_end_to_end_dump_then_auto_score(tmp_path):
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa", "title": "SAP Functional Analyst", "vertical": "sap"},
        {"job_id": "bbbbbbbb", "title": "Public Health Data Analyst", "vertical": ""},
    ])
    scored_p = tmp_path / "scored.parquet"
    out_path = tmp_path / "unscored.jsonl"
    n_judge = dump_unscored(clean_p, scored_p, out_path)
    n_skip = auto_score_out_of_lane(tmp_path / "auto_skip.jsonl", scored_p)
    assert n_judge == 1
    assert n_skip == 1
    df = pd.read_parquet(scored_p)
    assert set(df["job_id"]) == {"bbbbbbbb"}
    assert df.loc[df["job_id"] == "bbbbbbbb", "fit_score"].iloc[0] == 0
    assert df.loc[df["job_id"] == "bbbbbbbb", "scored_by_model"].iloc[0] == AUTO_SKIP_SCORED_BY


# ---------- merge_scores ----------

def test_merge_scores_adds_new_and_stamps_model(tmp_path):
    scored_p = tmp_path / "scored.parquet"
    ts = datetime(2026, 6, 6, 15, 30)
    n = merge_scores(
        scored_p, [_valid_score(job_id="aaaaaaaa")],
        scored_by_model="claude-opus-4-7", scored_at=ts,
    )
    assert n == 1
    df = pd.read_parquet(scored_p)
    assert df.iloc[0]["scored_by_model"] == "claude-opus-4-7"
    assert pd.Timestamp(df.iloc[0]["scored_at"]) == pd.Timestamp(ts)
    assert df.iloc[0]["fit_score"] == 75.0
    assert list(df.columns) == SCORED_COLUMNS


def test_merge_scores_keeps_existing(tmp_path):
    scored_p = tmp_path / "scored.parquet"
    merge_scores(scored_p, [_valid_score(job_id="aaaaaaaa", fit_score=80)],
                 scored_by_model="t")
    merge_scores(scored_p, [_valid_score(job_id="bbbbbbbb", fit_score=60)],
                 scored_by_model="t")
    df = pd.read_parquet(scored_p)
    assert set(df["job_id"]) == {"aaaaaaaa", "bbbbbbbb"}
    assert df.set_index("job_id").loc["aaaaaaaa", "fit_score"] == 80.0


def test_merge_scores_overwrites_on_collision(tmp_path):
    """For /rescore: re-judging an existing job_id MUST replace the prior row."""
    scored_p = tmp_path / "scored.parquet"
    merge_scores(scored_p, [_valid_score(job_id="aaaaaaaa", fit_score=60)],
                 scored_by_model="old")
    merge_scores(scored_p, [_valid_score(job_id="aaaaaaaa", fit_score=85)],
                 scored_by_model="new")
    df = pd.read_parquet(scored_p)
    assert len(df) == 1
    assert df.iloc[0]["fit_score"] == 85.0
    assert df.iloc[0]["scored_by_model"] == "new"


def test_merge_scores_raises_on_invalid_subscores(tmp_path):
    scored_p = tmp_path / "scored.parquet"
    bad = _valid_score(fit_score=99,
                       fit_subscores={"title": 25, "skills": 25, "seniority": 15, "domain": 10})
    with pytest.raises(ValueError, match="invalid score"):
        merge_scores(scored_p, [bad], scored_by_model="t")


def test_merge_scores_from_dir_aggregates_batches(tmp_path):
    scored_p = tmp_path / "scored.parquet"
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "batch_001.json").write_text(json.dumps([
        _valid_score(job_id="aaaaaaaa"),
        _valid_score(job_id="bbbbbbbb"),
    ]))
    (staging / "batch_002.json").write_text(json.dumps([
        _valid_score(job_id="cccccccc"),
    ]))
    n = merge_scores_from_dir(scored_p, staging, scored_by_model="t")
    assert n == 3
    df = pd.read_parquet(scored_p)
    assert set(df["job_id"]) == {"aaaaaaaa", "bbbbbbbb", "cccccccc"}


# ---------- prune_scored ----------

def test_prune_scored_drops_obsolete_job_ids(tmp_path):
    clean_p = _make_clean(tmp_path, [{"job_id": "aaaaaaaa"}])
    scored_p = tmp_path / "scored.parquet"
    merge_scores(scored_p, [
        _valid_score(job_id="aaaaaaaa"),
        _valid_score(job_id="bbbbbbbb"),
    ], scored_by_model="t")
    dropped = prune_scored(scored_p, clean_p)
    assert dropped == 1
    df = pd.read_parquet(scored_p)
    assert set(df["job_id"]) == {"aaaaaaaa"}


def test_prune_scored_noop_when_synced(tmp_path):
    clean_p = _make_clean(tmp_path, [{"job_id": "aaaaaaaa"}])
    scored_p = tmp_path / "scored.parquet"
    merge_scores(scored_p, [_valid_score(job_id="aaaaaaaa")], scored_by_model="t")
    assert prune_scored(scored_p, clean_p) == 0


# ---------- validate_scores ----------

def test_validate_scores_clean():
    assert validate_scores([_valid_score()]) == []


def test_validate_scores_subscores_dont_sum():
    bad = _valid_score(fit_score=99,
                       fit_subscores={"title": 25, "skills": 25, "seniority": 15, "domain": 10})
    errs = validate_scores([bad])
    assert any("sum 75 != fit_score 99" in e for e in errs)


def test_validate_scores_missing_field():
    bad = _valid_score()
    del bad["reasoning"]
    errs = validate_scores([bad])
    assert any("missing required field 'reasoning'" in e for e in errs)


def test_validate_scores_bad_label():
    bad = _valid_score(sponsorship_label="maybe_sponsor")
    errs = validate_scores([bad])
    assert any("sponsorship_label" in e for e in errs)


def test_validate_scores_ineligible_needs_evidence():
    bad = _valid_score(sponsorship_label="ineligible", sponsorship_evidence="")
    errs = validate_scores([bad])
    assert any("ineligible label requires non-empty sponsorship_evidence" in e for e in errs)


def test_validate_scores_axis_over_max():
    bad = _valid_score(fit_score=100,
                       fit_subscores={"title": 40, "skills": 40, "seniority": 10, "domain": 10})
    errs = validate_scores([bad])
    assert any("out of range" in e for e in errs)


# ---------- compute_shortlist ----------

def _setup_shortlist(tmp_path, rows: list[dict], scores: list[dict],
                     pipeline_state: dict | None = None):
    clean_p = _make_clean(tmp_path, rows)
    scored_p = tmp_path / "scored.parquet"
    merge_scores(scored_p, scores, scored_by_model="t")
    pipeline_dir = tmp_path / "pipeline"
    if pipeline_state:
        for jid, state_data in pipeline_state.items():
            (pipeline_dir / jid).mkdir(parents=True)
            (pipeline_dir / jid / "state.yaml").write_text(yaml.safe_dump({
                "job_id": jid, **state_data,
            }))
    return clean_p, scored_p, pipeline_dir


def test_compute_shortlist_top_n_cap_and_min_fit(tmp_path):
    # 12 rows with descending fit_scores from 95 down to 40 by 5s
    rows = [{"job_id": f"{i:08x}"} for i in range(12)]
    scores = [_valid_score(job_id=f"{i:08x}", fit_score=95 - i * 5) for i in range(12)]
    clean_p, scored_p, pipe_d = _setup_shortlist(tmp_path, rows, scores)
    out = compute_shortlist(scored_p, clean_p, pipe_d, top_n=10, min_fit=50)
    assert len(out["main"]["sap"]) == 10  # cap at top_n
    assert all(r["fit_score"] >= 50 for r in out["main"]["sap"])  # min_fit floor
    # Rows with fit_score 45 and 40 (i=10, 11) excluded by min_fit
    main_ids = {r["job_id"] for r in out["main"]["sap"]}
    assert f"{10:08x}" not in main_ids
    assert f"{11:08x}" not in main_ids


def test_compute_shortlist_excluded_footer(tmp_path):
    rows = [{"job_id": "aaaaaaaa"}, {"job_id": "bbbbbbbb"}]
    scores = [
        _valid_score(job_id="aaaaaaaa", sponsorship_label="opt_ok", fit_score=80),
        _valid_score(job_id="bbbbbbbb", sponsorship_label="ineligible",
                     sponsorship_evidence="must be a US citizen", fit_score=0,
                     fit_subscores={"title": 0, "skills": 0, "seniority": 0, "domain": 0}),
    ]
    clean_p, scored_p, pipe_d = _setup_shortlist(tmp_path, rows, scores)
    out = compute_shortlist(scored_p, clean_p, pipe_d)
    main_ids = {r["job_id"] for r in out["main"]["sap"]}
    excluded_ids = {r["job_id"] for r in out["excluded"]}
    assert main_ids == {"aaaaaaaa"}
    assert excluded_ids == {"bbbbbbbb"}


def test_compute_shortlist_suppressed_on_first_skip(tmp_path):
    """skip != ineligible. Any skip in state_history -> Suppressed footer."""
    rows = [{"job_id": "aaaaaaaa"}, {"job_id": "bbbbbbbb"}]
    scores = [
        _valid_score(job_id="aaaaaaaa", fit_score=80),
        _valid_score(job_id="bbbbbbbb", fit_score=80),
    ]
    pipeline_state = {
        "bbbbbbbb": {
            "company": "Acme", "title": "X", "state": "skip",
            "state_history": [
                {"state": "saved", "at": "2026-06-01"},
                {"state": "skip",  "at": "2026-06-02"},
            ],
        },
    }
    clean_p, scored_p, pipe_d = _setup_shortlist(tmp_path, rows, scores, pipeline_state)
    out = compute_shortlist(scored_p, clean_p, pipe_d)
    assert {r["job_id"] for r in out["main"]["sap"]} == {"aaaaaaaa"}
    assert {r["job_id"] for r in out["suppressed"]} == {"bbbbbbbb"}
    assert {r["job_id"] for r in out["excluded"]}   == set()  # NOT in excluded; R8


def test_compute_shortlist_sort_fit_score_desc(tmp_path):
    rows = [{"job_id": f"{i:08x}"} for i in range(3)]
    scores = [_valid_score(job_id=f"{i:08x}", fit_score=60 + i * 10) for i in range(3)]
    clean_p, scored_p, pipe_d = _setup_shortlist(tmp_path, rows, scores)
    out = compute_shortlist(scored_p, clean_p, pipe_d)
    # 80 > 70 > 60 — main should be in descending fit_score order
    assert [r["fit_score"] for r in out["main"]["sap"]] == [80, 70, 60]


def test_compute_shortlist_writes_ranks_back_to_scored(tmp_path):
    rows = [{"job_id": "aaaaaaaa"}, {"job_id": "bbbbbbbb"}]
    scores = [
        _valid_score(job_id="aaaaaaaa", fit_score=80),
        _valid_score(job_id="bbbbbbbb", fit_score=70),
    ]
    clean_p, scored_p, pipe_d = _setup_shortlist(tmp_path, rows, scores)
    compute_shortlist(scored_p, clean_p, pipe_d, write_ranks=True)
    df = pd.read_parquet(scored_p).set_index("job_id")
    assert df.loc["aaaaaaaa", "shortlist_rank"] == 1.0
    assert df.loc["bbbbbbbb", "shortlist_rank"] == 2.0


def test_compute_shortlist_empty_when_no_scored(tmp_path):
    out = compute_shortlist(
        tmp_path / "nope.parquet", tmp_path / "nope.parquet", tmp_path / "nope",
    )
    assert out == {"main": {"ai_eng": [], "risk_ai": [], "sap": []}, "excluded": [], "suppressed": []}


def test_dump_shortlist_input_writes_json(tmp_path):
    rows = [{"job_id": "aaaaaaaa"}]
    scores = [_valid_score(job_id="aaaaaaaa", fit_score=80)]
    clean_p, scored_p, pipe_d = _setup_shortlist(tmp_path, rows, scores)
    out_p = tmp_path / "shortlist_input.json"
    dump_shortlist_input(scored_p, clean_p, pipe_d, out_p)
    data = json.loads(out_p.read_text())
    assert "main" in data and "excluded" in data and "suppressed" in data
    assert data["main"]["sap"][0]["job_id"] == "aaaaaaaa"


def test_dump_shortlist_input_preserves_keywords_to_mirror_as_list(tmp_path):
    """Regression: parquet stores list columns as numpy.ndarray. _to_jsonable
    must serialise them as JSON lists, not as the array's str repr (which
    would later iterate per-character and break shortlist rendering)."""
    rows = [{"job_id": "aaaaaaaa"}]
    scores = [_valid_score(
        job_id="aaaaaaaa", fit_score=80,
        keywords_to_mirror=["Agricultural Contract Management", "Source to Pay", "S/4HANA"],
    )]
    clean_p, scored_p, pipe_d = _setup_shortlist(tmp_path, rows, scores)
    out_p = tmp_path / "shortlist_input.json"
    dump_shortlist_input(scored_p, clean_p, pipe_d, out_p)
    data = json.loads(out_p.read_text())
    kw = data["main"]["sap"][0]["keywords_to_mirror"]
    assert isinstance(kw, list)
    assert kw == ["Agricultural Contract Management", "Source to Pay", "S/4HANA"]
    # Anti-regression: NOT a single string of the array's repr
    assert not isinstance(kw, str)
