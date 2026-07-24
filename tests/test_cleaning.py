"""Cleaning tests.

Includes the test that is deliberately separate from plain hash determinism:
test_job_id_url_independent — guards the silent-state-orphaning failure mode
where a hash that includes url or jd_text would flip job_id across re-scrapes
and break pipeline/<job_id>/state.yaml + applications/<dir> keys."""
from __future__ import annotations

import inspect
from pathlib import Path

import pandas as pd
import pytest
import yaml
from rapidfuzz import fuzz

from src import cleaning
from src.cleaning import (
    CLEAN_COLUMNS,
    apply_state_yaml,
    classify_vertical_from_title,
    compute_job_id,
    drop_short_jd,
    drop_stale,
    exact_dedupe,
    near_dedupe,
    normalize_company,
    normalize_title,
    project_raw,
)


# ---------- helpers ----------

def _raw_row(**overrides) -> dict:
    base = {
        "site": "manual",
        "company": "Acme",
        "title": "SAP SD Consultant",
        "location": "Remote",
        "is_remote": False,
        "date_posted": pd.Timestamp("2026-06-01"),
        "scraped_date": pd.Timestamp("2026-06-06"),
        "job_url": "https://example.com/job/1",
        "job_url_direct": "",
        "description": "x" * 250,
        "min_amount": float("nan"),
        "max_amount": float("nan"),
        "currency": "",
        "job_type": "",
        "job_level": "",
        "ingested_run_id": "2026-06-06_1000",
    }
    base.update(overrides)
    return base


def _clean_df(rows: list[dict]) -> pd.DataFrame:
    """Build a df in the canonical clean shape (already normalized + job_id computed)."""
    base = {
        "job_id": "",
        "source": "manual",
        "company": "Acme",
        "company_normalized": "acme",
        "title": "SAP SD Consultant",
        "title_normalized": "sap sd consultant",
        "location": "",
        "remote_flag": False,
        "posted_date": pd.Timestamp("2026-06-01"),
        "posted_date_missing": False,
        "scraped_date": pd.Timestamp("2026-06-06"),
        "url": "",
        "jd_text": "x" * 250,
        "salary_min": float("nan"),
        "salary_max": float("nan"),
        "salary_currency": "",
        "employment_type": "",
        "seniority_raw": "",
        "ingested_run_id": "2026-06-06_1000",
        "already_seen": False,
        "application_status": "",
        "fit_score": float("nan"),
        "fit_subscores": "",
        "sponsorship_label": "unknown",
        "sponsorship_evidence": "",
        "shortlist_rank": float("nan"),
    }
    data = []
    for r in rows:
        m = {**base, **r}
        if not m["job_id"]:
            m["job_id"] = compute_job_id(m["company_normalized"], m["title_normalized"])
        data.append(m)
    return pd.DataFrame(data)


def _make_raw_parquet(
    tmp_path: Path,
    rows: list[dict],
    run_id: str = "2026-06-06_1000",
) -> Path:
    raw_dir = tmp_path / "jobs" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([_raw_row(**r) for r in rows])
    df.to_parquet(raw_dir / f"{run_id}.parquet", index=False)
    return raw_dir


# ---------- T1: job_id determinism ----------

def test_job_id_deterministic():
    a = compute_job_id("acme", "sap sd consultant")
    b = compute_job_id("acme", "sap sd consultant")
    assert a == b
    assert len(a) == 8
    assert all(c in "0123456789abcdef" for c in a)


# ---------- T2: job_id cross-run stability (the separate test) ----------

def test_job_id_url_independent():
    """Same (company_normalized, title_normalized) MUST yield the same job_id
    regardless of url or jd_text. If this ever fails, the hash has started
    swallowing url/jd_text and state.yaml + applications/<dir> keys will
    silently orphan across re-scrapes."""
    # Enforce the invariant at the signature level: compute_job_id must
    # only accept (company_normalized, title_normalized). Adding url or
    # jd_text as parameters would be the regression we're guarding against.
    sig = inspect.signature(compute_job_id)
    assert list(sig.parameters) == ["company_normalized", "title_normalized"], (
        "compute_job_id signature changed — url or jd_text must not be "
        "added to the hash."
    )
    # Same normalized inputs across two simulated runs whose only differences
    # would be the (excluded) url and (excluded) jd_text → identical job_id.
    company_norm = "acme"
    title_norm = "sap sd consultant"
    id_run1 = compute_job_id(company_norm, title_norm)
    id_run2 = compute_job_id(company_norm, title_norm)
    assert id_run1 == id_run2

    # And: an end-to-end check via exact_dedupe — two rows with same
    # (company_norm, title_norm) but different url and different jd_text
    # length collapse to one row that still carries the same job_id as
    # either input would compute. (Defense-in-depth against future code
    # that might recompute job_id post-dedupe from jd_text or url.)
    df = _clean_df([
        {"company_normalized": company_norm, "title_normalized": title_norm,
         "url": "https://board-a.example.com/job/1", "jd_text": "short " * 50},
        {"company_normalized": company_norm, "title_normalized": title_norm,
         "url": "https://board-b.example.com/job/9", "jd_text": "longer " * 200},
    ])
    deduped = exact_dedupe(df)
    assert len(deduped) == 1
    assert deduped.iloc[0]["job_id"] == compute_job_id(company_norm, title_norm)


# ---------- T3: normalize_company ----------

@pytest.mark.parametrize("raw,expected", [
    ("Acme Inc", "acme"),
    ("ACME LLC", "acme"),
    ("The Acme Corp", "acme"),
    ("The Acme", "acme"),
    ("Acme Inc.", "acme"),
    ("Acme, Inc.", "acme"),
    ("Acme  via LinkedIn", "acme"),
    ("Acme Inc via Indeed", "acme"),
    ("  Acme   ", "acme"),
    ("ACME Corp via LinkedIn", "acme"),
])
def test_normalize_company(raw, expected):
    assert normalize_company(raw) == expected


# ---------- T4: normalize_title — seniority preserved ----------

def test_normalize_title_seniority_preserved():
    assert normalize_title("Senior SAP SD Consultant") == "senior sap sd consultant"
    assert normalize_title("Sr. ERP Analyst") == "sr erp analyst"
    assert "senior" in normalize_title("SENIOR sap sd!")
    assert "sr" in normalize_title("Sr. SAP SD")
    # Lead/Principal/Manager preserved too (they trigger seniority penalty in scoring)
    assert "lead" in normalize_title("Lead SAP Consultant")
    assert "principal" in normalize_title("Principal Business Analyst")


# ---------- T5: rapidfuzz near-dedupe boundary + longest-jd wins ----------

def test_rapidfuzz_dedupe_boundary():
    collapse_a = "sap sd consultant"
    collapse_b = "sap sd consultant senior"
    keep_a = "data analyst"
    keep_b = "machine learning engineer"
    # Sanity-pin the pairs to the correct side of the threshold.
    assert fuzz.WRatio(collapse_a, collapse_b) >= 90
    assert fuzz.WRatio(keep_a, keep_b) < 90

    df = _clean_df([
        {"company_normalized": "acme", "title_normalized": collapse_a,
         "jd_text": "a" * 300},
        {"company_normalized": "acme", "title_normalized": collapse_b,
         "jd_text": "a" * 500},   # longer JD — must be the survivor
        {"company_normalized": "beta", "title_normalized": keep_a,
         "jd_text": "b" * 300},
        {"company_normalized": "beta", "title_normalized": keep_b,
         "jd_text": "b" * 300},
    ])
    out = near_dedupe(df)
    acme = out[out["company_normalized"] == "acme"]
    beta = out[out["company_normalized"] == "beta"]
    assert len(acme) == 1, "near-dupes within a company must collapse"
    assert acme["title_normalized"].iloc[0] == collapse_b, "longer jd_text must win"
    assert len(beta) == 2, "distinct titles within a company must NOT collapse"


# ---------- T6: drop_short_jd at exactly 200 chars ----------

def test_drop_short_jd():
    df = _clean_df([
        {"title_normalized": "t1", "jd_text": "x" * 199},
        {"title_normalized": "t2", "jd_text": "x" * 200},
        {"title_normalized": "t3", "jd_text": "x" * 201},
    ])
    out = drop_short_jd(df)
    assert set(out["title_normalized"]) == {"t2", "t3"}


# ---------- T7: drop_stale + posted_date_missing flag ----------

def test_drop_stale():
    today = pd.Timestamp("2026-06-06")
    df = _clean_df([
        {"title_normalized": "t1", "posted_date": today - pd.Timedelta(days=15)},
        {"title_normalized": "t2", "posted_date": today - pd.Timedelta(days=14)},
        {"title_normalized": "t3", "posted_date": today},
        {"title_normalized": "t4", "posted_date": pd.NaT},
    ])
    out = drop_stale(df, today=today)
    assert set(out["title_normalized"]) == {"t2", "t3", "t4"}
    t4 = out[out["title_normalized"] == "t4"].iloc[0]
    assert bool(t4["posted_date_missing"]) is True
    t2 = out[out["title_normalized"] == "t2"].iloc[0]
    assert bool(t2["posted_date_missing"]) is False


# ---------- T8: exact_dedupe keeps longest jd_text ----------

def test_exact_dedupe_keeps_longest_jd():
    df = _clean_df([
        {"company_normalized": "acme", "title_normalized": "sap sd consultant",
         "jd_text": "short " * 40, "url": "https://example.com/short"},
        {"company_normalized": "acme", "title_normalized": "sap sd consultant",
         "jd_text": "longer description text " * 60, "url": "https://example.com/long"},
    ])
    out = exact_dedupe(df)
    assert len(out) == 1
    assert "longer" in out["jd_text"].iloc[0]
    assert out["url"].iloc[0] == "https://example.com/long"


# ---------- T9: end-to-end clean schema is exactly the canonical schema ----------

def test_clean_schema_closed(tmp_path):
    raw_dir = _make_raw_parquet(tmp_path, [
        {"company": "Acme Inc", "title": "SAP SD Consultant",
         "description": "x" * 300, "date_posted": pd.Timestamp("2026-06-01")},
        {"company": "Beta LLC", "title": "ERP Business Analyst",
         "description": "y" * 300, "date_posted": pd.Timestamp("2026-06-02")},
    ])
    out = cleaning.run(
        run_id="2026-06-06_1000",
        raw_dir=raw_dir,
        clean_dir=tmp_path / "jobs",
        runs_dir=tmp_path / "jobs" / "runs",
        pipeline_dir=tmp_path / "pipeline",
        today=pd.Timestamp("2026-06-06"),
    )
    assert list(out.columns) == CLEAN_COLUMNS
    written = pd.read_parquet(tmp_path / "jobs" / "clean.parquet")
    assert list(written.columns) == CLEAN_COLUMNS


# ---------- vertical column: discovery-set passthrough + legacy backfill ----------

def test_project_raw_backfills_missing_vertical_from_title():
    """Legacy raw rows from before the vertical column existed (or any row
    that otherwise reaches here without one) get classified from title
    rather than left empty — 'discovery decides, scoring consumes',
    with cleaning.py as the fallback layer for the no-search-term case."""
    df = pd.DataFrame([
        _raw_row(title="SAP SD Consultant"),
        _raw_row(title="Model Risk Analyst"),
        _raw_row(title="Senior Platform Engineer"),
    ])
    out = project_raw(df)
    assert list(out["vertical"]) == ["sap", "risk_ai", ""]


def test_project_raw_never_overrides_discovery_set_vertical():
    """A row discovery already tagged (e.g. risk_ai via search term, even
    though its title superficially reads sap-adjacent) keeps that tag —
    the title-fallback only fills genuinely empty values."""
    df = pd.DataFrame([
        _raw_row(title="Risk Analyst", vertical="risk_ai"),
    ])
    out = project_raw(df)
    assert out.iloc[0]["vertical"] == "risk_ai"


def test_classify_vertical_sap_strong_signal():
    assert classify_vertical_from_title("SAP ACM Functional Consultant") == "sap"
    assert classify_vertical_from_title("S/4HANA Functional Lead") == "sap"
    assert classify_vertical_from_title("Commodity Trading Operations Lead") == "sap"


def test_classify_vertical_risk_ai_signal():
    assert classify_vertical_from_title("Model Risk Analyst") == "risk_ai"
    assert classify_vertical_from_title("Model Validation Analyst") == "risk_ai"
    assert classify_vertical_from_title("AI Governance Analyst") == "risk_ai"
    assert classify_vertical_from_title("Quantitative Risk Analyst") == "risk_ai"
    assert classify_vertical_from_title("Financial Risk Analyst") == "risk_ai"


def test_classify_vertical_sap_wins_on_ambiguity():
    # title containing both a strong SAP signal and a risk_ai-ish word -> sap
    assert classify_vertical_from_title("SAP Model Risk Analyst") == "sap"


def test_classify_vertical_sap_adjacent_fallback():
    # bare "Risk Analyst" with no SAP-strong or risk_ai-specific phrase -> sap
    assert classify_vertical_from_title("Risk and Controls Analyst") == "sap"


def test_classify_vertical_unclassified():
    assert classify_vertical_from_title("Senior Platform Engineer") == ""
    assert classify_vertical_from_title("") == ""
    assert classify_vertical_from_title(None) == ""


def test_classify_vertical_ai_eng_signal():
    assert classify_vertical_from_title("AI Engineer") == "ai_eng"
    assert classify_vertical_from_title("Forward Deployed Engineer") == "ai_eng"
    # standalone genai/llm tokens catch the AI-qualifier-after-engineer shape
    assert classify_vertical_from_title("Software Engineer - Generative AI") == "ai_eng"
    # ai_eng's rule sits before sap's catch-all, so trad* doesn't pull it away
    assert classify_vertical_from_title("AI Engineer, Trading Systems") == "ai_eng"


def test_classify_vertical_ai_eng_collisions_keep_prior_verticals():
    # risk_ai's rule sits before ai_eng's — AI Risk/Governance titles stay risk_ai
    assert classify_vertical_from_title("AI Risk Engineer") == "risk_ai"
    assert classify_vertical_from_title("AI Governance Analyst") == "risk_ai"
    # sap's strong-signal rule is still first
    assert classify_vertical_from_title("SAP AI Engineer") == "sap"
    # bare "Risk Analyst" still lands on the sap catch-all
    assert classify_vertical_from_title("Risk Analyst") == "sap"
    # Machine Learning Engineer is out-of-lane by charter (locked 2026-07-13)
    assert classify_vertical_from_title("Machine Learning Engineer") == ""


def test_classify_vertical_agrees_with_search_terms(cfg):
    """The title-fallback classifier must agree with how discovery.py tags a
    row by search term — otherwise a manual inbox clip or legacy row for the
    exact same title silently lands in the wrong rubric. Caught a real bug
    where "AI Compliance Analyst" (a risk_ai search term) fell through to the
    sap-adjacent "compliance" catch-all instead of matching risk_ai. Iterates
    the config so every current AND future vertical is covered."""
    for vertical in cfg.verticals.values():
        for term in vertical.search_terms + vertical.linkedin_terms:
            assert classify_vertical_from_title(term) == vertical.name, term


# ---------- T10: already_seen + application_status from state.yaml ----------

def test_already_seen_from_state_yaml(tmp_path):
    df = _clean_df([
        {"company_normalized": "acme", "title_normalized": "sap sd consultant"},
        {"company_normalized": "beta", "title_normalized": "erp business analyst"},
    ])
    matched_id = df.iloc[0]["job_id"]
    unmatched_id = df.iloc[1]["job_id"]

    pipeline_dir = tmp_path / "pipeline"
    (pipeline_dir / matched_id).mkdir(parents=True)
    (pipeline_dir / matched_id / "state.yaml").write_text(yaml.safe_dump({
        "job_id": matched_id,
        "company": "Acme",
        "title": "SAP SD Consultant",
        "state": "tailored",
    }))

    out = apply_state_yaml(df, pipeline_dir)
    matched = out[out["job_id"] == matched_id].iloc[0]
    unmatched = out[out["job_id"] == unmatched_id].iloc[0]
    assert bool(matched["already_seen"]) is True
    assert matched["application_status"] == "tailored"
    assert bool(unmatched["already_seen"]) is False
    assert unmatched["application_status"] == ""


# ---------- T11: cleaning idempotent within a day ----------

def test_cleaning_idempotent(tmp_path):
    raw_dir = _make_raw_parquet(tmp_path, [
        {"company": "Acme Inc", "title": "SAP SD Consultant",
         "description": "x" * 300, "date_posted": pd.Timestamp("2026-06-01")},
        {"company": "Beta LLC", "title": "ERP Business Analyst",
         "description": "y" * 300, "date_posted": pd.Timestamp("2026-06-02")},
    ])
    kwargs = dict(
        raw_dir=raw_dir,
        clean_dir=tmp_path / "jobs",
        runs_dir=tmp_path / "jobs" / "runs",
        pipeline_dir=tmp_path / "pipeline",
        today=pd.Timestamp("2026-06-06"),
    )
    out1 = cleaning.run(run_id="2026-06-06_1000", **kwargs)
    out2 = cleaning.run(run_id="2026-06-06_1000", **kwargs)
    pd.testing.assert_frame_equal(
        out1.reset_index(drop=True), out2.reset_index(drop=True)
    )
