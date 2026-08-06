"""Deterministic pre-screens: the judgments that need no judge.

Split out of scoring_io, which was three modules in one file. Everything here is
a pure predicate over a JD or a title, decided from config — no parquet, no LLM
(R7). /score applies these before any judge sees a row, because paying for a
call to confirm a keyword match is waste.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from src import paths
from src import verticals

# Hard-ineligible pre-label: a JD containing a `hard_ineligible` phrase is
# labeled ineligible before any judge runs, routed exactly like a judge-assigned
# label — never as a `skip`, which is a different thing. Substring match only;
# every nuanced sponsorship case stays with the judge.
SPONSORSHIP_RULES_PATH = paths.PROFILE / "sponsorship_rules.yaml"


HARD_INELIGIBLE_SCORED_BY = "rubric:hard-ineligible-pre-screen"


HARD_INELIGIBLE_REASONING = (
    "Auto-labeled ineligible by deterministic pre-screen: JD text contains "
    "a hard_ineligible phrase (citizenship/clearance bar) from "
    "profile/sponsorship_rules.yaml. Excluded from the shortlist and "
    "never fit-scored."
)


# Out-of-lane pre-screen: rows with vertical="" are auto-skipped at
# fit_score=0 without an LLM call. The human-facing text is the config's
# `out_of_lane.reasoning`.
AUTO_SKIP_SCORED_BY = "rubric:title-out-of-lane"


def load_hard_ineligible(path: Path = SPONSORSHIP_RULES_PATH) -> tuple[str, ...]:
    """Lowercased `hard_ineligible` phrases from sponsorship_rules.yaml.
    Missing key -> () (the pre-label is opt-in); missing file fails loud."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    phrases = data.get("hard_ineligible") or []
    if not isinstance(phrases, list) or not all(isinstance(p, str) and p for p in phrases):
        raise ValueError(f"{path}: hard_ineligible must be a list of nonempty strings")
    return tuple(p.lower() for p in phrases)


def hard_ineligible_phrase(phrases: tuple[str, ...], jd_text: Any) -> str | None:
    """First matching hard_ineligible phrase in jd_text (case-insensitive
    substring), else None."""
    if not isinstance(jd_text, str) or not jd_text:
        return None
    lowered = jd_text.lower()
    for p in phrases:
        if p in lowered:
            return p
    return None


def _normalize_jd(jd_text: Any) -> str:
    """Lowercase jd_text and strip markdown noise (backslash-escaped
    punctuation like "5\\+ years", "\\-", "\\&"; "**" bold markers) that
    would otherwise break a "number directly followed by years/experience"
    or "degree clause" regex match. Returns "" for non-string/empty input."""
    if not isinstance(jd_text, str) or not jd_text:
        return ""
    text = jd_text.lower()
    text = re.sub(r"\\([+\-&])", r"\1", text)
    text = text.replace("**", "")
    return text


# Minimum-years disqualifier. Requires a year-count within a few words of
# "experience", so an unrelated year mention ("150-year legacy") cannot
# false-positive. The optional "-M" group captures the LOWER bound of a range,
# without which "3-6 years" reads as a flat 6-year requirement.
EXPERIENCE_YEARS_RE = re.compile(
    r"(\d+)\s*(?:\+|[-–—]\s*\d+\+?)?\s*years?\s+(?:of\s+)?(?:[a-z][\w/&,-]*\s+){0,4}experience",
)


def max_years_required(jd_text: Any) -> int:
    """Return the highest N found in an explicit "N+ years ... experience"
    phrase in jd_text (0 if none found)."""
    normalized = _normalize_jd(jd_text)
    if not normalized:
        return 0
    return max((int(n) for n in EXPERIENCE_YEARS_RE.findall(normalized)), default=0)


def disqualify_reason(
    vertical: verticals.Vertical, jd_text: Any, title: Any = None
) -> str | None:
    """Return 'title', 'phrase' or 'years' if the row trips one of the
    vertical's configured disqualifier checks (in that priority order),
    else None. `title_phrases` match the job title, `phrases` and
    `max_years` match jd_text; all come from the vertical's `disqualifier`
    block in profile/verticals.yaml. All lists are optional —
    a vertical with empty lists gets the years check only.

    Semantic degree-requirement cases stay out: telling a genuinely closed
    degree list from an ordinary "or related field" listing is a judgment call
    a keyword regex over-triggers on. Those belong in the vertical's rubric.md,
    for the judge."""
    if isinstance(title, str) and title:
        title_lowered = title.lower()
        if any(p in title_lowered for p in vertical.disqualifier_title_phrases):
            return "title"
    if not isinstance(jd_text, str) or not jd_text:
        return None
    lowered = jd_text.lower()
    if any(phrase in lowered for phrase in vertical.disqualifier_phrases):
        return "phrase"
    if max_years_required(jd_text) > vertical.disqualifier_max_years:
        return "years"
    return None
