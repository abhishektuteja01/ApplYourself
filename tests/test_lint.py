"""Tests for src/lint.py — deterministic two-tier linter."""
from __future__ import annotations


import pytest

from src.lint import (
    LintConfigError,
    find_phrase_violations,
    fix_mechanical,
    load_de_ai_rules,
)


# ---------- helpers ----------

def _rules(**overrides) -> dict:
    """Build a minimal de_ai_rules dict for tests."""
    base = {
        "mechanical": {
            "em_dash": ", ",
            "en_dash_in_range": "-",
            "en_dash_parenthetical": ", ",
            "hyphen": "-",
            "smart_quote_single": "'",
            "smart_quote_double": '"',
            "ellipsis": "...",
            "nbsp": " ",
            "zero_width": "",
        },
        "banned_phrases": {
            "soft_verbs": ["leveraged", "spearheaded", "orchestrated"],
            "stock_adjectives": ["robust", "seamless"],
            "stock_nouns": ["synergy"],
            "stock_verbs": ["delve", "deep dive"],
            "filler_starters": ["Notably,", "Furthermore,"],
            "resume_cliches": ["responsible for", "team player"],
            "hedges_when_softening_own_work": ["potentially", "may"],
            "no_emoji": True,
            "no_exclamation": True,
        },
        "outreach_only": [
            "I hope this message finds you well",
            "I wanted to reach out",
            "<greeting>!",
        ],
    }
    base.update(overrides)
    return base


# ===========================================================
# Tier 1 — mechanical
# ===========================================================

def test_em_dash_pass_does_not_shift_reported_columns():
    """Columns reference the ORIGINAL line, per fix_mechanical's contract."""
    text = "Role — Company 2022–2024 done"
    _, subs = fix_mechanical(text, _rules())
    reported = sorted(s["column"] for s in subs)
    true_cols = sorted(i + 1 for i, ch in enumerate(text) if ch in "—–")
    assert reported == true_cols


def test_fix_mechanical_smart_quotes():
    text = "She said “hello” and ‘goodbye’."
    out, subs = fix_mechanical(text, _rules())
    assert out == "She said \"hello\" and 'goodbye'."
    cats = {s["category"] for s in subs}
    assert "smart_quote_double" in cats
    assert "smart_quote_single" in cats


def test_fix_mechanical_ellipsis():
    text = "Wait…"
    out, _ = fix_mechanical(text, _rules())
    assert out == "Wait..."


def test_fix_mechanical_nbsp_and_zero_width():
    text = "a b​c"
    out, _ = fix_mechanical(text, _rules())
    assert out == "a bc"


def test_fix_mechanical_reports_substitutions_with_line_and_column():
    text = "line one—\nline two—"
    _, subs = fix_mechanical(text, _rules())
    em = [s for s in subs if s["category"] == "em_dash"]
    assert len(em) == 2
    assert em[0]["line"] == 1
    assert em[0]["column"] == 9   # 1-indexed column of "—" on line 1
    assert em[1]["line"] == 2
    assert em[1]["column"] == 9


# ===========================================================
# Tier 2 — phrase
# ===========================================================

def test_find_phrase_violations_soft_verbs():
    v = find_phrase_violations("I leveraged the new system.", context="resume", rules=_rules())
    assert any(x["phrase"] == "leveraged" and x["category"] == "soft_verbs" for x in v)


def test_find_phrase_violations_stock_adjectives():
    v = find_phrase_violations("Built a robust pipeline.", context="resume", rules=_rules())
    assert any(x["phrase"] == "robust" and x["category"] == "stock_adjectives" for x in v)


def test_find_phrase_violations_resume_cliches_multiword():
    v = find_phrase_violations("Was responsible for reporting.", context="resume", rules=_rules())
    assert any(x["phrase"] == "responsible for" for x in v)


def test_find_phrase_violations_filler_starters():
    v = find_phrase_violations("Notably, the system scaled.", context="resume", rules=_rules())
    assert any(x["category"] == "filler_starters" for x in v)


def test_find_phrase_violations_filler_starters_not_mid_sentence():
    v = find_phrase_violations(
        "The board is called Notably, not a filler word here.",
        context="resume", rules=_rules(),
    )
    assert not any(x["category"] == "filler_starters" for x in v)


def test_find_phrase_violations_hedges():
    v = find_phrase_violations("This may improve outcomes.", context="resume", rules=_rules())
    assert any(x["phrase"] == "may" and x["category"] == "hedges_when_softening_own_work" for x in v)


def test_find_phrase_violations_no_emoji():
    v = find_phrase_violations("Shipped \U0001F680 today.", context="resume", rules=_rules())
    assert any(x["category"] == "emoji" for x in v)


def test_find_phrase_violations_no_exclamation_in_resume():
    v = find_phrase_violations("Great work!", context="resume", rules=_rules())
    assert any(x["category"] == "exclamation" for x in v)


def test_find_phrase_violations_outreach_only_NOT_flagged_in_resume():
    v = find_phrase_violations(
        "I wanted to reach out about the role.",
        context="resume", rules=_rules(),
    )
    assert not any(x["category"] == "outreach_only" for x in v)


def test_find_phrase_violations_outreach_only_flagged_in_outreach():
    v = find_phrase_violations(
        "I wanted to reach out about the role.",
        context="outreach", rules=_rules(),
    )
    assert any(x["category"] == "outreach_only" for x in v)


def test_find_phrase_violations_outreach_greeting_with_exclamation():
    v = find_phrase_violations("Hi Sarah!", context="outreach", rules=_rules())
    cats = {x["category"] for x in v}
    # Both the <greeting>! rule AND the no_exclamation rule should fire
    assert "outreach_only" in cats
    assert "exclamation" in cats


def test_find_phrase_violations_reports_line_and_column():
    text = "ok\nthis is robust stuff"
    v = find_phrase_violations(text, context="resume", rules=_rules())
    robust = [x for x in v if x["phrase"] == "robust"]
    assert len(robust) == 1
    assert robust[0]["line"] == 2
    assert robust[0]["column"] == 9


# ===========================================================
# Rule loading
# ===========================================================

def test_load_de_ai_rules_missing_file_fails_loud(tmp_path):
    with pytest.raises(LintConfigError, match="missing"):
        load_de_ai_rules(tmp_path / "absent.yaml")


# ===========================================================
# One dash rule, protected text, sentence starts
# ===========================================================

@pytest.mark.parametrize("text,expected", [
    # ranges: dates, numbers, day names, open-ended ends
    ("May 2022 — Jul 2024", "May 2022 - Jul 2024"),
    ("Tenure: May 2022 – Jul 2024", "Tenure: May 2022 - Jul 2024"),
    ("Tenure 2022–2024", "Tenure 2022-2024"),
    ("Open Mon–Fri", "Open Mon-Fri"),
    ("Jan – May 2024", "Jan - May 2024"),
    ("grew 40%–60%", "grew 40%-60%"),
    ("Acme (May 2022 – Present)", "Acme (May 2022 - Present)"),
    ("Acme (2022 – Present)", "Acme (2022 - Present)"),
    ("Acme (May 2022 – present)", "Acme (May 2022 - present)"),
    ("Acme (Jun 2021 – Current)", "Acme (Jun 2021 - Current)"),
    ("Acme (Mar 2019 – ongoing)", "Acme (Mar 2019 - ongoing)"),
    ("Acme (Mar 2019 – to date)", "Acme (Mar 2019 - to date)"),
    ("Acme | May 2022 – Present | Boston", "Acme | May 2022 - Present | Boston"),
    # minus sign
    ("Cut cost by –5%", "Cut cost by -5%"),
    # asides, including open-ended words that continue into prose
    ("We had a meeting — it went well.", "We had a meeting, it went well."),
    ("see the report (a small – b sized).", "see the report (a small, b sized)."),
    ("the plan — now revised", "the plan, now revised"),
    ("the plan – now revised", "the plan, now revised"),
    ("Shipped in 2024 – now the standard", "Shipped in 2024, now the standard"),
    ("Cut latency 40% – currently the fastest", "Cut latency 40%, currently the fastest"),
    ("Raised 3 rounds – ongoing work continues", "Raised 3 rounds, ongoing work continues"),
    # an earlier dash does not change how a later one reads
    ("aaaa – 2024", "aaaa, 2024"),
    ("1 — aaaa – 2024", "1, aaaa, 2024"),
])
def test_fix_mechanical_one_dash_rule(text, expected):
    out, _ = fix_mechanical(text, _rules())
    assert out == expected


def test_fix_mechanical_hyphen_variants_and_invisibles():
    out, subs = fix_mechanical("non‑blocking − a b﻿c", _rules())
    assert out == "non-blocking - a bc"
    assert {"hyphen", "nbsp", "zero_width"} <= {s["category"] for s in subs}


def test_fix_mechanical_missing_mechanical_key_fails_loud():
    from src.lint import LintConfigError
    rules = _rules()
    del rules["mechanical"]["hyphen"]
    with pytest.raises(LintConfigError, match="hyphen"):
        fix_mechanical("x", rules)


@pytest.mark.parametrize("text", [
    "* Notably, the system scaled.",
    "Built X. Furthermore, it ran nightly.",
    "1. Notably, it shipped.",
])
def test_filler_starters_caught_at_every_sentence_start(text):
    v = find_phrase_violations(text, context="resume", rules=_rules())
    assert any(x["category"] == "filler_starters" for x in v)


@pytest.mark.parametrize("text", [
    "Tenure: May 2022 - Jul 2024",
    "Tenure: May - Jul 2024",
    "Joined May, 2026",
    "Since May '26",
    "Jan-May 2024",
])
def test_month_may_is_not_a_hedge(text):
    v = find_phrase_violations(text, context="resume", rules=_rules())
    assert not any(x["phrase"] == "may" for x in v)


def test_keep_terms_are_not_scanned():
    rules = _rules(banned_phrases={"stock_adjectives": ["myriad"], "hedges_when_softening_own_work": ["may"]})
    v = find_phrase_violations("Dear May Chen, Myriad Genetics builds tests.",
                               context="resume", rules=rules, keep=["Myriad Genetics", "May Chen"])
    assert v == []


def test_exclamation_inside_url_or_email_is_not_flagged():
    text = "[site](https://example.com/#!/home) | https://x.io/a!b | a!b@example.com"
    v = find_phrase_violations(text, context="resume", rules=_rules())
    assert not any(x["category"] == "exclamation" for x in v)


def test_hedge_before_a_number_is_precision_not_doubt():
    rules = _rules(banned_phrases={"hedges_when_softening_own_work": ["approximately"]})
    assert find_phrase_violations("Cut cost approximately 40%.", context="resume", rules=rules) == []
    assert find_phrase_violations("It approximately works.", context="resume", rules=rules) != []
