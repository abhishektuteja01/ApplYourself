"""Tests for the /score plumbing: src/prescreen.py, src/scoring_io.py and
src/shortlist.py (one file, since they are one pipeline and share fixtures).

A bug in any of them silently drops or duplicates scored rows, so they carry
coverage even though the scoring prompt itself does not."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
import yaml

from src import verticals
from src.discovery.cleaning import CLEAN_COLUMNS
from src.prescreen import (
    AUTO_SKIP_SCORED_BY,
    disqualify_reason,
    hard_ineligible_phrase,
    load_hard_ineligible,
    max_years_required,
)
from src.scoring_io import (
    AXIS_MAXIMA,
    SCORED_COLUMNS,
    auto_score_disqualified,
    auto_score_ineligible,
    auto_score_out_of_lane,
    dump_unscored,
    fit_score_from_subscores,
    merge_scores,
    merge_scores_from_dir,
    prune_scored,
    select_unscored,
    validate_scores,
)
from src.shortlist import compute_shortlist, render_shortlist_markdown


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
        "vertical": "example_primary",
        "sponsorship_label": "opt_ok",
        "sponsorship_evidence": "no visa sponsorship",
        "reasoning": "widget assembly lifecycle fit; senior stretch flagged.",
        "keywords_to_mirror": ["widget assembly", "gizmo contract"],
        "suggested_action": "tailor",
    }
    base.update(overrides)
    if "fit_score" in overrides and "fit_subscores" not in overrides:
        base["fit_subscores"] = _split_score(overrides["fit_score"])
    return base


def _make_clean(tmp_path: Path, rows: list[dict]) -> Path:
    """Write a clean.parquet with the CLEAN_COLUMNS schema. Defaults are sensible;
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
        {"job_id": "aaaaaaaa", "company": "Acme", "title": "Widget Assembly",
         "jd_text": "hi" * 200, "url": "https://x", "vertical": "example_primary"},
    ])
    scored_p = tmp_path / "scored.parquet"
    out_path = tmp_path / "unscored.jsonl"
    n = dump_unscored(clean_p, scored_p, out_path)
    assert n == 1
    line = out_path.read_text(encoding="utf-8").strip()
    obj = json.loads(line)
    assert obj["job_id"] == "aaaaaaaa"
    assert obj["company"] == "Acme"
    assert obj["title"] == "Widget Assembly"
    assert "jd_text" in obj
    assert "url" in obj


def test_dump_unscored_force_all_ignores_scored(tmp_path):
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa", "title": "Widget Functional Analyst", "vertical": "example_primary"},
        {"job_id": "bbbbbbbb", "title": "Widget Functional Consultant", "vertical": "example_primary"},
    ])
    scored_p = tmp_path / "scored.parquet"
    merge_scores(scored_p, [_valid_score(job_id="aaaaaaaa")], scored_by_model="t")
    out_path = tmp_path / "unscored.jsonl"
    n = dump_unscored(clean_p, scored_p, out_path, force_all=True)
    assert n == 2  # both, despite aaaaaaaa already scored


# ---------- dump_unscored: only_job_id / skip_prescreens (/ingest) ----------

def test_dump_unscored_only_job_id_selects_one_row(tmp_path):
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa", "title": "Widget Functional Consultant",
         "vertical": "example_primary"},
        {"job_id": "bbbbbbbb", "title": "Widget Platform Analyst",
         "vertical": "example_primary"},
    ])
    out_path = tmp_path / "unscored.jsonl"
    n = dump_unscored(clean_p, tmp_path / "scored.parquet", out_path,
                      only_job_id="bbbbbbbb")
    assert n == 1
    assert json.loads(out_path.read_text(encoding="utf-8").strip())["job_id"] == "bbbbbbbb"


def test_dump_unscored_only_job_id_already_scored_yields_zero(tmp_path):
    """The /ingest re-run path: an already-scored id dumps nothing, so the
    command skips the judge instead of spawning one on an empty range."""
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa", "title": "Widget Functional Consultant",
         "vertical": "example_primary"},
    ])
    scored_p = tmp_path / "scored.parquet"
    merge_scores(scored_p, [_valid_score(job_id="aaaaaaaa")], scored_by_model="t")
    out_path = tmp_path / "unscored.jsonl"
    assert dump_unscored(clean_p, scored_p, out_path, only_job_id="aaaaaaaa") == 0
    assert out_path.read_text(encoding="utf-8").strip() == ""


def test_dump_unscored_skip_prescreens_sends_ineligible_row_to_judge(tmp_path):
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa", "title": "Widget Functional Consultant",
         "jd_text": "US citizenship required for this role.",
         "vertical": "example_primary"},
    ])
    out_path = tmp_path / "unscored.jsonl"
    phrases = ("us citizenship required",)
    assert dump_unscored(clean_p, tmp_path / "s.parquet", out_path,
                         hard_ineligible=phrases) == 0
    assert (out_path.parent / "auto_skip_ineligible.jsonl").read_text(encoding="utf-8").strip()

    n = dump_unscored(clean_p, tmp_path / "s.parquet", out_path,
                      hard_ineligible=phrases, skip_prescreens=True)
    assert n == 1
    assert (out_path.parent / "auto_skip_ineligible.jsonl").read_text(encoding="utf-8").strip() == ""


def test_dump_unscored_skip_prescreens_sends_disqualified_row_to_judge(tmp_path):
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa", "title": "Widget Functional Consultant",
         "jd_text": "You will act as a solution architect for the team.",
         "vertical": "example_primary"},
    ])
    out_path = tmp_path / "unscored.jsonl"
    assert dump_unscored(clean_p, tmp_path / "s.parquet", out_path,
                         hard_ineligible=()) == 0
    assert (out_path.parent / "auto_skip_example_primary.jsonl").read_text(encoding="utf-8").strip()

    n = dump_unscored(clean_p, tmp_path / "s.parquet", out_path,
                      hard_ineligible=(), skip_prescreens=True)
    assert n == 1
    assert (out_path.parent / "auto_skip_example_primary.jsonl").read_text(encoding="utf-8").strip() == ""


def test_dump_unscored_skip_prescreens_still_routes_out_of_lane(tmp_path):
    """split_by_vertical raises on a vertical it does not know, so the
    out-of-lane branch must survive skip_prescreens."""
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa", "title": "Sous Chef", "vertical": ""},
    ])
    out_path = tmp_path / "unscored.jsonl"
    n = dump_unscored(clean_p, tmp_path / "s.parquet", out_path,
                      hard_ineligible=(), skip_prescreens=True)
    assert n == 0
    assert out_path.read_text(encoding="utf-8").strip() == ""
    assert (out_path.parent / "auto_skip.jsonl").read_text(encoding="utf-8").strip()


# ---------- dump_unscored file-handle safety ----------

def _patch_failing_open(monkeypatch, fail_on: int):
    """Make the fail_on'th write-mode Path.open raise OSError. Returns a dict
    with the call count and every handle that was successfully opened, so the
    caller can assert they all got closed."""
    real_open = Path.open
    seen = {"calls": 0, "opened": []}

    def fake_open(self, mode="r", *args, **kwargs):
        if "w" not in mode:
            return real_open(self, mode, *args, **kwargs)
        seen["calls"] += 1
        if seen["calls"] == fail_on:
            raise OSError(28, "No space left on device")
        f = real_open(self, mode, *args, **kwargs)
        seen["opened"].append(f)
        return f

    monkeypatch.setattr(Path, "open", fake_open)
    return seen


# 1-3 are unscored/auto_skip/auto_skip_ineligible; 4 is the first per-vertical
# skip file, opened inside a dict comprehension that binds no name per handle.
@pytest.mark.parametrize("fail_on", [1, 2, 3, 4])
def test_dump_unscored_open_failure_surfaces_oserror(tmp_path, monkeypatch, fail_on):
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa", "title": "Widget Functional Analyst", "vertical": "example_primary"},
    ])
    scored_p = tmp_path / "scored.parquet"
    out_path = tmp_path / "unscored.jsonl"
    seen = _patch_failing_open(monkeypatch, fail_on)
    with pytest.raises(OSError) as exc:
        dump_unscored(clean_p, scored_p, out_path)
    monkeypatch.undo()
    assert not isinstance(exc.value, NameError)
    assert seen["calls"] == fail_on  # aborted at the failing open, no others tried
    assert len(seen["opened"]) == fail_on - 1
    assert all(f.closed for f in seen["opened"])


def test_dump_unscored_open_failure_in_last_skip_file_leaks_nothing(tmp_path, monkeypatch):
    """The per-vertical handles are opened inside one comprehension, so a
    failure on the last one must still close the earlier ones."""
    names = verticals.get_config().names
    if len(names) < 2:
        pytest.skip("needs >=2 configured verticals to have an earlier handle to leak")
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa", "title": "Widget Functional Analyst", "vertical": "example_primary"},
    ])
    scored_p = tmp_path / "scored.parquet"
    out_path = tmp_path / "unscored.jsonl"
    seen = _patch_failing_open(monkeypatch, 3 + len(names))
    with pytest.raises(OSError):
        dump_unscored(clean_p, scored_p, out_path)
    monkeypatch.undo()
    assert len(seen["opened"]) == 2 + len(names)
    assert all(f.closed for f in seen["opened"])


def test_dump_unscored_closes_every_handle_on_success(tmp_path, monkeypatch):
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa", "title": "Widget Functional Analyst", "vertical": "example_primary"},
        {"job_id": "bbbbbbbb", "title": "Public Health Data Analyst", "vertical": ""},
    ])
    scored_p = tmp_path / "scored.parquet"
    out_path = tmp_path / "unscored.jsonl"
    seen = _patch_failing_open(monkeypatch, 0)  # never fails
    dump_unscored(clean_p, scored_p, out_path)
    monkeypatch.undo()
    assert len(seen["opened"]) == 3 + len(verticals.get_config().names)
    assert all(f.closed for f in seen["opened"])


def test_dump_unscored_row_error_closes_handles(tmp_path, monkeypatch):
    """A failure mid-loop must still close every handle (the old try/finally's
    one working case) and must not be masked."""
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa", "title": "Widget Functional Analyst", "vertical": "example_primary"},
    ])
    scored_p = tmp_path / "scored.parquet"
    out_path = tmp_path / "unscored.jsonl"
    seen = _patch_failing_open(monkeypatch, 0)
    monkeypatch.setattr(
        "src.scoring_io.disqualify_reason",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError, match="boom"):
        dump_unscored(clean_p, scored_p, out_path)
    monkeypatch.undo()
    assert all(f.closed for f in seen["opened"])


# ---------- out-of-lane pre-screen (vertical="" auto-skip) ----------

def test_dump_unscored_splits_in_lane_vs_out_of_lane(tmp_path):
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa", "title": "Widget Functional Analyst", "vertical": "example_primary"},
        {"job_id": "bbbbbbbb", "title": "Public Health Data Analyst", "vertical": ""},
        {"job_id": "cccccccc", "title": "Gizmo Trading Analyst", "vertical": "example_primary"},
        {"job_id": "dddddddd", "title": "Senior Platform Engineer", "vertical": ""},
    ])
    scored_p = tmp_path / "scored.parquet"
    out_path = tmp_path / "unscored.jsonl"
    n_judge = dump_unscored(clean_p, scored_p, out_path)
    assert n_judge == 2
    judge_ids = {json.loads(l)["job_id"] for l in out_path.read_text(encoding="utf-8").splitlines() if l.strip()}
    assert judge_ids == {"aaaaaaaa", "cccccccc"}
    skip_path = tmp_path / "auto_skip.jsonl"
    assert skip_path.exists()
    skip_ids = {json.loads(l)["job_id"] for l in skip_path.read_text(encoding="utf-8").splitlines() if l.strip()}
    assert skip_ids == {"bbbbbbbb", "dddddddd"}


def test_dump_unscored_only_vertical_filters_to_one_vertical(tmp_path):
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa", "title": "Widget Functional Analyst", "vertical": "example_primary"},
        {"job_id": "bbbbbbbb", "title": "Sprocket Risk Analyst", "vertical": "example_secondary"},
        {"job_id": "cccccccc", "title": "Public Health Data Analyst", "vertical": ""},
    ])
    scored_p = tmp_path / "scored.parquet"
    out_path = tmp_path / "unscored.jsonl"
    n_judge = dump_unscored(clean_p, scored_p, out_path, only_vertical="example_secondary")
    assert n_judge == 1
    judge_ids = {json.loads(l)["job_id"] for l in out_path.read_text(encoding="utf-8").splitlines() if l.strip()}
    assert judge_ids == {"bbbbbbbb"}
    # example_primary and out-of-lane rows are left entirely untouched (not even auto-skipped)
    skip_path = tmp_path / "auto_skip.jsonl"
    skip_ids = {json.loads(l)["job_id"] for l in skip_path.read_text(encoding="utf-8").splitlines() if l.strip()} if skip_path.exists() else set()
    assert skip_ids == set()


# ---------- example_secondary vertical: JD disqualifier pre-screen ----------

def test_secondary_disqualify_reason_matches_phrases(cfg):
    example_secondary = cfg.verticals["example_secondary"]
    assert disqualify_reason(example_secondary, "Candidates must have a PhD required for this role.") == "phrase"
    assert disqualify_reason(example_secondary, "Sprocket charter required; 5+ years quantitative finance.") == "phrase"
    assert disqualify_reason(example_secondary, "Legacy sprocket ledger coverage for the compliance desk.") == "phrase"
    assert disqualify_reason(example_secondary, "Sprocket certification required.") == "phrase"


def test_secondary_disqualify_reason_case_insensitive(cfg):
    assert disqualify_reason(cfg.verticals["example_secondary"], "Ph.D. Required in Mathematics or Statistics") == "phrase"


def test_secondary_disqualify_reason_none_on_preferred_not_required(cfg):
    # "preferred" must NOT trip the disqualifier — only hard "required" wording does.
    example_secondary = cfg.verticals["example_secondary"]
    assert disqualify_reason(example_secondary, "PhD preferred but not required. CS background welcome.") is None
    assert disqualify_reason(example_secondary, "") is None
    assert disqualify_reason(example_secondary, None) is None


# ---------- example_secondary vertical: explicit 5+ years experience disqualifier ----------

def test_max_years_required_simple_and_plus_forms():
    assert max_years_required("5+ years of experience in sprocket governance, risk, compliance") == 5
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
        "Acme has dedicated their expertise... over 150-year legacy of providing service."
    ) == 0


def test_max_years_required_handles_markdown_escaped_text():
    # Real scraped JDs carry markdown-escaped punctuation: "5\+ years", "**5\+ years**".
    assert max_years_required("* 5\\+ years of experience in governance and risk") == 5
    assert max_years_required("* **5\\+ years** \n of experience in one or more of the following") == 5


def test_max_years_required_takes_max_across_multiple_clauses():
    text = (
        "5+ years of experience in sprocket governance, risk, compliance, privacy, "
        "information security, technology risk, third-party risk, model risk, "
        "audit, or a related field. 2+ years of direct experience in AI governance."
    )
    assert max_years_required(text) == 5


def test_secondary_disqualify_reason_years_over_threshold(cfg):
    example_secondary = cfg.verticals["example_secondary"]
    assert disqualify_reason(example_secondary, "5+ years of experience in sprocket governance and risk") == "years"
    assert disqualify_reason(example_secondary, "6+ years of experience in sprocket risk") == "years"
    assert disqualify_reason(example_secondary, "4+ years of experience in sprocket risk") is None
    assert disqualify_reason(example_secondary, "2+ years of experience, 3-4 years preferred") is None


def test_secondary_disqualify_reason_markdown_escaped_5plus_year_requirement(cfg):
    # Scraped JDs arrive markdown-escaped. These two shapes originally
    # over-scored seniority=18 despite an explicit 5+ year requirement.
    bulleted_jd = (
        "**Skills And Qualifications**\n"
        "* 5\\+ years of experience in sprocket governance, risk, compliance, "
        "privacy, third\\-party risk, sprocket validation, audit, or a related "
        "field.\n"
        "* 2\\+ years of direct experience in sprocket governance."
    )
    assert disqualify_reason(cfg.verticals["example_secondary"], bulleted_jd) == "years"

    bold_wrapped_jd = (
        "**Required Qualifications** \n\n\n\n* **5\\+ years** \n of experience "
        "in one or more of the following:\n"
        "* Governance, Risk \\& Compliance, Privacy, Third\\-Party Risk, "
        "Sprocket Risk, or Audit\n"
        "* **2\\+ years** of hands\\-on experience in:\n"
        "* sprocket governance or sprocket risk management"
    )
    assert disqualify_reason(cfg.verticals["example_secondary"], bold_wrapped_jd) == "years"


# One test for both lanes: the routing logic is per-vertical by construction, so
# a copy per vertical only proves the loop runs twice.
@pytest.mark.parametrize("vertical,other,dq_title,dq_jd,ok_title,ok_jd", [
    ("example_secondary", "example_primary",
     "Sprocket Risk Analyst", "PhD required. ",
     "Sprocket Validation Analyst", "CS or Engineering background welcome. "),
    ("example_primary", "example_secondary",
     "Widget Functional Analyst", "5+ years of experience required. ",
     "Widget Assembly Analyst", "2+ years of experience preferred. "),
], ids=["secondary_phrase", "primary_years"])
def test_dump_unscored_routes_disqualified_to_a_per_vertical_skip_file(
        tmp_path, vertical, other, dq_title, dq_jd, ok_title, ok_jd):
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa", "title": dq_title, "vertical": vertical,
         "jd_text": dq_jd + "x" * 200},
        {"job_id": "bbbbbbbb", "title": ok_title, "vertical": vertical,
         "jd_text": ok_jd + "x" * 200},
        {"job_id": "cccccccc", "title": "Some Other Role", "vertical": other,
         "jd_text": "x" * 200},
    ])
    out_path = tmp_path / "unscored.jsonl"
    n_judge = dump_unscored(clean_p, tmp_path / "scored.parquet", out_path)

    judged = {json.loads(l)["job_id"]: json.loads(l)["vertical"]
              for l in out_path.read_text(encoding="utf-8").splitlines() if l.strip()}
    assert n_judge == 2
    assert set(judged) == {"bbbbbbbb", "cccccccc"}
    # Each judged row carries its own vertical through, for the rubric choice.
    assert judged["bbbbbbbb"] == vertical
    assert judged["cccccccc"] == other

    skip_path = tmp_path / f"auto_skip_{vertical}.jsonl"
    skip_ids = {json.loads(l)["job_id"]
                for l in skip_path.read_text(encoding="utf-8").splitlines() if l.strip()}
    assert skip_ids == {"aaaaaaaa"}
    # The other lane's file must not have absorbed it.
    other_path = tmp_path / f"auto_skip_{other}.jsonl"
    assert not other_path.exists() or not other_path.read_text(encoding="utf-8").strip()
    # The title-out-of-lane file is a different gate and stays empty here.
    assert (tmp_path / "auto_skip.jsonl").exists()


@pytest.mark.parametrize("vertical,title,extra,stamp_attr,reason_attr", [
    ("example_secondary", "Sprocket Risk Analyst", {},
     "disqualifier_scored_by", None),
    ("example_primary", "Widget Functional Analyst", {"_disqualify_reason": "years"},
     "disqualifier_scored_by", "reasoning_years"),
], ids=["secondary_phrase", "primary_years"])
def test_auto_score_disqualified_materializes_skip_rows(
        tmp_path, cfg, vertical, title, extra, stamp_attr, reason_attr):
    v = cfg.verticals[vertical]
    skip_path = tmp_path / f"auto_skip_{vertical}.jsonl"
    skip_path.write_text(
        json.dumps({"job_id": "aaaaaaaa", "title": title, **extra}) + "\n",
        encoding="utf-8",
    )
    scored_p = tmp_path / "scored.parquet"
    assert auto_score_disqualified(v, skip_path, scored_p) == 1

    row = pd.read_parquet(scored_p).iloc[0]
    assert row["job_id"] == "aaaaaaaa"
    assert row["fit_score"] == 0
    assert row["vertical"] == vertical
    assert row["suggested_action"] == "skip"
    # The stamp is a literal config field, never another vertical's.
    assert row["scored_by_model"] == getattr(v, stamp_attr)
    if reason_attr:
        assert row["reasoning"] == getattr(v, reason_attr)


def test_auto_score_disqualified_noop_on_missing_file(tmp_path, cfg):
    scored_p = tmp_path / "scored.parquet"
    assert auto_score_disqualified(cfg.verticals["example_secondary"], tmp_path / "nope.jsonl", scored_p) == 0
    assert not scored_p.exists()


# ---------- example_primary vertical: explicit 5+ years experience disqualifier ----------

def test_primary_disqualify_reason_years_over_threshold(cfg):
    example_primary = cfg.verticals["example_primary"]
    assert disqualify_reason(example_primary, "5+ years of experience in Widget Assembly commodity management") == "years"
    assert disqualify_reason(example_primary, "6+ years of experience as a widget functional analyst") == "years"
    assert disqualify_reason(example_primary, "4+ years of experience as a widget functional analyst") is None
    assert disqualify_reason(example_primary, "2+ years of experience, 3-4 years preferred") is None
    assert disqualify_reason(example_primary, "") is None
    assert disqualify_reason(example_primary, None) is None
    # example_primary's phrase list carries no credential wording, so a
    # doctorate requirement alone never trips it
    assert disqualify_reason(example_primary, "PhD required for this role.") is None


def test_dump_unscored_routes_primary_disqualified_to_separate_skip_file(tmp_path):
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa", "title": "Widget Functional Analyst", "vertical": "example_primary",
         "jd_text": "5+ years of experience required. " + "x" * 200},
        {"job_id": "bbbbbbbb", "title": "Widget Assembly Analyst", "vertical": "example_primary",
         "jd_text": "2+ years of experience preferred. " + "x" * 200},
        {"job_id": "cccccccc", "title": "Sprocket Risk Analyst", "vertical": "example_secondary",
         "jd_text": "x" * 200},
    ])
    scored_p = tmp_path / "scored.parquet"
    out_path = tmp_path / "unscored.jsonl"
    n_judge = dump_unscored(clean_p, scored_p, out_path)
    assert n_judge == 2  # bbbbbbbb (example_primary, under threshold) + cccccccc (example_secondary)
    judge_ids = {json.loads(l)["job_id"] for l in out_path.read_text(encoding="utf-8").splitlines() if l.strip()}
    assert judge_ids == {"bbbbbbbb", "cccccccc"}
    primary_skip_path = tmp_path / "auto_skip_example_primary.jsonl"
    assert primary_skip_path.exists()
    skip_ids = {json.loads(l)["job_id"] for l in primary_skip_path.read_text(encoding="utf-8").splitlines() if l.strip()}
    assert skip_ids == {"aaaaaaaa"}


def test_auto_score_disqualified_materializes_primary_skip_rows(tmp_path, cfg):
    skip_path = tmp_path / "auto_skip_example_primary.jsonl"
    skip_path.write_text(
        json.dumps({"job_id": "aaaaaaaa", "title": "Widget Functional Analyst",
                    "_disqualify_reason": "years"}) + "\n",
        encoding="utf-8",
    )
    scored_p = tmp_path / "scored.parquet"
    n = auto_score_disqualified(cfg.verticals["example_primary"], skip_path, scored_p)
    assert n == 1
    df = pd.read_parquet(scored_p)
    assert df.iloc[0]["job_id"] == "aaaaaaaa"
    assert df.iloc[0]["fit_score"] == 0
    assert df.iloc[0]["vertical"] == "example_primary"
    assert df.iloc[0]["suggested_action"] == "skip"
    assert df.iloc[0]["scored_by_model"] == "rubric:example-primary-jd-years-disqualifier"
    # a "years" skip carries the vertical's reasoning_years text
    assert df.iloc[0]["reasoning"] == cfg.verticals["example_primary"].reasoning_years


# ---------- hard-ineligible pre-label (carve-out) ----------

def test_load_hard_ineligible_reads_and_lowercases(tmp_path):
    p = tmp_path / "rules.yaml"
    p.write_text("hard_ineligible:\n  - 'Active Security Clearance'\n  - 'green card required'\n", encoding="utf-8")
    assert load_hard_ineligible(p) == ("active security clearance", "green card required")


def test_load_hard_ineligible_missing_key_is_empty(tmp_path):
    p = tmp_path / "rules.yaml"
    p.write_text("ineligible:\n  - 'US citizen'\n", encoding="utf-8")
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
        {"job_id": "aaaaaaaa", "title": "Widget Functional Analyst", "vertical": "example_primary",
         "jd_text": "Active security clearance. 8+ years of experience. " + "x" * 200},
        {"job_id": "bbbbbbbb", "title": "Widget Assembly Analyst", "vertical": "example_primary",
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
                 (tmp_path / "auto_skip_ineligible.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert [r["job_id"] for r in inel_rows] == ["aaaaaaaa"]
    assert inel_rows[0]["_ineligible_phrase"] == "active security clearance"
    # the years disqualifier never saw the row
    assert (tmp_path / "auto_skip_example_primary.jsonl").read_text(encoding="utf-8").strip() == ""
    # out-of-lane row stayed in auto_skip.jsonl
    ool = [json.loads(l)["job_id"] for l in
           (tmp_path / "auto_skip.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert ool == ["cccccccc"]


def test_auto_score_ineligible_materializes_labeled_rows(tmp_path):
    skip_path = tmp_path / "auto_skip_ineligible.jsonl"
    skip_path.write_text(json.dumps({
        "job_id": "aaaaaaaa", "title": "Widget Functional Analyst",
        "vertical": "example_primary", "_ineligible_phrase": "active security clearance",
    }) + "\n", encoding="utf-8")
    scored_p = tmp_path / "scored.parquet"
    n = auto_score_ineligible(skip_path, scored_p)
    assert n == 1
    df = pd.read_parquet(scored_p)
    r = df.iloc[0]
    assert r["sponsorship_label"] == "ineligible"
    assert r["sponsorship_evidence"] == "active security clearance"
    assert r["fit_score"] == 0
    assert r["vertical"] == "example_primary"
    assert r["scored_by_model"] == "rubric:hard-ineligible-pre-screen"
    assert r["reasoning"].startswith("Auto-labeled ineligible by deterministic pre-screen")


def test_auto_score_ineligible_noop_on_missing_file(tmp_path):
    scored_p = tmp_path / "scored.parquet"
    assert auto_score_ineligible(tmp_path / "nope.jsonl", scored_p) == 0
    assert not scored_p.exists()


# ---------- title-level disqualifier ----------

def test_disqualify_reason_title_phrase_trips(cfg):
    example_primary = cfg.verticals["example_primary"]
    clean_jd = "2+ years of experience preferred. " + "x" * 200
    assert disqualify_reason(example_primary, clean_jd, "Widget Solution Architect") == "title"
    assert disqualify_reason(example_primary, clean_jd, "Director, Widget Programs") == "title"
    assert disqualify_reason(example_primary, clean_jd, "Widget Functional Analyst") is None


def test_disqualify_reason_title_case_insensitive_and_optional(cfg):
    example_primary = cfg.verticals["example_primary"]
    assert disqualify_reason(example_primary, "x" * 50, "widget SOLUTION ARCHITECT") == "title"
    # title omitted / None / empty — jd-side checks still run, no crash
    assert disqualify_reason(example_primary, "x" * 50) is None
    assert disqualify_reason(example_primary, "x" * 50, None) is None
    assert disqualify_reason(example_primary, "x" * 50, "") is None


def test_disqualify_reason_title_takes_priority_over_years(cfg):
    example_primary = cfg.verticals["example_primary"]
    jd = "8+ years of experience required. " + "x" * 200
    assert disqualify_reason(example_primary, jd, "Widget Solution Architect") == "title"
    assert disqualify_reason(example_primary, jd, "Widget Functional Analyst") == "years"


def test_disqualify_reason_title_not_checked_on_vertical_without_title_phrases(cfg):
    example_secondary = cfg.verticals["example_secondary"]
    assert disqualify_reason(example_secondary, "x" * 50, "Solution Architect") is None


def test_dump_unscored_routes_title_disqualified_to_skip_file(tmp_path):
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa", "title": "Widget Solution Architect", "vertical": "example_primary",
         "jd_text": "2+ years of experience preferred. " + "x" * 200},
        {"job_id": "bbbbbbbb", "title": "Widget Assembly Analyst", "vertical": "example_primary",
         "jd_text": "2+ years of experience preferred. " + "x" * 200},
    ])
    scored_p = tmp_path / "scored.parquet"
    out_path = tmp_path / "unscored.jsonl"
    n_judge = dump_unscored(clean_p, scored_p, out_path)
    assert n_judge == 1
    skip_rows = [json.loads(l) for l in
                 (tmp_path / "auto_skip_example_primary.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert [r["job_id"] for r in skip_rows] == ["aaaaaaaa"]
    assert skip_rows[0]["_disqualify_reason"] == "title"


def test_auto_score_disqualified_uses_title_reasoning(tmp_path, cfg):
    skip_path = tmp_path / "auto_skip_example_primary.jsonl"
    skip_path.write_text(json.dumps({
        "job_id": "aaaaaaaa", "title": "Widget Solution Architect",
        "_disqualify_reason": "title",
    }) + "\n", encoding="utf-8")
    scored_p = tmp_path / "scored.parquet"
    n = auto_score_disqualified(cfg.verticals["example_primary"], skip_path, scored_p)
    assert n == 1
    df = pd.read_parquet(scored_p)
    assert df.iloc[0]["reasoning"] == cfg.verticals["example_primary"].reasoning_title
    assert df.iloc[0]["fit_score"] == 0
    assert df.iloc[0]["suggested_action"] == "skip"


def test_auto_score_out_of_lane_materializes_skip_rows(tmp_path):
    skip_path = tmp_path / "auto_skip.jsonl"
    skip_path.write_text(
        json.dumps({"job_id": "aaaaaaaa", "title": "Plumber"}) + "\n"
        + json.dumps({"job_id": "bbbbbbbb", "title": "Chef"}) + "\n",
        encoding="utf-8",
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
    skip_path.write_text("", encoding="utf-8")
    scored_p = tmp_path / "scored.parquet"
    assert auto_score_out_of_lane(skip_path, scored_p) == 0


def test_end_to_end_dump_then_auto_score(tmp_path):
    clean_p = _make_clean(tmp_path, [
        {"job_id": "aaaaaaaa", "title": "Widget Functional Analyst", "vertical": "example_primary"},
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
    ]), encoding="utf-8")
    (staging / "batch_002.json").write_text(json.dumps([
        _valid_score(job_id="cccccccc"),
    ]), encoding="utf-8")
    n, skipped = merge_scores_from_dir(scored_p, staging, scored_by_model="t")
    assert n == 3
    assert skipped == []
    df = pd.read_parquet(scored_p)
    assert set(df["job_id"]) == {"aaaaaaaa", "bbbbbbbb", "cccccccc"}


@pytest.mark.parametrize("bad_body", [
    '[{"job_id": "dddddddd", "fit_score": 7',   # truncated mid-write
    '{"job_id": "dddddddd"}',                   # object, not an array
])
def test_merge_scores_from_dir_reports_unreadable_batches(tmp_path, bad_body):
    """Skipping an unreadable batch is fine; failing to REPORT it is what lets
    the caller delete ~100 judged rows behind a healthy merged= count."""
    scored_p = tmp_path / "scored.parquet"
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "batch_001.json").write_text(json.dumps([
        _valid_score(job_id="aaaaaaaa"),
    ]), encoding="utf-8")
    (staging / "batch_002.json").write_text(bad_body, encoding="utf-8")
    n, skipped = merge_scores_from_dir(scored_p, staging, scored_by_model="t")
    assert n == 1  # the good batch still merges
    assert [f.name for f in skipped] == ["batch_002.json"]


def test_merge_scores_from_dir_reports_unreadable_when_nothing_merges(tmp_path):
    """The all-batches-bad case still has to report — the early `not all_scores`
    return is the path where merged=0 looks like an empty-staging no-op."""
    scored_p = tmp_path / "scored.parquet"
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "batch_001.json").write_text("[{", encoding="utf-8")
    n, skipped = merge_scores_from_dir(scored_p, staging, scored_by_model="t")
    assert n == 0
    assert [f.name for f in skipped] == ["batch_001.json"]


def test_merge_scores_from_dir_error_names_source_batch(tmp_path):
    """score.md tells the operator to fix 'the named row in the named batch
    file' — so the error has to name the file, not a concatenated row index."""
    scored_p = tmp_path / "scored.parquet"
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "batch_001.json").write_text(json.dumps([
        _valid_score(job_id="aaaaaaaa"),
    ]), encoding="utf-8")
    bad = _valid_score(job_id="bbbbbbbb", fit_score=70)
    bad["fit_subscores"] = {"title": 30, "skills": 30, "seniority": 20, "domain": 20}
    (staging / "batch_002.json").write_text(json.dumps([bad]), encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        merge_scores_from_dir(scored_p, staging, scored_by_model="t")
    assert "batch_002.json[0]" in str(exc.value)
    assert "fit_score 70 disagrees with fit_subscores sum 100" in str(exc.value)
    assert not scored_p.exists()  # raises before touching the parquet


def test_validate_scores_non_dict_row_is_an_error_not_a_crash():
    errs = validate_scores([_valid_score(), 42])
    assert any("expected an object, got int" in e for e in errs)


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


def test_validate_scores_rejects_duplicate_job_id():
    """Two records for one job_id make scored.loc[job_id] a DataFrame, which
    breaks track_cli and tailor_cli downstream."""
    a = _valid_score(job_id="aaaaaaaa")
    a["_source"] = "batch_example_primary_001.json[0]"
    b = _valid_score(job_id="aaaaaaaa")
    b["_source"] = "batch_example_primary_002.json[3]"
    errors = validate_scores([a, b])
    assert len(errors) == 1
    assert "duplicate job_id" in errors[0]
    # Both batches must be named, or the operator cannot find the overlap.
    assert "batch_example_primary_001.json[0]" in errors[0]
    assert "batch_example_primary_002.json[3]" in errors[0]


def test_merge_scores_never_writes_two_rows_for_one_job_id(tmp_path):
    scored_p = tmp_path / "scored.parquet"
    merge_scores(scored_p, [_valid_score(job_id="aaaaaaaa")], scored_by_model="m")
    merge_scores(scored_p, [_valid_score(job_id="aaaaaaaa")], scored_by_model="m")
    out = pd.read_parquet(scored_p)
    assert list(out["job_id"]) == ["aaaaaaaa"]


# ---------- fit_score is derived, never authored ----------

def test_fit_score_from_subscores_sums_the_four_axes():
    assert fit_score_from_subscores(
        {"title": 27, "skills": 22, "seniority": 18, "domain": 11}) == 78


def test_fit_score_from_subscores_maxima_sum_to_100():
    assert fit_score_from_subscores(AXIS_MAXIMA) == 100


def test_merge_derives_fit_score_when_judge_omits_it(tmp_path):
    scored_p = tmp_path / "scored.parquet"
    s = _valid_score(job_id="aaaaaaaa")
    s["fit_subscores"] = {"title": 27, "skills": 22, "seniority": 18, "domain": 11}
    del s["fit_score"]
    merge_scores(scored_p, [s], scored_by_model="t")
    df = pd.read_parquet(scored_p)
    assert df.loc[0, "fit_score"] == 78.0


def test_merge_ignores_a_stray_agreeing_fit_score(tmp_path):
    """Subscores are authoritative: an agreeing fit_score is accepted but the
    stored total still comes from the sum, not from the supplied key."""
    scored_p = tmp_path / "scored.parquet"
    s = _valid_score(job_id="aaaaaaaa", fit_score=78)
    s["fit_subscores"] = {"title": 27, "skills": 22, "seniority": 18, "domain": 11}
    merge_scores(scored_p, [s], scored_by_model="t")
    df = pd.read_parquet(scored_p)
    assert df.loc[0, "fit_score"] == 78.0


def test_auto_skipped_rows_still_land_fit_score_zero(tmp_path):
    """The three auto-skip paths no longer hand-write fit_score: 0 — it has to
    fall out of their all-zero subscores."""
    skip_p = tmp_path / "auto_skip.jsonl"
    skip_p.write_text(json.dumps({"job_id": "aaaaaaaa", "vertical": ""}) + "\n", encoding="utf-8")
    scored_p = tmp_path / "scored.parquet"
    n = auto_score_out_of_lane(skip_p, scored_p)
    assert n == 1
    df = pd.read_parquet(scored_p)
    assert df.loc[0, "fit_score"] == 0.0


def test_validate_scores_stray_fit_score_must_agree():
    """A judge should omit fit_score. Emitting a disagreeing one is prompt
    drift, so it fails loud instead of being silently overridden."""
    bad = _valid_score(fit_score=99,
                       fit_subscores={"title": 25, "skills": 25, "seniority": 15, "domain": 10})
    errs = validate_scores([bad])
    assert any("fit_score 99 disagrees with fit_subscores sum 75" in e for e in errs)


def test_validate_scores_fit_score_omitted_is_valid():
    s = _valid_score()
    del s["fit_score"]
    assert validate_scores([s]) == []


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
            }), encoding="utf-8")
    return clean_p, scored_p, pipeline_dir


def test_compute_shortlist_top_n_cap_and_min_fit(tmp_path):
    # 12 rows with descending fit_scores from 95 down to 40 by 5s
    rows = [{"job_id": f"{i:08x}"} for i in range(12)]
    scores = [_valid_score(job_id=f"{i:08x}", fit_score=95 - i * 5) for i in range(12)]
    clean_p, scored_p, pipe_d = _setup_shortlist(tmp_path, rows, scores)
    out = compute_shortlist(scored_p, clean_p, pipe_d, top_n=10, min_fit=50)
    assert len(out["main"]["example_primary"]) == 10  # cap at top_n
    assert all(r["fit_score"] >= 50 for r in out["main"]["example_primary"])  # min_fit floor
    # Rows with fit_score 45 and 40 (i=10, 11) excluded by min_fit
    main_ids = {r["job_id"] for r in out["main"]["example_primary"]}
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
    main_ids = {r["job_id"] for r in out["main"]["example_primary"]}
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
    assert {r["job_id"] for r in out["main"]["example_primary"]} == {"aaaaaaaa"}
    assert {r["job_id"] for r in out["suppressed"]} == {"bbbbbbbb"}
    assert {r["job_id"] for r in out["excluded"]}   == set()  # a skip is not an ineligible


def test_compute_shortlist_sort_fit_score_desc(tmp_path):
    rows = [{"job_id": f"{i:08x}"} for i in range(3)]
    scores = [_valid_score(job_id=f"{i:08x}", fit_score=60 + i * 10) for i in range(3)]
    clean_p, scored_p, pipe_d = _setup_shortlist(tmp_path, rows, scores)
    out = compute_shortlist(scored_p, clean_p, pipe_d)
    # 80 > 70 > 60 — main should be in descending fit_score order
    assert [r["fit_score"] for r in out["main"]["example_primary"]] == [80, 70, 60]


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
    assert out == {"main": {"example_tertiary": [], "example_secondary": [], "example_primary": []}, "excluded": [], "suppressed": []}


def test_compute_shortlist_output_is_json_serialisable(tmp_path):
    rows = [{"job_id": "aaaaaaaa"}]
    scores = [_valid_score(job_id="aaaaaaaa", fit_score=80)]
    clean_p, scored_p, pipe_d = _setup_shortlist(tmp_path, rows, scores)
    out = compute_shortlist(scored_p, clean_p, pipe_d)
    data = json.loads(json.dumps(out, default=str))
    assert "main" in data and "excluded" in data and "suppressed" in data
    assert data["main"]["example_primary"][0]["job_id"] == "aaaaaaaa"


def test_compute_shortlist_preserves_keywords_to_mirror_as_list(tmp_path):
    """Regression: parquet stores list columns as numpy.ndarray. to_jsonable
    must serialise them as JSON lists, not as the array's str repr (which
    would later iterate per-character and break shortlist rendering)."""
    rows = [{"job_id": "aaaaaaaa"}]
    scores = [_valid_score(
        job_id="aaaaaaaa", fit_score=80,
        keywords_to_mirror=["Widget Contract Management", "Source to Pay", "Doohickey"],
    )]
    clean_p, scored_p, pipe_d = _setup_shortlist(tmp_path, rows, scores)
    kw = compute_shortlist(scored_p, clean_p, pipe_d)["main"]["example_primary"][0]["keywords_to_mirror"]
    assert isinstance(kw, list)
    assert kw == ["Widget Contract Management", "Source to Pay", "Doohickey"]
    # Anti-regression: NOT a single string of the array's repr
    assert not isinstance(kw, str)


# ---------- render_shortlist_markdown ----------

def _sl_row(**over) -> dict:
    """One compute_shortlist "main" row, as render_shortlist_markdown sees it."""
    row = {
        "job_id": "aaaaaaaa",
        "vertical": "example_primary",
        "company": "Acme",
        "title": "Widget Functional Consultant",
        "location": "Boston, MA",
        "source": "indeed",
        "posted_date": "2026-06-01",
        "url": "https://x/1",
        "fit_score": 80,
        "fit_subscores": _split_score(80),
        "sponsorship_label": "opt_ok",
        "sponsorship_evidence": "no visa sponsorship language",
        "reasoning": "strong widget assembly overlap",
        "keywords_to_mirror": ["widget assembly", "gizmo", "doohickey", "calibration"],
        "suggested_action": "tailor",
        "already_seen": False,
        "application_status": "",
    }
    row.update(over)
    if "fit_score" in over and "fit_subscores" not in over:
        row["fit_subscores"] = _split_score(over["fit_score"])
    return row


def _render(cfg, main: dict, n_scored=10, n_clean=20, date_str="2026-06-06"):
    return render_shortlist_markdown(
        {"main": main, "excluded": [], "suppressed": []},
        cfg, date_str, n_scored, n_clean,
    )


def test_render_shortlist_header_counts_and_date(cfg):
    md = _render(cfg, {"example_primary": [_sl_row()]}, n_scored=7, n_clean=99)
    assert md.startswith("# Shortlist — 2026-06-06\n")
    assert "(7 of 99 scored, top 25 per vertical with fit >= 50)" in md


def test_render_shortlist_row_fields(cfg):
    md = _render(cfg, {"example_primary": [_sl_row()]})
    assert "### 1. 80 — Acme — Widget Functional Consultant" in md
    assert "- **job_id:** `aaaaaaaa`" in md
    # `https://x/1` is not a board /apply can submit to, so the row is marked.
    # The mark is derived from the url rather than the source name: keying it
    # on `source == "workday"` marked no rows at all, while leaving every
    # LinkedIn, Indeed and Ashby role silently un-submittable.
    assert ("- **location:** Boston, MA · **source:** indeed"
            " — **manual-apply, not auto-submittable** · **posted:** 2026-06-01") in md
    assert "- **sponsorship:** opt_ok — \"no visa sponsorship language\"" in md
    assert "- **why:** strong widget assembly overlap" in md
    assert "- **suggested:** tailor" in md
    assert "- **verify E-Verify** before submitting (manual step)" in md
    assert "- https://x/1" in md


def test_render_shortlist_mirrors_only_the_first_three_keywords(cfg):
    md = _render(cfg, {"example_primary": [_sl_row()]})
    assert "- **mirror in tailoring:** widget assembly, gizmo, doohickey" in md
    assert "IDoc" not in md


def test_render_shortlist_handles_missing_keywords(cfg):
    row = _sl_row()
    del row["keywords_to_mirror"]
    assert "- **mirror in tailoring:** \n" in _render(cfg, {"example_primary": [row]})


def test_render_shortlist_subscore_breakdown(cfg):
    md = _render(cfg, {"example_primary": [_sl_row(fit_score=80)]})
    sub = _split_score(80)
    assert (f"- **fit:** 80 (title {sub['title']} / skills {sub['skills']} "
            f"/ seniority {sub['seniority']} / domain {sub['domain']})") in md


def test_render_shortlist_accepts_json_encoded_subscores(cfg):
    """scored.parquet stores fit_subscores as a JSON string."""
    md = _render(cfg, {"example_primary": [_sl_row(fit_subscores=json.dumps(_split_score(80)))]})
    assert "- **fit:** 80 (title 30 / skills 30 / seniority 20 / domain 0)" in md


def test_render_shortlist_numbers_rows_within_a_section(cfg):
    rows = [_sl_row(job_id=f"{i:08x}", fit_score=90 - i) for i in range(3)]
    md = _render(cfg, {"example_primary": rows})
    for i in range(1, 4):
        assert f"### {i}. " in md


def test_render_shortlist_status_is_new_unless_already_seen(cfg):
    md = _render(cfg, {"example_primary": [_sl_row(already_seen=False,
                                       application_status="applied")]})
    assert "- **status:** new" in md


def test_render_shortlist_status_uses_application_status_when_seen(cfg):
    md = _render(cfg, {"example_primary": [_sl_row(already_seen=True,
                                       application_status="applied")]})
    assert "- **status:** applied" in md


def test_render_shortlist_sections_follow_config_order(cfg):
    md = _render(cfg, {v: [_sl_row(vertical=v, job_id=v[:8])] for v in cfg.names})
    positions = [md.index(f"## {cfg.verticals[v].display_name} ") for v in cfg.names]
    assert positions == sorted(positions)


def test_render_shortlist_section_header_carries_its_count(cfg):
    rows = [_sl_row(job_id=f"{i:08x}") for i in range(2)]
    md = _render(cfg, {"example_primary": rows})
    assert f"## {cfg.verticals['example_primary'].display_name} (2)" in md


def test_render_shortlist_empty_vertical_gets_a_placeholder(cfg):
    md = _render(cfg, {"example_primary": [_sl_row()]})
    # the two other configured verticals have no rows
    assert md.count("No keepers today in this vertical.") == len(cfg.names) - 1


def test_render_shortlist_all_empty_short_circuits(cfg):
    md = _render(cfg, {v: [] for v in cfg.names})
    assert md.endswith("\nNo keepers today in this vertical.\n")
    assert "##" not in md  # no per-vertical sections at all


def test_render_shortlist_missing_vertical_key_is_treated_as_empty(cfg):
    md = _render(cfg, {})
    assert md.endswith("\nNo keepers today in this vertical.\n")


class TestRenderShortlistInvariants:
    """These asserts are the last line of defence before a shortlist reaches
    the user: an ineligible or low-fit row here means a wasted application."""

    def test_over_cap_section_raises(self, cfg):
        rows = [_sl_row(job_id=f"{i:08x}") for i in range(26)]
        with pytest.raises(AssertionError, match="exceeds cap 25"):
            _render(cfg, {"example_primary": rows})

    def test_exactly_at_cap_is_allowed(self, cfg):
        rows = [_sl_row(job_id=f"{i:08x}") for i in range(25)]
        assert "### 25. " in _render(cfg, {"example_primary": rows})

    def test_cross_vertical_leak_raises(self, cfg):
        with pytest.raises(AssertionError, match="leaked into"):
            _render(cfg, {"example_primary": [_sl_row(vertical="example_tertiary")]})

    def test_below_min_fit_raises(self, cfg):
        with pytest.raises(AssertionError, match="fit 49 < 50"):
            _render(cfg, {"example_primary": [_sl_row(fit_score=49)]})

    def test_fit_exactly_50_is_allowed(self, cfg):
        assert "### 1. 50 " in _render(cfg, {"example_primary": [_sl_row(fit_score=50)]})

    def test_subscores_not_summing_to_fit_raises(self, cfg):
        row = _sl_row(fit_score=80, fit_subscores={"title": 30, "skills": 30,
                                                   "seniority": 20, "domain": 5})
        with pytest.raises(AssertionError, match="!= fit_score 80"):
            _render(cfg, {"example_primary": [row]})

    def test_ineligible_row_in_main_raises(self, cfg):
        with pytest.raises(AssertionError, match="ineligible in main"):
            _render(cfg, {"example_primary": [_sl_row(sponsorship_label="ineligible")]})

    def test_the_first_bad_row_raises_before_rendering_it(self, cfg):
        """Fail loud, not "render 24 rows and drop one"."""
        rows = [_sl_row(job_id="aaaaaaaa"), _sl_row(job_id="bbbbbbbb", fit_score=10)]
        with pytest.raises(AssertionError):
            _render(cfg, {"example_primary": rows})


def test_a_submittable_board_url_carries_no_manual_apply_mark(cfg):
    """The mark has to discriminate, not decorate every row."""
    md = _render(cfg, {"example_primary": [_sl_row(
        source="greenhouse",
        url="https://job-boards.greenhouse.io/widgetco/jobs/1000003",
    )]})
    assert "manual-apply" not in md


def test_ashby_is_not_flagged_manual_now_that_it_has_a_fill_driver(cfg):
    """The shortlist and the queue read the same flag, so a role the queue can
    fill must not be labelled by hand here."""
    md = _render(cfg, {"example_primary": [_sl_row(
        source="ashby",
        url="https://jobs.ashbyhq.com/widgetco/00000001-0000-0000-0000-000000000001",
    )]})
    assert "manual-apply, not auto-submittable" not in md


def test_a_workday_url_is_marked_whatever_its_source_says(cfg):
    """41 rows in clean.parquet carry a myworkdayjobs.com url under
    `source == "indeed"`. The old source-keyed check missed every one."""
    md = _render(cfg, {"example_primary": [_sl_row(
        source="indeed",
        url="https://widgetco.wd1.myworkdayjobs.com/en-US/careers/job/Widget-Engineer_R-1",
    )]})
    assert "manual-apply, not auto-submittable" in md
