"""Cleaning tests.

Includes the test that is deliberately separate from plain hash determinism:
test_job_id_url_independent — guards the silent-state-orphaning failure mode
where a hash that includes url or jd_text would flip job_id across re-scrapes
and break pipeline/<job_id>/state.yaml + applications/<dir> keys."""
from __future__ import annotations

import inspect
import json
import re
import warnings
from pathlib import Path

import pandas as pd
import pytest
import yaml
from rapidfuzz import fuzz

from src.discovery import cleaning
from src.discovery.cleaning import (
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
    load_raw_window,
)


# ---------- helpers ----------

def _raw_row(**overrides) -> dict:
    base = {
        "site": "manual",
        "company": "Acme",
        "title": "Widget Functional Consultant",
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
        "title": "Widget Functional Consultant",
        "title_normalized": "widget functional consultant",
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
    a = compute_job_id("acme", "widget functional consultant")
    b = compute_job_id("acme", "widget functional consultant")
    assert a == b
    assert len(a) == 8
    assert all(c in "0123456789abcdef" for c in a)


# ---------- T2: job_id cross-run stability (the separate test) ----------

# Golden values, not self-comparisons. These pin sha1, the "|" separator, utf-8
# encoding and the 8-char truncation in one place. A change here is never a test
# failure to fix: job_id keys every pipeline/<job_id>/state.yaml and
# applications/<dir> on disk, so a new value orphans real data.
@pytest.mark.parametrize("company,title,expected", [
    ("acme", "widget functional consultant", "1c08ad22"),
    ("a", "b", "9abe6de2"),
    ("", "", "3eb41622"),
    # Non-ascii: pins utf-8 specifically, not the platform's default codec.
    ("josé álvarez gmbh", "señor widget engineer", "50e5989f"),
])
def test_job_id_golden_values(company, title, expected):
    assert compute_job_id(company, title) == expected


def test_job_id_separator_collision_is_unreachable_after_normalization():
    """The key is joined on "|" with no escaping, so these two genuinely collide.
    What makes it unreachable is that both normalizers turn "|" into a space.
    Escaping the separator would rehash every row on disk, so the normalizers
    are the invariant to guard, not the join."""
    assert compute_job_id("a|b", "c") == compute_job_id("a", "b|c")
    assert "|" not in normalize_company("Acme | Widgets")
    assert "|" not in normalize_title("Engineer | Platform")


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
    company_norm = "acme"
    title_norm = "widget functional consultant"

    # End-to-end via exact_dedupe — two rows with same
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
    ("Databricks Inc.", "databricks"),
    ("Databricks", "databricks"),
    ("databricks, inc", "databricks"),
    ("Best Co Labs", "best co labs"),
    ("Coinbase", "coinbase"),
])
def test_normalize_company(raw, expected):
    assert normalize_company(raw) == expected


# ---------- T4: normalize_title — seniority preserved ----------

def test_normalize_title_seniority_preserved():
    assert normalize_title("Senior Widget Functional Consultant") == "senior widget functional consultant"
    assert normalize_title("Sr. Gizmo Analyst") == "sr gizmo analyst"
    assert "senior" in normalize_title("SENIOR widget fn!")
    assert "sr" in normalize_title("Sr. Widget FN")
    # Lead/Principal/Manager preserved too (they trigger seniority penalty in scoring)
    assert "lead" in normalize_title("Lead Widget Consultant")
    assert "principal" in normalize_title("Principal Business Analyst")


# ---------- T5: rapidfuzz near-dedupe boundary + longest-jd wins ----------

def test_rapidfuzz_dedupe_boundary():
    collapse_a = "widget functional consultant"
    collapse_b = "widget functional consultant remote"
    keep_a = "data analyst"
    keep_b = "cog learning engineer"
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


# Every pair here scores >= 90 on title alone, so ratio by itself deletes a
# real posting. The survivor is picked by JD length, which is not stable across
# re-scrapes, so the lost job_id varies run to run.
@pytest.mark.parametrize("title_a,title_b", [
    ("data analyst", "data analyst intern"),
    ("software engineer", "software engineer manager"),
    ("machine learning engineer i", "machine learning engineer ii"),
    ("data engineer", "lead data engineer"),
    ("widget consultant", "senior widget consultant"),
])
def test_near_dedupe_keeps_titles_that_differ_only_by_level(title_a, title_b):
    assert fuzz.WRatio(title_a, title_b) >= 90, "pair must be a ratio near-dup"
    df = _clean_df([
        {"company_normalized": "acme", "title_normalized": title_a, "jd_text": "a" * 300},
        {"company_normalized": "acme", "title_normalized": title_b, "jd_text": "a" * 500},
    ])
    out = near_dedupe(df)
    assert sorted(out["title_normalized"]) == sorted([title_a, title_b])


# The guard compares canonical level sets. If it compared raw tokens, every
# pair here would be exempted from collapsing and the same req scraped from two
# boards with different spellings would survive twice, with two job_ids.
@pytest.mark.parametrize("title_a,title_b", [
    ("junior software engineer", "jr software engineer"),
    ("senior widget consultant", "sr widget consultant"),
    ("associate product manager", "associate product mgr"),
    ("technical program manager", "technical program management"),
    ("machine learning engineer iii", "machine learning engineer 3"),
    ("graduate research assistant", "grad research assistant"),
    ("data analyst intern", "data analyst internship"),
    ("widget engineer co op", "widget engineer coop"),
])
def test_near_dedupe_collapses_abbreviated_spellings_of_one_level(title_a, title_b):
    df = _clean_df([
        {"company_normalized": "acme", "title_normalized": title_a, "jd_text": "a" * 300},
        {"company_normalized": "acme", "title_normalized": title_b, "jd_text": "a" * 500},
    ])
    assert fuzz.WRatio(title_a, title_b) >= 90, "pair must be a ratio near-dup"
    assert len(near_dedupe(df)) == 1


def test_near_dedupe_still_collapses_when_levels_match():
    """The guard compares level tokens, so two titles that share one still
    collapse on ratio."""
    df = _clean_df([
        {"company_normalized": "acme", "title_normalized": "senior widget consultant",
         "jd_text": "a" * 300},
        {"company_normalized": "acme", "title_normalized": "senior widget consultants",
         "jd_text": "a" * 500},
    ])
    assert len(near_dedupe(df)) == 1


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
    # source must be one that ages: _clean_df defaults to "manual", which is
    # staleness-exempt along with the career boards.
    today = pd.Timestamp("2026-06-06")
    df = _clean_df([
        {"source": "indeed", "title_normalized": "t1", "posted_date": today - pd.Timedelta(days=15)},
        {"source": "indeed", "title_normalized": "t2", "posted_date": today - pd.Timedelta(days=14)},
        {"source": "indeed", "title_normalized": "t3", "posted_date": today},
        {"source": "indeed", "title_normalized": "t4", "posted_date": pd.NaT},
    ])
    out = drop_stale(df, today=today)
    assert set(out["title_normalized"]) == {"t2", "t3", "t4"}
    t4 = out[out["title_normalized"] == "t4"].iloc[0]
    assert bool(t4["posted_date_missing"]) is True
    t2 = out[out["title_normalized"] == "t2"].iloc[0]
    assert bool(t2["posted_date_missing"]) is False


def test_drop_stale_survives_one_tz_aware_posted_date():
    """One tz-aware value makes the column object dtype; a non-utc parse would
    coerce every naive value to NaT and keep the whole frame."""
    today = pd.Timestamp("2026-08-06")
    df = _clean_df([
        {"source": "indeed", "title_normalized": "aware_fresh",
         "posted_date": pd.Timestamp("2026-08-01T10:00:00Z")},
        {"source": "indeed", "title_normalized": "naive_fresh",
         "posted_date": pd.Timestamp("2026-08-04")},
        {"source": "indeed", "title_normalized": "naive_stale",
         "posted_date": pd.Timestamp("2026-01-01")},
    ])
    assert df["posted_date"].dtype == object  # the trigger condition
    out = drop_stale(df, today=today)
    assert set(out["title_normalized"]) == {"aware_fresh", "naive_fresh"}
    assert out["posted_date"].dtype == "datetime64[ns]"
    assert not out["posted_date_missing"].any()


def test_drop_stale_survives_mixed_offset_strings():
    today = pd.Timestamp("2026-08-06")
    df = _clean_df([
        {"source": "indeed", "title_normalized": "utc", "posted_date": "2026-08-01T10:00:00+00:00"},
        {"source": "indeed", "title_normalized": "est", "posted_date": "2026-08-01T10:00:00-05:00"},
        {"source": "indeed", "title_normalized": "stale", "posted_date": "2026-01-01"},
    ])
    out = drop_stale(df, today=today)
    assert set(out["title_normalized"]) == {"utc", "est"}


# ---------- T8: exact_dedupe keeps longest jd_text ----------

def test_exact_dedupe_keeps_longest_jd():
    df = _clean_df([
        {"company_normalized": "acme", "title_normalized": "widget functional consultant",
         "jd_text": "short " * 40, "url": "https://example.com/short"},
        {"company_normalized": "acme", "title_normalized": "widget functional consultant",
         "jd_text": "longer description text " * 60, "url": "https://example.com/long"},
    ])
    out = exact_dedupe(df)
    assert len(out) == 1
    assert "longer" in out["jd_text"].iloc[0]
    assert out["url"].iloc[0] == "https://example.com/long"


def test_blank_company_rows_are_dropped_not_collapsed(tmp_path):
    """A null company from JobSpy normalizes to "", so job_id becomes a function
    of the title alone and two rows from different employers collide — one is
    then silently deleted by exact_dedupe."""
    raw_dir = _make_raw_parquet(tmp_path, [
        {"company": None, "title": "Widget Functional Consultant",
         "description": "x" * 300, "date_posted": pd.Timestamp("2026-06-01")},
        {"company": "   ", "title": "Widget Functional Consultant",
         "description": "y" * 300, "date_posted": pd.Timestamp("2026-06-02")},
        {"company": "Acme Inc", "title": "Widget Functional Consultant",
         "description": "z" * 300, "date_posted": pd.Timestamp("2026-06-02")},
    ])
    out = cleaning.run(
        run_id="2026-06-06_1000",
        raw_dir=raw_dir,
        clean_dir=tmp_path / "jobs",
        runs_dir=tmp_path / "jobs" / "runs",
        pipeline_dir=tmp_path / "pipeline",
        today=pd.Timestamp("2026-06-06"),
    )
    assert list(out["company_normalized"]) == ["acme"]
    report = (tmp_path / "jobs" / "runs" / "2026-06-06_1000.md").read_text(encoding="utf-8")
    assert "after blank-company drop: 1 (dropped 2)" in report


# ---------- T9: end-to-end clean schema is exactly the canonical schema ----------

def test_clean_schema_closed(tmp_path):
    raw_dir = _make_raw_parquet(tmp_path, [
        {"company": "Acme Inc", "title": "Widget Functional Consultant",
         "description": "x" * 300, "date_posted": pd.Timestamp("2026-06-01")},
        {"company": "Beta LLC", "title": "Gizmo Business Analyst",
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
        _raw_row(title="Widget Functional Consultant"),
        _raw_row(title="Sprocket Risk Analyst"),
        _raw_row(title="Senior Platform Engineer"),
    ])
    out = project_raw(df)
    assert list(out["vertical"]) == ["example_primary", "example_secondary", ""]


def test_project_raw_never_overrides_discovery_set_vertical():
    """A row discovery already tagged (e.g. secondary via search term, even
    though its title superficially reads primary-adjacent) keeps that tag —
    the title-fallback only fills genuinely empty values."""
    df = pd.DataFrame([
        _raw_row(title="Risk Analyst", vertical="example_secondary"),
    ])
    out = project_raw(df)
    assert out.iloc[0]["vertical"] == "example_secondary"


def test_classify_vertical_sap_strong_signal():
    assert classify_vertical_from_title("Widget Assembly Functional Consultant") == "example_primary"
    assert classify_vertical_from_title("Doohickey Functional Lead") == "example_primary"
    assert classify_vertical_from_title("Gizmo Trading Operations Lead") == "example_primary"


def test_classify_vertical_secondary_signal():
    assert classify_vertical_from_title("Sprocket Risk Analyst") == "example_secondary"
    assert classify_vertical_from_title("Sprocket Validation Analyst") == "example_secondary"
    assert classify_vertical_from_title("Cog Governance Analyst") == "example_secondary"
    assert classify_vertical_from_title("Quantitative Sprocket Analyst") == "example_secondary"
    assert classify_vertical_from_title("Sprocket Compliance Analyst") == "example_secondary"


def test_classify_vertical_sap_wins_on_ambiguity():
    # title containing both a strong primary signal and a secondary-ish word -> primary
    assert classify_vertical_from_title("Widget Sprocket Risk Analyst") == "example_primary"


def test_classify_vertical_sap_adjacent_fallback():
    # bare "Risk Analyst" with no primary-strong or secondary-specific phrase -> primary
    assert classify_vertical_from_title("Risk and Controls Analyst") == "example_primary"


def test_classify_vertical_unclassified():
    assert classify_vertical_from_title("Senior Platform Engineer") == ""
    assert classify_vertical_from_title("") == ""
    assert classify_vertical_from_title(None) == ""


def test_classify_vertical_tertiary_signal():
    assert classify_vertical_from_title("Cog Engineer") == "example_tertiary"
    assert classify_vertical_from_title("Forward Deployed Engineer") == "example_tertiary"
    # a trailing lane qualifier still classifies, not just a leading one
    assert classify_vertical_from_title("Software Engineer - Applied Cog") == "example_tertiary"
    # tertiary's rules sit before primary's catch-all, so a catch-all word
    # in the same title doesn't pull it away
    assert classify_vertical_from_title("Cog Engineer, Operations") == "example_tertiary"


def test_classify_vertical_tertiary_collisions_keep_prior_verticals():
    # secondary's rule sits before tertiary's — risk/governance titles stay secondary
    assert classify_vertical_from_title("Cog Risk Engineer") == "example_secondary"
    assert classify_vertical_from_title("Cog Governance Analyst") == "example_secondary"
    # example_primary's strong-signal rule is still first
    assert classify_vertical_from_title("Widget Cog Engineer") == "example_primary"
    # bare "Risk Analyst" still lands on the example_primary catch-all
    assert classify_vertical_from_title("Risk Analyst") == "example_primary"
    # the compound rule needs its qualifier — bare, this is out-of-lane
    assert classify_vertical_from_title("Cog Learning Engineer") == ""


def test_classify_vertical_agrees_with_search_terms(cfg):
    """The title-fallback classifier must agree with how discovery.py tags a
    row by search term — otherwise a manual inbox clip or legacy row for the
    exact same title silently lands in the wrong rubric. Caught a real bug
    where a secondary-lane search term fell through to the primary-adjacent
    catch-all instead of matching its own lane. Iterates the config so every
    current AND future vertical is covered."""
    for vertical in cfg.verticals.values():
        for term in vertical.search_terms + vertical.linkedin_terms:
            assert classify_vertical_from_title(term) == vertical.name, term


# ---------- T10: already_seen + application_status from state.yaml ----------

def test_already_seen_from_state_yaml(tmp_path):
    df = _clean_df([
        {"company_normalized": "acme", "title_normalized": "widget functional consultant"},
        {"company_normalized": "beta", "title_normalized": "gizmo business analyst"},
    ])
    matched_id = df.iloc[0]["job_id"]
    unmatched_id = df.iloc[1]["job_id"]

    pipeline_dir = tmp_path / "pipeline"
    (pipeline_dir / matched_id).mkdir(parents=True)
    (pipeline_dir / matched_id / "state.yaml").write_text(yaml.safe_dump({
        "job_id": matched_id,
        "company": "Acme",
        "title": "Widget Functional Consultant",
        "state": "tailored",
    }), encoding="utf-8")

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
        {"company": "Acme Inc", "title": "Widget Functional Consultant",
         "description": "x" * 300, "date_posted": pd.Timestamp("2026-06-01")},
        {"company": "Beta LLC", "title": "Gizmo Business Analyst",
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


# ---------- run-report stat chain ----------

def test_report_dropped_stats_chain_off_predecessor(tmp_path):
    """Every `dropped_*` must equal its own stage's loss (previous_after -
    current_after). dropped_short used to subtract from raw_rows, re-counting
    every title-gate drop."""
    # site must not be "manual" — manual rows are exempt from the title gate.
    raw_dir = _make_raw_parquet(tmp_path, [
        # survives every stage
        {"site": "linkedin", "company": "Acme", "title": "Widget Functional Consultant",
         "description": "x" * 300},
        # classify as example_primary, then trip a title_exclude_term -> gate drops both
        {"site": "linkedin", "company": "Beta", "title": "Widget Clinical Data Consultant",
         "description": "y" * 300},
        {"site": "linkedin", "company": "Gamma", "title": "Widget MM Nurse Lead",
         "description": "z" * 300},
        # in-lane but too short -> the only legitimate short-JD drop
        {"site": "linkedin", "company": "Delta", "title": "Widget Functional Consultant",
         "description": "q" * 10},
    ])
    cleaning.run(
        run_id="2026-06-06_1000",
        raw_dir=raw_dir,
        clean_dir=tmp_path / "jobs",
        runs_dir=tmp_path / "jobs" / "runs",
        pipeline_dir=tmp_path / "pipeline",
        today=pd.Timestamp("2026-06-06"),
    )
    report = (tmp_path / "jobs" / "runs" / "2026-06-06_1000.md").read_text(encoding="utf-8")

    def stage(label: str) -> tuple[int, int]:
        """Return (after, dropped) for a report line."""
        line = next(ln for ln in report.splitlines() if ln.startswith(f"- {label}"))
        after = int(re.search(r":\s*(\d+)", line).group(1))
        dropped = int(re.search(r"\(dropped (\d+)\)", line).group(1))
        return after, dropped

    raw_rows = int(re.search(r"- raw rows loaded: (\d+)", report).group(1))
    assert raw_rows == 4

    excl_after, excl_dropped = stage("after classification/exclusion")
    short_after, short_dropped = stage("after short-JD drop")

    # The title gate ate 2 rows; the short-JD filter ate exactly 1.
    assert (excl_after, excl_dropped) == (2, 2)
    assert (short_after, short_dropped) == (1, 1)
    # The regression: raw_rows - short_after would have reported 3.
    assert short_dropped != raw_rows - short_after

    # Whole chain must telescope: each dropped == predecessor_after - after.
    prev = short_after
    for label in ("after stale drop", "after exact dedupe", "after near dedupe",
                  "after seen-ledger expiry", "after location filter"):
        after, dropped = stage(label)
        assert dropped == prev - after, f"{label}: {dropped} != {prev} - {after}"
        prev = after


# ---------- T12: career-board staleness exemption ----------

def test_drop_stale_exempts_career_sources():
    today = pd.Timestamp("2026-07-15")
    old = pd.Timestamp("2026-05-01")  # far past the 14-day cutoff
    df = pd.DataFrame([
        {"source": "greenhouse", "posted_date": old},
        {"source": "lever", "posted_date": old},
        {"source": "ashby", "posted_date": old},
        {"source": "linkedin", "posted_date": old},
        {"source": "linkedin", "posted_date": pd.Timestamp("2026-07-10")},
    ])
    out = drop_stale(df, today=today)
    assert sorted(out["source"]) == ["ashby", "greenhouse", "lever", "linkedin"]
    assert pd.Timestamp("2026-05-01") not in set(out[out["source"] == "linkedin"]["posted_date"])


def test_drop_stale_exempts_manual_adds():
    """ingest_url rewrites site to "manual" on every path, including the ATS
    ones, so without this a board URL for a still-live older req is dropped and
    ingest() blames JS rendering."""
    today = pd.Timestamp("2026-07-15")
    df = pd.DataFrame([
        {"source": "manual", "posted_date": pd.Timestamp("2026-05-01")},
        {"source": "indeed", "posted_date": pd.Timestamp("2026-05-01")},
    ])
    out = drop_stale(df, today=today)
    assert list(out["source"]) == ["manual"]


# ---------- T13: seen-ledger ----------

def _ledger(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{
        "job_id": r["job_id"],
        "first_seen": pd.Timestamp(r["first_seen"]),
        "last_score": r.get("last_score", float("nan")),
    } for r in rows])


def test_update_seen_ledger_stamps_new_ids(tmp_path):
    ledger_path = tmp_path / "seen.parquet"
    out = cleaning.update_seen_ledger(
        ["aaaa1111", "bbbb2222"], ledger_path, tmp_path / "scored.parquet",
        today=pd.Timestamp("2026-07-15"),
    )
    assert sorted(out["job_id"]) == ["aaaa1111", "bbbb2222"]
    assert (out["first_seen"] == pd.Timestamp("2026-07-15")).all()
    assert out["last_score"].isna().all()
    assert ledger_path.exists()


def test_update_seen_ledger_keeps_first_seen_and_refreshes_score(tmp_path):
    ledger_path = tmp_path / "seen.parquet"
    _ledger([{"job_id": "aaaa1111", "first_seen": "2026-07-01"}]).to_parquet(
        ledger_path, index=False
    )
    pd.DataFrame([{"job_id": "aaaa1111", "fit_score": 85.0}]).to_parquet(
        tmp_path / "scored.parquet", index=False
    )
    out = cleaning.update_seen_ledger(
        ["aaaa1111"], ledger_path, tmp_path / "scored.parquet",
        today=pd.Timestamp("2026-07-15"),
    )
    row = out[out["job_id"] == "aaaa1111"].iloc[0]
    assert row["first_seen"] == pd.Timestamp("2026-07-01")  # never restamped
    assert row["last_score"] == 85.0


def test_update_seen_ledger_purges_after_resurface_window(tmp_path):
    # low tier: expiry 15d + resurface 60d = forgotten 75d after first_seen
    ledger_path = tmp_path / "seen.parquet"
    _ledger([
        {"job_id": "old11111", "first_seen": "2026-04-01"},               # 105d old -> purged
        {"job_id": "high1111", "first_seen": "2026-04-01", "last_score": 90.0},  # 60+60=120d -> kept
        {"job_id": "new22222", "first_seen": "2026-07-10"},               # kept
    ]).to_parquet(ledger_path, index=False)
    out = cleaning.update_seen_ledger(
        [], ledger_path, tmp_path / "scored.parquet", today=pd.Timestamp("2026-07-15"),
    )
    assert sorted(out["job_id"]) == ["high1111", "new22222"]


def test_update_seen_ledger_purged_id_resurfaces_as_new(tmp_path):
    ledger_path = tmp_path / "seen.parquet"
    _ledger([{"job_id": "old11111", "first_seen": "2026-04-01"}]).to_parquet(
        ledger_path, index=False
    )
    out = cleaning.update_seen_ledger(
        ["old11111"], ledger_path, tmp_path / "scored.parquet",
        today=pd.Timestamp("2026-07-15"),
    )
    row = out[out["job_id"] == "old11111"].iloc[0]
    assert row["first_seen"] == pd.Timestamp("2026-07-15")  # fresh clock
    assert pd.isna(row["last_score"])  # will be re-judged


def test_apply_expiry_tiers_and_tracked_exemption():
    today = pd.Timestamp("2026-07-15")
    ledger = _ledger([
        {"job_id": "low11111", "first_seen": "2026-06-01"},                    # 44d, low tier -> expired
        {"job_id": "high1111", "first_seen": "2026-06-01", "last_score": 85.0},  # 44d, high tier -> visible
        {"job_id": "trak1111", "first_seen": "2026-06-01"},                    # expired but tracked
        {"job_id": "new11111", "first_seen": "2026-07-10"},                    # 5d -> visible
    ])
    df = _clean_df([
        {"job_id": "low11111", "title_normalized": "a"},
        {"job_id": "high1111", "title_normalized": "b"},
        {"job_id": "trak1111", "title_normalized": "c", "already_seen": True},
        {"job_id": "new11111", "title_normalized": "d"},
    ])
    out = cleaning.apply_expiry(df, ledger, today)
    assert sorted(out["job_id"]) == ["high1111", "new11111", "trak1111"]


def test_load_raw_window_shards(tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    
    # legacy file
    pd.DataFrame([{"id": 1, "source": "jobspy"}]).to_parquet(raw_dir / "2026-07-15_1200.parquet")
    # new shards
    pd.DataFrame([{"id": 2, "source": "greenhouse"}]).to_parquet(raw_dir / "2026-07-15_1200_greenhouse.parquet")
    pd.DataFrame([{"id": 3, "source": "lever"}]).to_parquet(raw_dir / "2026-07-15_1200_lever.parquet")
    # stale file (should be ignored)
    pd.DataFrame([{"id": 4, "source": "old"}]).to_parquet(raw_dir / "2026-05-01_1200.parquet")
    
    df = load_raw_window(raw_dir, today=pd.Timestamp("2026-07-16"))
    assert len(df) == 3
    assert set(df["source"]) == {"jobspy", "greenhouse", "lever"}


def _salary_shards(raw_dir: Path) -> None:
    """A board shard (no salary at all) beside a job-site shard that has one."""
    pd.DataFrame({"id": [1, 2], "min_amount": [None, None]}).to_parquet(
        raw_dir / "2026-07-15_1200_greenhouse.parquet"
    )
    pd.DataFrame({"id": [3], "min_amount": [90000.0]}).to_parquet(
        raw_dir / "2026-07-15_1200_indeed.parquet"
    )


def test_load_raw_window_pins_numeric_dtype_past_all_na_shards(tmp_path):
    """Guards salary_min/salary_max against a pandas bump.

    Boards leave min_amount/max_amount entirely null, so those shards land as
    object dtype. Pandas currently drops all-NA columns when inferring the
    concat result dtype; a future version will not, which would turn the whole
    column object and silently break the numeric salary fields downstream.
    """
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    _salary_shards(raw_dir)

    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        df = load_raw_window(raw_dir, today=pd.Timestamp("2026-07-16"))

    assert df["min_amount"].dtype == "float64"
    assert len(df) == 3
    assert df.loc[df["id"] == 3, "min_amount"].iloc[0] == 90000.0
    assert df.loc[df["id"].isin([1, 2]), "min_amount"].isna().all()


def test_load_raw_window_keeps_column_all_na_in_every_shard(tmp_path):
    """Only columns typed by some other shard are excluded — never a column
    that is empty everywhere, which must survive for project_raw to default."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    pd.DataFrame({"id": [1], "job_level": [None]}).to_parquet(
        raw_dir / "2026-07-15_1200_greenhouse.parquet"
    )
    pd.DataFrame({"id": [2], "job_level": [None]}).to_parquet(
        raw_dir / "2026-07-15_1200_lever.parquet"
    )

    df = load_raw_window(raw_dir, today=pd.Timestamp("2026-07-16"))
    assert "job_level" in df.columns
    assert len(df) == 2
    assert df["job_level"].isna().all()


def test_load_raw_window_zero_row_shard_does_not_set_dtype(tmp_path):
    """The manual shard is written empty on every run with an inbox miss; it
    must not drive the dtype of a column real shards populate."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    empty = pd.DataFrame({"id": [], "is_remote": pd.Series([], dtype=object)})
    empty.to_parquet(raw_dir / "2026-07-15_1200_manual.parquet")
    pd.DataFrame({"id": [1], "is_remote": [True]}).to_parquet(
        raw_dir / "2026-07-15_1200_indeed.parquet"
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        df = load_raw_window(raw_dir, today=pd.Timestamp("2026-07-16"))

    assert len(df) == 1
    assert df["is_remote"].dtype == "bool"


def test_load_raw_window_all_shards_empty_keeps_columns(tmp_path):
    """Nothing scraped anywhere: still return the column shape, not a bare
    frame, so the pipeline's column lookups stay valid."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    cols = pd.DataFrame({"id": [], "company": [], "title": []})
    cols.to_parquet(raw_dir / "2026-07-15_1200_manual.parquet")
    cols.to_parquet(raw_dir / "2026-07-15_1200_greenhouse.parquet")

    df = load_raw_window(raw_dir, today=pd.Timestamp("2026-07-16"))
    assert df.empty
    assert list(df.columns) == ["id", "company", "title"]


def test_project_raw_remote_flag_no_future_warning():
    """remote_flag arrives object-typed from mixed shards; the fillna must not
    lean on the deprecated silent downcast."""
    df = pd.DataFrame({"remote_flag": pd.Series([True, None, False], dtype=object)})
    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        out = project_raw(df)

    assert out["remote_flag"].dtype == "bool"
    assert list(out["remote_flag"]) == [True, False, False]


def test_apply_expiry_boundary_day_still_visible():
    # visible THROUGH first_seen + 15d; dropped strictly after
    ledger = _ledger([{"job_id": "edge1111", "first_seen": "2026-07-01"}])
    df = _clean_df([{"job_id": "edge1111"}])
    assert len(cleaning.apply_expiry(df, ledger, pd.Timestamp("2026-07-16"))) == 1
    assert len(cleaning.apply_expiry(df, ledger, pd.Timestamp("2026-07-17"))) == 0


# ---------- T14: location filter ----------

def test_location_filter():
    from src.discovery.config import DiscoveryConfig, LocationAllowlist
    from src.discovery.cleaning import filter_and_canonicalize_location
    
    cfg = DiscoveryConfig(
        location_allowlist=LocationAllowlist(countries=["United States"])
    )
    df = pd.DataFrame({
        "job_id": ["1", "2", "3", "4", "5", "6"],
        "location": ["Austin, Texas", "Berlin, Germany", "", "Remote", "San Francisco, CA", "London, UK"]
    })
    
    out = filter_and_canonicalize_location(df, cfg)
    assert len(out) == 4

    locs = out.set_index("job_id")["location"].to_dict()
    assert locs["1"] == "Austin, TX"
    assert locs["3"] == ""
    assert locs["4"] == "Remote"
    assert locs["5"] == "San Francisco, CA"


def test_location_filter_drops_bare_foreign_cities():
    # Regression: bare foreign-city strings (no country suffix) used to parse
    # to nothing and slip through the US allowlist onto the shortlist.
    from src.discovery.config import DiscoveryConfig, LocationAllowlist
    from src.discovery.cleaning import filter_and_canonicalize_location

    cfg = DiscoveryConfig(
        location_allowlist=LocationAllowlist(countries=["United States"])
    )
    df = pd.DataFrame({
        "job_id": ["kept_us", "kept_namesake", "drop_london", "drop_stockholm",
                   "drop_iasi", "drop_kl"],
        "location": ["Austin, TX", "Paris, TX", "London", "Stockholm",
                     "Iasi", "Kuala Lumpur"],
    })

    out = filter_and_canonicalize_location(df, cfg)

    assert set(out["job_id"]) == {"kept_us", "kept_namesake"}
    locs = out.set_index("job_id")["location"].to_dict()
    assert locs["kept_us"] == "Austin, TX"
    assert locs["kept_namesake"] == "Paris, TX"  # US namesake, not dropped


def test_out_of_allowlist_rows_never_enter_the_seen_ledger(tmp_path, monkeypatch):
    """The location filter runs before the seen-ledger. Otherwise a foreign row
    gets first_seen stamped while invisible, and widening the allowlist surfaces
    it already expired — the change looks like a no-op for RESURFACE_AFTER_DAYS."""
    from src.discovery.config import DiscoveryConfig, LocationAllowlist

    us_only = DiscoveryConfig(location_allowlist=LocationAllowlist(countries=["United States"]))
    monkeypatch.setattr(cleaning, "load_config", lambda *a, **k: us_only)

    rows = [
        {"company": "Acme", "title": "Widget Functional Consultant", "location": "Austin, TX"},
        {"company": "Beta", "title": "Widget MM Consultant", "location": "Bengaluru, India"},
    ]
    jobs = tmp_path / "jobs"
    raw_dir = _make_raw_parquet(tmp_path, rows, run_id="2026-06-06_1000")
    cleaning.run(
        run_id="2026-06-06_1000",
        raw_dir=raw_dir,
        clean_dir=jobs,
        runs_dir=jobs / "runs",
        pipeline_dir=tmp_path / "pipeline",
        today=pd.Timestamp("2026-06-06"),
    )
    india_id = compute_job_id(normalize_company("Beta"), normalize_title("Widget MM Consultant"))
    ledger_ids = set(pd.read_parquet(jobs / "seen.parquet")["job_id"])
    assert india_id not in ledger_ids, "dropped row was stamped first_seen"

    # Widen the allowlist and re-scrape past RETENTION_LOW_DAYS. The row is new
    # to the ledger, so it gets today's first_seen and survives apply_expiry.
    plus_india = DiscoveryConfig(
        location_allowlist=LocationAllowlist(countries=["United States", "India"])
    )
    monkeypatch.setattr(cleaning, "load_config", lambda *a, **k: plus_india)
    later = pd.Timestamp("2026-06-22")  # 16d > RETENTION_LOW_DAYS
    _make_raw_parquet(
        tmp_path,
        [{**r, "date_posted": later, "scraped_date": later} for r in rows],
        run_id="2026-06-22_1000",
    )
    df = cleaning.run(
        run_id="2026-06-22_1000",
        raw_dir=raw_dir,
        clean_dir=jobs,
        runs_dir=jobs / "runs",
        pipeline_dir=tmp_path / "pipeline",
        today=later,
    )
    assert india_id in set(df["job_id"])


def test_dedupe_survivor_chosen_among_in_allowlist_rows(tmp_path, monkeypatch):
    """Dedupe keeps the longest jd_text, blind to location. Filtering after it
    let a foreign duplicate win the group and then get dropped, losing the
    in-allowlist row with it."""
    from src.discovery.config import DiscoveryConfig, LocationAllowlist

    us_only = DiscoveryConfig(location_allowlist=LocationAllowlist(countries=["United States"]))
    monkeypatch.setattr(cleaning, "load_config", lambda *a, **k: us_only)

    raw_dir = _make_raw_parquet(tmp_path, [
        {"company": "Acme", "title": "Widget Functional Consultant",
         "location": "Bengaluru, India", "description": "x" * 900},
        {"company": "Acme", "title": "Widget Functional Consultant",
         "location": "Austin, TX", "description": "y" * 300},
    ])
    df = cleaning.run(
        run_id="2026-06-06_1000",
        raw_dir=raw_dir,
        clean_dir=tmp_path / "jobs",
        runs_dir=tmp_path / "jobs" / "runs",
        pipeline_dir=tmp_path / "pipeline",
        today=pd.Timestamp("2026-06-06"),
    )
    assert len(df) == 1
    assert df.iloc[0]["location"] == "Austin, TX"


# ---------- T15: raw retention ----------

def test_prune_raw_files(tmp_path):
    from src.discovery.config import DiscoveryConfig
    from src.discovery.cleaning import prune_raw_files
    
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    
    # recent kept
    recent_path = raw_dir / "2026-07-10_1200.parquet"
    recent_path.write_text("dummy", encoding="utf-8")
    
    # old file deleted
    old_path = raw_dir / "2026-06-01_1200.parquet"
    old_path.write_text("dummy", encoding="utf-8")
    
    # unparseable name kept
    unparse_path = raw_dir / "not_a_date.parquet"
    unparse_path.write_text("dummy", encoding="utf-8")
    
    cfg = DiscoveryConfig(raw_retention_days=30)
    today = pd.Timestamp("2026-07-15")
    
    pruned = prune_raw_files(raw_dir, cfg, today)
    assert pruned == 1
    
    assert recent_path.exists()
    assert unparse_path.exists()
    assert not old_path.exists()

@pytest.mark.parametrize("term,title,expected", [
    # A trailing \b after "+" can never be satisfied, so these all missed.
    ("c++", "Senior C++ Engineer", True),
    ("c++", "Data Engineer -C++", True),
    ("c++", "Data Engineer - C++", True),
    ("c++", "Data Engineer (C++)", True),
    ("c++", "Sr. Data Engineer -C++/Python", True),
    ("c++", "Data EngineerC++", False),
    ("c++", "ABC++ Corp", False),
    ("c#", "C# Developer", True),
    # Leading \b before "." is the mirror case.
    (".net", ".NET Developer", True),
    (".net", "ASP.NET Developer", True),
    (".net", "Netflix Engineer", False),
    # Alnum on both ends keeps the old behavior exactly.
    ("data engineer", "Senior Data Engineer", True),
    ("data engineer", "Data Engineering Lead", False),
    ("node.js", "Node.js Engineer", True),
])
def test_term_pattern_matches_terms_ending_in_punctuation(term, title, expected):
    from src.discovery.cleaning import _term_pattern
    import re
    rx = re.compile(_term_pattern(term), re.IGNORECASE)
    assert bool(rx.search(title)) is expected


def test_title_exclusion(cfg):
    from src.discovery.cleaning import apply_title_exclusion
    import pandas as pd
    
    df = pd.DataFrame([
        {"title": "Senior Software Engineer (Cog Agents)", "vertical": "example_tertiary", "source": "linkedin"},
        {"title": "Sr. Cog Engineering Lead", "vertical": "example_tertiary", "source": "linkedin"},
        {"title": "Software Engineer, Applied Cog, New Grad", "vertical": "example_tertiary", "source": "linkedin"},
        {"title": "Internal Tools Cog Engineer", "vertical": "example_tertiary", "source": "linkedin"},
        {"title": "Senior Cog Engineer", "vertical": "example_tertiary", "source": "manual"},
        {"title": "Senior Widget Consultant", "vertical": "example_primary", "source": "linkedin"},
        {"title": "Senior Widget Assembly Lead", "vertical": "example_primary", "source": "manual"},
    ])

    out, drops = apply_title_exclusion(df, cfg)

    titles = out["title"].tolist()
    assert "Senior Software Engineer (Cog Agents)" not in titles
    assert "Sr. Cog Engineering Lead" not in titles
    assert "Software Engineer, Applied Cog, New Grad" in titles
    assert "Internal Tools Cog Engineer" in titles
    assert "Senior Cog Engineer" in titles
    # example_primary excludes seniority title families
    assert "Senior Widget Consultant" not in titles
    # ...but manual/URL-ingested rows stay exempt, even with senior+lead
    assert "Senior Widget Assembly Lead" in titles

    assert drops["example_tertiary"] == 2
    assert drops["example_primary"] == 1


def test_title_inclusion_gate(cfg):
    """All three verticals configure a title include-gate: keep iff strong_keep
    OR (include AND NOT exclude)."""
    from src.discovery.cleaning import apply_title_exclusion
    import pandas as pd

    df = pd.DataFrame([
        # include hit, no exclude -> keep
        {"title": "Sprocket Risk Analyst", "vertical": "example_secondary", "source": "linkedin"},
        {"title": "Governance Analyst", "vertical": "example_secondary", "source": "linkedin"},
        # strong_keep overrides an exclude term (machine learning)
        {"title": "Sprocket Risk Management Lead, Cog Learning", "vertical": "example_secondary", "source": "linkedin"},
        # include hit (governance) but role-type exclude (software engineer) -> drop
        {"title": "Software Engineer, Governance", "vertical": "example_secondary", "source": "linkedin"},
        # no include term at all -> drop
        {"title": "Body Worn Camera Coordinator", "vertical": "example_secondary", "source": "linkedin"},
        {"title": "Quantitative Researcher", "vertical": "example_secondary", "source": "linkedin"},
        # manual rows exempt from the gate even when off-lane
        {"title": "Totally Off-Lane Widget Maker", "vertical": "example_secondary", "source": "manual"},
        # example_primary include-gate: in-lane signal, no exclude -> keep
        {"title": "Widget Assembly Functional Analyst", "vertical": "example_primary", "source": "linkedin"},
        # example_primary: no in-lane signal at all -> drop
        {"title": "Business Analyst", "vertical": "example_primary", "source": "linkedin"},
        # example_primary: include hit (doohickey) but competing-vendor exclude -> drop
        {"title": "Rival Doohickey Functional Consultant", "vertical": "example_primary", "source": "linkedin"},
        # example_primary: manual row exempt even with no include term
        {"title": "Warehouse Associate", "vertical": "example_primary", "source": "manual"},
    ])

    out, drops = apply_title_exclusion(df, cfg)
    titles = out["title"].tolist()

    assert "Sprocket Risk Analyst" in titles
    assert "Governance Analyst" in titles
    assert "Sprocket Risk Management Lead, Cog Learning" in titles  # strong-keep override
    assert "Software Engineer, Governance" not in titles                   # include + exclude
    assert "Body Worn Camera Coordinator" not in titles             # no include
    assert "Quantitative Researcher" not in titles                  # no include
    assert "Totally Off-Lane Widget Maker" in titles                # manual exempt
    assert "Widget Assembly Functional Analyst" in titles                   # example_primary include hit
    assert "Business Analyst" not in titles                         # example_primary: no include
    assert "Rival Doohickey Functional Consultant" not in titles         # example_primary: include + exclude
    assert "Warehouse Associate" in titles                          # example_primary manual exempt

    assert drops["example_secondary"] == 3
    assert drops["example_primary"] == 2


def test_title_inclusion_gate_tertiary(cfg):
    """example_tertiary's include-gate does the most work of the three lanes."""
    from src.discovery.cleaning import apply_title_exclusion
    import pandas as pd

    keep = ["Cog Engineer", "Applied Cog Engineer", "Agentic Cog Engineer"]
    drop = [
        "Backend Engineer, Payments",       # no include term
        "Data Engineer",
        "Full Stack Engineer",
        "Platform Engineer",
        "Solutions Engineer",
        "Distinguished Engineer, Cog",       # include hit, seniority exclude
        "Research Scientist, Cog",          # include hit, role-type exclude
        "Cog Learning Engineer, Cog Platform",
    ]
    df = pd.DataFrame(
        [{"title": t, "vertical": "example_tertiary", "source": "linkedin"} for t in keep + drop]
        # manual rows stay exempt from the gate
        + [{"title": "Totally Off-Lane Widget Maker", "vertical": "example_tertiary",
            "source": "manual"}]
    )

    out, drops = apply_title_exclusion(df, cfg)
    titles = out["title"].tolist()

    for t in keep:
        assert t in titles, f"{t} should survive the example_tertiary gate"
    for t in drop:
        assert t not in titles, f"{t} should be dropped by the example_tertiary gate"
    assert "Totally Off-Lane Widget Maker" in titles
    assert drops["example_tertiary"] == len(drop)


def test_clean_preview_jsonl_is_written_and_json_safe(tmp_path):
    """The second documented cleaning output, and nothing asserted on it. It is
    the file a human greps when clean.parquet looks wrong, so a NaN or a
    Timestamp leaking through as a non-JSON value makes it unreadable."""
    raw_dir = _make_raw_parquet(tmp_path, [
        {"company": "Acme Inc", "title": "Widget Functional Consultant",
         "description": "x" * 300, "date_posted": pd.Timestamp("2026-06-01")},
        # No date at all: posted_date is NaT and salary is NaN downstream.
        {"company": "Beta LLC", "title": "Gizmo Business Analyst",
         "description": "y" * 300, "date_posted": None},
    ])
    out = cleaning.run(
        run_id="2026-06-06_1000",
        raw_dir=raw_dir,
        clean_dir=tmp_path / "jobs",
        runs_dir=tmp_path / "jobs" / "runs",
        pipeline_dir=tmp_path / "pipeline",
        today=pd.Timestamp("2026-06-06"),
    )
    preview = tmp_path / "jobs" / "clean.preview.jsonl"
    lines = [l for l in preview.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == len(out)

    rows = [json.loads(l) for l in lines]
    # Exactly the preview columns, in order — not the whole 28-column schema.
    for row in rows:
        assert list(row) == cleaning.PREVIEW_COLUMNS
    # job_id joins the preview back to clean.parquet.
    assert {r["job_id"] for r in rows} == set(out["job_id"])
    # NaT/NaN become null, and timestamps become ISO strings, not repr junk.
    missing = next(r for r in rows if r["posted_date"] is None)
    assert missing["fit_score"] is None
    dated = next(r for r in rows if r["posted_date"] is not None)
    assert dated["posted_date"].startswith("2026-06-01")
