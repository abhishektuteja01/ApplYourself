"""Which lane a title belongs to, and whether it passes that lane's title gate.

A title fits lane L when one of L's `classifier_rules` matches it, or one of
L's search terms (search, LinkedIn and abroad) appears in it, matched loosely
(`title_terms`).
"""

from __future__ import annotations

from applyourself.shared import title_terms, verticals

# ---------------------------------------------------------------------
# vertical classifier and title gate
# ---------------------------------------------------------------------


def classify_vertical_from_title(
    title: str | None, vcfg: verticals.VerticalsConfig | None = None
) -> str:
    """The first `classifier_rules` lane whose rule matches, or ""."""
    if not isinstance(title, str) or not title:
        return ""
    vcfg = vcfg or verticals.get_config()
    for vertical, rule in vcfg.classifier_rules:
        if rule.matches(title):
            return vertical
    return ""


def _lane_terms(lane: verticals.Vertical) -> tuple[str, ...]:
    # The include and strong-keep lists still count as fits while a config
    # carries them.
    return (
        lane.search_terms
        + lane.linkedin_terms
        + lane.abroad_terms
        + lane.title_include_terms
        + lane.title_strong_keep_terms
    )


def fits(title: str, lane: str, vcfg: verticals.VerticalsConfig | None = None) -> bool:
    """Whether `title` fits `lane`: one of its rules, or one of its terms."""
    vcfg = vcfg or verticals.get_config()
    v = vcfg.verticals.get(lane)
    if v is None or not isinstance(title, str) or not title:
        return False
    if any(name == lane and rule.matches(title) for name, rule in vcfg.classifier_rules):
        return True
    rx = title_terms.compile_terms(_lane_terms(v), vcfg.title_variants)
    return bool(rx and rx.search(title_terms.normalize(title)))


def _first_fit(title: str, vcfg: verticals.VerticalsConfig) -> str:
    """The classifier's lane, else the first lane (config order) one of whose
    terms the title holds, else ""."""
    lane = classify_vertical_from_title(title, vcfg)
    if lane:
        return lane
    text = title_terms.normalize(title)
    for name, v in vcfg.verticals.items():
        rx = title_terms.compile_terms(_lane_terms(v), vcfg.title_variants)
        if rx and rx.search(text):
            return name
    return ""


def title_passes(
    title: str, vertical: str, source: str = "", vcfg: verticals.VerticalsConfig | None = None
) -> bool:
    """The per-lane title gate: strong_keep OR (fits AND NOT exclude). A
    manual add and a title in no lane are not gated here."""
    if source == "manual":
        return True
    vcfg = vcfg or verticals.get_config()
    lane = vcfg.verticals.get(vertical)
    if lane is None:
        return True
    strong = title_terms.compile_terms(lane.title_strong_keep_terms, vcfg.title_variants)
    if strong and strong.search(title_terms.normalize(title)):
        return True
    if not fits(title, vertical, vcfg):
        return False
    return not title_terms.first_exact(lane.title_exclude_terms, title)


def gate_reason(
    title: str, source: str = "", vcfg: verticals.VerticalsConfig | None = None
) -> tuple[str, bool]:
    """(vertical, passes) for a board posting: its first fitting lane, then
    the gate. A title that fits no lane never passes."""
    vcfg = vcfg or verticals.get_config()
    vertical = _first_fit(title, vcfg)
    if not vertical:
        return "", False
    return vertical, title_passes(title, vertical, source, vcfg)
