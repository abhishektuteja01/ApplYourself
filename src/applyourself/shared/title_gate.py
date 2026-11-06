"""Which lane a title belongs to, and whether the title gate lets it through.

A title fits lane L when one of L's `classifier_rules` matches it, or one of
L's search terms (search, LinkedIn and abroad) appears in it, matched loosely
(`title_terms`). The gate is lenient: a title in some lane passes unless it
holds a ban term (`title_exclude_terms`, the global list in `defaults:`).
"""

from __future__ import annotations

from dataclasses import dataclass

from applyourself.shared import title_terms, verticals

NO_LANE = "no_lane"
BANNED = "banned"


@dataclass(frozen=True)
class Verdict:
    lane: str
    """The lane the title goes to; "" when it fits none."""
    reason: str = ""
    """Why it is dropped ("no_lane", "banned: <term>"); "" when it passes."""

    @property
    def passes(self) -> bool:
        return not self.reason


def classify_vertical_from_title(
    title: str | None, vcfg: verticals.VerticalsConfig | None = None
) -> str:
    """The first `classifier_rules` lane whose rule matches, or ""."""
    if not isinstance(title, str) or not title:
        return ""
    vcfg = vcfg or verticals.get_config()
    title = title_terms.nfc(title)
    for vertical, rule in vcfg.classifier_rules:
        if rule.matches(title):
            return vertical
    return ""


def _search_terms(lane: verticals.Vertical) -> tuple[str, ...]:
    return lane.search_terms + lane.linkedin_terms + lane.abroad_terms


def fits(title: str, lane: str, vcfg: verticals.VerticalsConfig | None = None) -> bool:
    """Whether `title`, found by a search for `lane`, fits it: one of its
    rules (the search is a `{match, require_any}` rule's context), one of its
    search terms, or a legacy include / strong-keep term (`config migrate`
    moves those into classifier_rules)."""
    vcfg = vcfg or verticals.get_config()
    v = vcfg.verticals.get(lane)
    if v is None or not isinstance(title, str) or not title:
        return False
    title = title_terms.nfc(title)
    if any(name == lane and rule.matches(title, True) for name, rule in vcfg.classifier_rules):
        return True
    terms = _search_terms(v) + v.title_include_terms + v.title_strong_keep_terms
    rx = title_terms.compile_terms(terms, vcfg.title_variants)
    return bool(rx and rx.search(title_terms.normalize(title)))


def _first_fit(title: str, vcfg: verticals.VerticalsConfig) -> str:
    """The classifier's lane, else the first lane (config order) one of whose
    search terms the title holds, else ""."""
    lane = classify_vertical_from_title(title, vcfg)
    if lane:
        return lane
    text = title_terms.normalize(title)
    for name, v in vcfg.verticals.items():
        rx = title_terms.compile_terms(_search_terms(v), vcfg.title_variants)
        if rx and rx.search(text):
            return name
    return ""


def _kept(title: str, lane: str, vcfg: verticals.VerticalsConfig) -> bool:
    """Whether a lane's keep term (a `keep: true` rule, or a legacy
    strong-keep term) is in the title: it beats a ban word."""
    if any(
        name == lane and getattr(rule, "keep", False) and rule.matches(title)
        for name, rule in vcfg.classifier_rules
    ):
        return True
    strong = title_terms.compile_terms(
        vcfg.verticals[lane].title_strong_keep_terms, vcfg.title_variants
    )
    return bool(strong and strong.search(title_terms.normalize(title)))


def lane_for(
    title: str | None,
    hint: str = "",
    source: str = "",
    vcfg: verticals.VerticalsConfig | None = None,
) -> Verdict:
    """The lane a title goes to and whether it passes. `hint` is the lane of
    the search term that found it: kept when the title fits it, else the
    title goes where `_first_fit` puts it. A manual add, and a row whose lane
    is no longer configured, always pass in the lane they came with."""
    vcfg = vcfg or verticals.get_config()
    title = title_terms.nfc(title) if isinstance(title, str) else ""
    if source == "manual" or (hint and hint not in vcfg.verticals):
        return Verdict(hint)
    if not title.strip():
        return Verdict("", NO_LANE)
    lane = hint if hint and fits(title, hint, vcfg) else _first_fit(title, vcfg)
    if not lane:
        return Verdict("", NO_LANE)
    if _kept(title, lane, vcfg):
        return Verdict(lane)
    hit = title_terms.first_exact(vcfg.verticals[lane].title_exclude_terms, title)
    return Verdict(lane, f"{BANNED}: {hit.lower()}" if hit else "")
