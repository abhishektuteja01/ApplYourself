"""Which lane a title belongs to, and whether it passes that lane's title gate."""

from __future__ import annotations

import re
from functools import lru_cache

from src.shared import verticals

# ---------------------------------------------------------------------
# vertical classifier and title gate
# ---------------------------------------------------------------------


def classify_vertical_from_title(title: str | None) -> str:
    """The first `classifier_rules` lane whose rule matches, or ""."""
    if not isinstance(title, str) or not title:
        return ""
    for vertical, rule in verticals.get_config().classifier_rules:
        if rule.matches(title):
            return vertical
    return ""


def _term_pattern(term: str) -> str:
    r"""`term` escaped, with \b only on a side that ends in a word character,
    so "c++" and ".net" still match."""
    pat = re.escape(term)
    if term[:1].isalnum():
        pat = r"\b" + pat
    if term[-1:].isalnum():
        pat = pat + r"\b"
    return pat


def _compile(terms) -> re.Pattern | None:
    if not terms:
        return None
    return re.compile("|".join(_term_pattern(t) for t in terms), re.IGNORECASE)


@lru_cache(maxsize=64)
def _gate_patterns(exclude: tuple, include: tuple, strong: tuple):
    return _compile(exclude), _compile(include), _compile(strong)


def title_passes(title: str, vertical: str, source: str = "", vcfg=None) -> bool:
    """The per-lane title gate: strong_keep OR (include_ok AND NOT exclude).
    include_ok is True when the lane sets no include terms. A manual add and
    a title in no lane are not gated here."""
    if source == "manual":
        return True
    vcfg = vcfg or verticals.get_config()
    lane = vcfg.verticals.get(vertical)
    if lane is None:
        return True
    exclude, include, strong = _gate_patterns(
        tuple(lane.title_exclude_terms),
        tuple(lane.title_include_terms),
        tuple(lane.title_strong_keep_terms),
    )
    # "_" is a word character to \b, so "IN_Manager_Tax" hides "manager".
    text = (title or "").replace("_", " ")
    if strong and strong.search(text):
        return True
    if include and not include.search(text):
        return False
    return not (exclude and exclude.search(text))


def gate_reason(title: str, source: str = "", vcfg=None) -> tuple[str, bool]:
    """(vertical, passes) for a board posting: classify, then gate. A title
    that classifies into no lane never passes."""
    vertical = classify_vertical_from_title(title)
    if not vertical:
        return "", False
    return vertical, title_passes(title, vertical, source, vcfg)
