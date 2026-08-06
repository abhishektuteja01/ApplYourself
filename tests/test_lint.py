"""Tests for src/lint.py — deterministic two-tier linter."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.lint import (
    bullets_diction_pass_completed,
    compute_exempt_lines,
    find_phrase_violations,
    fix_mechanical,
    lint_bullets_md,
    load_de_ai_rules,
    parse_bullets_md,
)


# ---------- helpers ----------

def _rules(**overrides) -> dict:
    """Build a minimal de_ai_rules dict for tests."""
    base = {
        "mechanical": {
            "em_dash": ", ",
            "en_dash_in_range": "-",
            "en_dash_parenthetical": ", ",
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
        "bullets_diction_pass_completed": False,
    }
    base.update(overrides)
    return base


# ===========================================================
# Tier 1 — mechanical
# ===========================================================

def test_fix_mechanical_em_dash():
    text = "We had a meeting — it went well."
    out, subs = fix_mechanical(text, _rules())
    assert "—" not in out
    # Surrounding whitespace collapses so " — " -> ", " (not ",  ")
    assert out == "We had a meeting, it went well."
    assert any(s["category"] == "em_dash" for s in subs)


def test_fix_mechanical_en_dash_range_vs_paren():
    text = "Tenure 2022–2024 — see the report (a small – b sized)."
    out, subs = fix_mechanical(text, _rules())
    # range: 2022-2024 (no whitespace to collapse around digit-en-digit)
    assert "2022-2024" in out
    # parenthetical: " – " collapses to ", "
    assert "a small, b sized" in out
    cats = {s["category"] for s in subs}
    assert "en_dash_in_range" in cats
    assert "en_dash_parenthetical" in cats


def test_fix_mechanical_en_dash_spacious_date_range():
    """Resume date ranges like 'May 2022 – Jul 2024' must be recognised as
    range (digits on both sides across whitespace) -- not silently
    collapsed to 'May 2022, Jul 2024' as a parenthetical."""
    out, subs = fix_mechanical("Tenure: May 2022 – Jul 2024", _rules())
    assert out == "Tenure: May 2022 - Jul 2024"  # surrounding spaces preserved
    cats = {s["category"] for s in subs}
    assert cats == {"en_dash_in_range"}
    assert "May 2022 - Jul 2024" in out  # not "May 2022, Jul 2024"


@pytest.mark.parametrize("text,expected", [
    ("Acme (May 2022 – Present)", "Acme (May 2022 - Present)"),
    ("Acme (2022 – Present)", "Acme (2022 - Present)"),
    ("Acme (May 2022 – present)", "Acme (May 2022 - present)"),
    ("Acme (Jun 2021 – Current)", "Acme (Jun 2021 - Current)"),
    ("Acme (Mar 2019 – ongoing)", "Acme (Mar 2019 - ongoing)"),
    ("Acme (Mar 2019 – to date)", "Acme (Mar 2019 - to date)"),
    ("Acme | May 2022 – Present | Boston", "Acme | May 2022 - Present | Boston"),
])
def test_fix_mechanical_open_ended_date_range(text, expected):
    """The current role's end has no digit, so a digits-on-both-sides test
    reads it as a parenthetical and ships 'May 2022, Present' in every
    tailored resume header."""
    out, subs = fix_mechanical(text, _rules())
    assert out == expected
    assert {s["category"] for s in subs} == {"en_dash_in_range"}


@pytest.mark.parametrize("text,expected", [
    ("Shipped in 2024 – now the standard", "Shipped in 2024, now the standard"),
    ("Cut latency 40% – currently the fastest", "Cut latency 40%, currently the fastest"),
    ("Raised 3 rounds – ongoing work continues", "Raised 3 rounds, ongoing work continues"),
    ("the plan – now revised", "the plan, now revised"),
])
def test_fix_mechanical_open_ended_words_still_paren_mid_sentence(text, expected):
    """'now'/'ongoing' also start asides. A real end-date terminates its
    field; an aside continues into prose, which is what separates them."""
    out, subs = fix_mechanical(text, _rules())
    assert out == expected
    assert {s["category"] for s in subs} == {"en_dash_parenthetical"}


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


def test_find_phrase_violations_exempt_lines_skipped_in_resume():
    text = "leveraged here\nrobust elsewhere"
    v = find_phrase_violations(text, context="resume", exempt_lines={1}, rules=_rules())
    # Line 1 exempt -> 'leveraged' should not appear; line 2 still flagged
    assert not any(x["line"] == 1 for x in v)
    assert any(x["line"] == 2 and x["phrase"] == "robust" for x in v)


def test_find_phrase_violations_outreach_ignores_exempt_lines():
    """Outreach NEVER honors exempt_lines — there is no bullets-style exemption
    for fresh-generation text."""
    text = "I leveraged the new role."
    v = find_phrase_violations(
        text, context="outreach", exempt_lines={1}, rules=_rules(),
    )
    assert any(x["phrase"] == "leveraged" for x in v)


def test_find_phrase_violations_reports_line_and_column():
    text = "ok\nthis is robust stuff"
    v = find_phrase_violations(text, context="resume", rules=_rules())
    robust = [x for x in v if x["phrase"] == "robust"]
    assert len(robust) == 1
    assert robust[0]["line"] == 2
    assert robust[0]["column"] == 9


# ===========================================================
# Conditional bullets.md exemption
# ===========================================================

def test_compute_exempt_lines_diction_pass_off_returns_empty():
    bullets = {"B-DEL-01": {"canonical": "Owned the daily report."}}
    text = "Owned the daily report."
    out = compute_exempt_lines(text, bullets, diction_pass_done=False)
    assert out == set()


def test_compute_exempt_lines_verbatim_match_is_exempt():
    bullets = {"B-DEL-01": {"canonical": "Owned the daily report."}}
    text = "Some other line\nOwned the daily report.\nAnother"
    out = compute_exempt_lines(text, bullets, diction_pass_done=True)
    assert out == {2}


def test_compute_exempt_lines_handles_bullet_marker_prefix():
    bullets = {"B-DEL-01": {"canonical": "Owned the daily report."}}
    text = "- Owned the daily report."
    out = compute_exempt_lines(text, bullets, diction_pass_done=True)
    assert out == {1}


def test_compute_exempt_lines_single_char_edit_is_NOT_exempt():
    """The 'any rephrase is fully linted' guard — even one-character edit
    fails the verbatim check."""
    bullets = {"B-DEL-01": {"canonical": "Owned the daily report."}}
    text = "Owned the daily reports."   # trailing 's'
    out = compute_exempt_lines(text, bullets, diction_pass_done=True)
    assert out == set()


def test_compute_exempt_lines_diction_pass_on_but_no_match_is_empty():
    bullets = {"B-DEL-01": {"canonical": "Owned the daily report."}}
    text = "completely unrelated text"
    out = compute_exempt_lines(text, bullets, diction_pass_done=True)
    assert out == set()


# ===========================================================
# Bullets.md parsing
# ===========================================================

def test_parse_bullets_md_basic():
    txt = """# header comment
## B-DEL-01
source: Acme Corp
canonical: Owned the daily report.
tags: [a, b]
allowable_synonyms: []

## B-DEL-02
source: Acme Corp
canonical: Investigated breaks.
allowable_synonyms: []
"""
    out = parse_bullets_md(txt)
    assert set(out) == {"B-DEL-01", "B-DEL-02"}
    assert out["B-DEL-01"]["canonical"] == "Owned the daily report."
    assert out["B-DEL-01"]["allowable_synonyms"] == []
    assert out["B-DEL-01"]["tags"] == ["a", "b"]


def test_parse_bullets_md_ignores_blocks_without_canonical():
    txt = """## B-X-01
source: foo
allowable_synonyms: []
"""
    out = parse_bullets_md(txt)
    assert "B-X-01" not in out


# ===========================================================
# Rule loading
# ===========================================================

def test_load_de_ai_rules_and_flag(tmp_path):
    p = tmp_path / "rules.yaml"
    p.write_text("bullets_diction_pass_completed: true\n", encoding="utf-8")
    rules = load_de_ai_rules(p)
    assert bullets_diction_pass_completed(rules) is True


def test_bullets_diction_pass_default_false_when_missing_key():
    assert bullets_diction_pass_completed({}) is False


# ===========================================================
# Diction pass — must lint canonical only, never metadata
# ===========================================================

def _write_rules(tmp_path: Path) -> Path:
    p = tmp_path / "rules.yaml"
    p.write_text(yaml.safe_dump(_rules()), encoding="utf-8")
    return p


def test_lint_bullets_md_ignores_source_metadata_dates(tmp_path):
    """Date strings inside `source:` metadata (e.g. 'May 2022') must NOT
    false-flag the hedge 'may'. Those lines never reach a resume so they
    must never be linted. This is the bug from slice 5 follow-up."""
    bullets = tmp_path / "bullets.md"
    bullets.write_text(
        "## B-DEL-01\n"
        "source: Acme Corp (Springfield, May 2022 - Jul 2024)\n"
        "canonical: Owned the daily Stock Mark-to-Market risk report.\n"
        "tags: [a]\n"
        "evidence: production system\n"
        "allowable_synonyms: []\n",
        encoding="utf-8",
    )
    rules_path = _write_rules(tmp_path)
    rules = load_de_ai_rules(rules_path)
    _, violations = lint_bullets_md(bullets, rules=rules)
    assert violations == [], f"expected zero violations, got {violations}"


def test_lint_bullets_md_flags_real_hedge_in_canonical(tmp_path):
    """A genuine hedge in the canonical text IS still flagged — scoping
    is to canonical only, not 'don't flag anything ever'."""
    bullets = tmp_path / "bullets.md"
    bullets.write_text(
        "## B-X-01\n"
        "source: Foo\n"
        "canonical: This may have improved outcomes for the team.\n"
        "tags: [a]\n"
        "evidence: x\n"
        "allowable_synonyms: []\n",
        encoding="utf-8",
    )
    rules_path = _write_rules(tmp_path)
    rules = load_de_ai_rules(rules_path)
    _, violations = lint_bullets_md(bullets, rules=rules)
    hedge_hits = [v for v in violations
                  if v["bullet_id"] == "B-X-01" and v["phrase"] == "may"]
    assert len(hedge_hits) == 1, f"expected 1 hedge flag, got {violations}"


def test_lint_bullets_md_reports_by_bullet_id_not_file_line(tmp_path):
    """Violations are keyed by bullet_id so the user knows which bullet
    to fix; column is the position within the canonical text."""
    bullets = tmp_path / "bullets.md"
    bullets.write_text(
        "## B-DEL-01\nsource: foo\ncanonical: clean text here.\nallowable_synonyms: []\n\n"
        "## B-DEL-02\nsource: bar\ncanonical: Was responsible for breaking things.\nallowable_synonyms: []\n",
        encoding="utf-8",
    )
    rules_path = _write_rules(tmp_path)
    rules = load_de_ai_rules(rules_path)
    _, violations = lint_bullets_md(bullets, rules=rules)
    # Only B-DEL-02 has a violation (resume_cliche "responsible for")
    assert all(v["bullet_id"] == "B-DEL-02" for v in violations)
    assert any(v["phrase"] == "responsible for" for v in violations)
