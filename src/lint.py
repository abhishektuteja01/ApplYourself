"""Two-tier linter.

Determinism boundary (R7): no LLM calls. The rewrite loop happens in the
.claude/commands/tailor.md and .claude/commands/outreach.md sessions, with
find_phrase_violations as the deterministic referee.

Tier 1 (mechanical, auto-fix): em/en-dash, smart quotes, ellipsis, NBSP,
zero-width. Applied to everything including verbatim canonical text — no
exemption.

Tier 2 (phrase, flag-and-rewrite): banned phrases per profile/de_ai_rules.yaml.
NEVER auto-rewritten. The caller loops the LLM to rewrite the offending line
until the linter returns empty.

The bullets.md exemption (CONDITIONAL):
  - Applies ONLY when de_ai_rules.yaml: bullets_diction_pass_completed: true
  - Applies ONLY to lines that are VERBATIM unchanged canonical bullet text
  - Outreach is NEVER exempt (no bullets-style exemption for fresh-generation text)
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, TypedDict

import yaml


class Substitution(TypedDict):
    line: int
    column: int
    original: str
    replacement: str
    category: str


class Violation(TypedDict):
    line: int
    column: int
    phrase: str
    category: str


_DEFAULT_RULES_PATH = Path("profile/de_ai_rules.yaml")

# Mechanical default mappings — also serve as the fallback when
# de_ai_rules.yaml is absent (tests, fresh checkouts).
_MECH_DEFAULTS = {
    "em_dash": ", ",
    "en_dash_in_range": "-",
    "en_dash_parenthetical": ", ",
    "smart_quote_single": "'",
    "smart_quote_double": '"',
    "ellipsis": "...",
    "nbsp": " ",
    "zero_width": "",
}


# =====================================================================
# Rule loading
# =====================================================================

def load_de_ai_rules(path: Path | None = None) -> dict:
    """Load profile/de_ai_rules.yaml. Returns {} if file missing."""
    p = path or _DEFAULT_RULES_PATH
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def bullets_diction_pass_completed(rules: dict) -> bool:
    return bool(rules.get("bullets_diction_pass_completed", False))


def _endash_has_digit_neighbors_across_space(line: str, i: int) -> bool:
    """True if line[i] is an en-dash with digit content within an 8-char
    window on BOTH sides. Covers all common date-range shapes:
      - compact:  "2022–2024"
      - spacious: "May 2022 – Jul 2024"  (digit-content nearby on each side
                   but immediate neighbor is a month abbreviation, not a digit)
      - mixed:    "2022 – Jul 2024"
    Falls through to "parenthetical" when no digits are within reach on at
    least one side -- e.g. "(a small – b sized)"."""
    WINDOW = 8
    left = line[max(0, i - WINDOW):i]
    right = line[i + 1:i + 1 + WINDOW]
    return any(c.isdigit() for c in left) and any(c.isdigit() for c in right)


def _mech_rules(rules: dict | None) -> dict:
    if rules is None:
        rules = load_de_ai_rules()
    user = rules.get("mechanical", {}) or {}
    merged = {**_MECH_DEFAULTS, **user}
    return merged


# =====================================================================
# Tier 1 — mechanical auto-fix
# =====================================================================

def fix_mechanical(text: str, rules: dict | None = None) -> tuple[str, list[Substitution]]:
    """Apply mechanical substitutions; return (fixed_text, subs).

    Column numbers in subs reference the ORIGINAL line position so the user
    can locate the source character in the input."""
    m = _mech_rules(rules)
    subs: list[Substitution] = []
    out_lines: list[str] = []

    for lineno, line in enumerate(text.split("\n"), start=1):
        new_line = line

        # Em-dash (U+2014). Collapse any surrounding whitespace so " — " -> ", "
        # rather than ",  " (preserves resume typography).
        for match in re.finditer("—", line):
            subs.append(Substitution(
                line=lineno, column=match.start() + 1,
                original="—", replacement=m["em_dash"], category="em_dash",
            ))
        new_line = re.sub(r"\s*—\s*", m["em_dash"], new_line)

        # En-dash (U+2013): range vs parenthetical. "Range" = digit content
        # within 8 chars on BOTH sides across whitespace, which covers both
        # "2022-2024" (no spaces) and "May 2022 - Jul 2024" (spacious date
        # ranges -- the common form in resumes).
        # Walk new_line en-dashes once; decision + substitution + reporting
        # in lockstep so they can't disagree.
        new_parts: list[str] = []
        last = 0
        for endash_match in re.finditer("–", new_line):
            i = endash_match.start()
            is_range = _endash_has_digit_neighbors_across_space(new_line, i)
            if is_range:
                # Preserve surrounding whitespace; just swap the char.
                new_parts.append(new_line[last:i])
                new_parts.append(m["en_dash_in_range"])
                last = i + 1
                subs.append(Substitution(
                    line=lineno, column=i + 1, original="–",
                    replacement=m["en_dash_in_range"], category="en_dash_in_range",
                ))
            else:
                # Parenthetical: collapse surrounding whitespace.
                prefix = new_line[last:i].rstrip()
                new_parts.append(prefix)
                new_parts.append(m["en_dash_parenthetical"])
                # Advance past trailing whitespace following the en-dash.
                j = i + 1
                while j < len(new_line) and new_line[j].isspace():
                    j += 1
                last = j
                subs.append(Substitution(
                    line=lineno, column=i + 1, original="–",
                    replacement=m["en_dash_parenthetical"],
                    category="en_dash_parenthetical",
                ))
        new_parts.append(new_line[last:])
        new_line = "".join(new_parts)

        # Smart single quotes (U+2018 U+2019)
        for ch in ("‘", "’"):
            for match in re.finditer(re.escape(ch), line):
                subs.append(Substitution(
                    line=lineno, column=match.start() + 1, original=ch,
                    replacement=m["smart_quote_single"], category="smart_quote_single",
                ))
            new_line = new_line.replace(ch, m["smart_quote_single"])

        # Smart double quotes (U+201C U+201D)
        for ch in ("“", "”"):
            for match in re.finditer(re.escape(ch), line):
                subs.append(Substitution(
                    line=lineno, column=match.start() + 1, original=ch,
                    replacement=m["smart_quote_double"], category="smart_quote_double",
                ))
            new_line = new_line.replace(ch, m["smart_quote_double"])

        # Ellipsis (U+2026)
        for match in re.finditer("…", line):
            subs.append(Substitution(
                line=lineno, column=match.start() + 1, original="…",
                replacement=m["ellipsis"], category="ellipsis",
            ))
        new_line = new_line.replace("…", m["ellipsis"])

        # NBSP (U+00A0)
        for match in re.finditer(" ", line):
            subs.append(Substitution(
                line=lineno, column=match.start() + 1, original=" ",
                replacement=m["nbsp"], category="nbsp",
            ))
        new_line = new_line.replace(" ", m["nbsp"])

        # Zero-width space (U+200B)
        for match in re.finditer("​", line):
            subs.append(Substitution(
                line=lineno, column=match.start() + 1, original="​",
                replacement=m["zero_width"], category="zero_width",
            ))
        new_line = new_line.replace("​", m["zero_width"])

        out_lines.append(new_line)

    return "\n".join(out_lines), subs


# =====================================================================
# Tier 2 — phrase flag-and-rewrite
# =====================================================================

# A loose emoji range cover — good enough for v1; we only need to flag, not
# normalize. If a real emoji slips through, the lint loop catches it.
_EMOJI_RANGES = (
    (0x1F300, 0x1F9FF),
    (0x1FA00, 0x1FAFF),
    (0x2600, 0x27BF),
)


def _is_emoji(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES)


def _strip_category_tag(phrase: str) -> str:
    """Remove parenthetical tags like '(soft)' or '(non-literal)' from
    a phrase entry — these are author notes, not part of the literal match."""
    return re.sub(r"\s*\(.*?\)\s*", "", phrase).strip()


def find_phrase_violations(
    text: str,
    *,
    context: Literal["resume", "outreach"],
    exempt_lines: set[int] | None = None,
    rules: dict | None = None,
) -> list[Violation]:
    """Scan text for banned phrases; return one Violation per occurrence.

    exempt_lines (1-indexed) skips phrase scanning on those lines — but
    ONLY for context='resume'. Outreach NEVER honors exempt_lines: fresh-
    generation text gets no bullets.md-style exemption."""
    if rules is None:
        rules = load_de_ai_rules()

    if context == "outreach":
        exempt = set()
    else:
        exempt = set(exempt_lines) if exempt_lines else set()

    banned = rules.get("banned_phrases", {}) or {}
    no_emoji = bool(banned.get("no_emoji", False))
    no_exclamation = bool(banned.get("no_exclamation", False))

    # Phrase categories: every banned_phrases sub-list (skip the no_emoji /
    # no_exclamation booleans).
    phrase_cats: dict[str, list[str]] = {
        k: v for k, v in banned.items() if isinstance(v, list)
    }
    if context == "outreach":
        outreach_only = rules.get("outreach_only", []) or []
        if outreach_only:
            phrase_cats["outreach_only"] = list(outreach_only)

    violations: list[Violation] = []
    for lineno, line in enumerate(text.split("\n"), start=1):
        if lineno in exempt:
            continue
        lower = line.lower()

        for category, phrases in phrase_cats.items():
            for raw_phrase in phrases:
                phrase = _strip_category_tag(raw_phrase)
                if not phrase:
                    continue
                # Special: the "<greeting>!" pseudo-pattern for outreach
                if phrase == "<greeting>!":
                    for m in re.finditer(
                        r"\b(?:hi|hello|hey|dear|greetings)[^.!?\n]*!",
                        line, re.IGNORECASE,
                    ):
                        violations.append(Violation(
                            line=lineno, column=m.start() + 1,
                            phrase=m.group(), category=category,
                        ))
                    continue
                pat = re.escape(phrase.lower())
                # Word-boundary only when phrase starts AND ends with a word char.
                # Phrases ending in punctuation (e.g. "Notably,") would never match
                # with trailing \b since \b requires word/non-word transition.
                if phrase[0].isalnum() and phrase[-1].isalnum():
                    pat = r"\b" + pat + r"\b"
                for m in re.finditer(pat, lower):
                    # Date false-positive guard: month names like "May" trip
                    # the bare-word hedge "may". Skip when the match is
                    # immediately followed by whitespace + a 4-digit year.
                    if category == "hedges_when_softening_own_work":
                        tail = lower[m.end():m.end() + 6]
                        if re.match(r"\s\d{4}\b", tail):
                            continue
                    violations.append(Violation(
                        line=lineno, column=m.start() + 1,
                        phrase=phrase, category=category,
                    ))

        if no_emoji:
            for i, ch in enumerate(line):
                if _is_emoji(ch):
                    violations.append(Violation(
                        line=lineno, column=i + 1, phrase=ch, category="emoji",
                    ))
        if no_exclamation:
            for i, ch in enumerate(line):
                if ch == "!":
                    violations.append(Violation(
                        line=lineno, column=i + 1, phrase="!", category="exclamation",
                    ))

    return violations


# =====================================================================
# Conditional bullets.md exemption
# =====================================================================

def parse_bullets_md(text: str) -> dict[str, dict]:
    """Parse profile/bullets.md into {bullet_id: {canonical, ...}}.
    Lenient on missing fields — only bullets with a
    non-empty canonical: are returned."""
    bullets: dict[str, dict] = {}
    current_id: str | None = None
    current: dict = {}

    for raw in text.split("\n"):
        line = raw.rstrip()
        # File-level comment lines (#) at column 0 are skipped; bullet IDs are ## headings.
        if line.startswith("## "):
            if current_id and current.get("canonical"):
                bullets[current_id] = current
            current_id = line[3:].strip()
            current = {}
            continue
        if not line or line.startswith("#"):
            continue
        if current_id is None:
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if val.startswith("[") and val.endswith("]"):
            try:
                val = yaml.safe_load(val)
            except yaml.YAMLError:
                pass
        current[key] = val

    if current_id and current.get("canonical"):
        bullets[current_id] = current
    return bullets


def lint_bullets_md(
    bullets_md_path: Path,
    rules: dict | None = None,
) -> tuple[int, list[dict]]:
    """The one-time diction pass over profile/bullets.md.

    Lints ONLY each bullet's `canonical:` text -- the metadata fields
    (`source:`, `tags:`, `evidence:`, `allowable_synonyms:`) are NEVER
    shipped to a resume, so they must NEVER be linted. If you let them
    through, date strings inside `source:` (e.g. "May 2022 - Jul 2024")
    false-flag the hedge "may" against every work-history entry.

    Returns (mechanical_subs_count, violations) where each violation is
    {bullet_id, column, phrase, category}. Bullets are keyed by id so the
    report tells you which bullet to fix, not a raw bullets.md line number.

    Does NOT modify bullets_md_path -- bullets.md is user-authored.
    mechanical_subs_count is informational only."""
    if rules is None:
        rules = load_de_ai_rules()
    text = bullets_md_path.read_text(encoding="utf-8")
    bullets = parse_bullets_md(text)
    total_subs = 0
    violations: list[dict] = []
    for bullet_id, b in bullets.items():
        canonical = b.get("canonical")
        if not isinstance(canonical, str) or not canonical.strip():
            continue
        # Mechanical: count would-be substitutions; don't write back.
        _, subs = fix_mechanical(canonical, rules)
        total_subs += len(subs)
        # Phrase: no exemption -- this IS the pass that establishes the
        # canonical-text baseline that future exemptions rely on.
        for v in find_phrase_violations(canonical, context="resume",
                                         exempt_lines=None, rules=rules):
            violations.append({
                "bullet_id": bullet_id,
                "column": v["column"],
                "phrase": v["phrase"],
                "category": v["category"],
            })
    return total_subs, violations


def compute_exempt_lines(
    rendered_text: str,
    bullets: dict[str, dict],
    diction_pass_done: bool,
) -> set[int]:
    """Return 1-indexed line numbers in rendered_text that are eligible for
    phrase-violation exemption. Eligibility requires BOTH:
      1. diction_pass_done is True
      2. The line's text (after stripping leading whitespace and bullet markers)
         exactly equals some bullet's canonical text.
    Any single-character edit fails (2) — paraphrases are fully linted."""
    if not diction_pass_done:
        return set()
    canonical_texts = {
        str(b["canonical"]).strip()
        for b in bullets.values()
        if isinstance(b.get("canonical"), str) and b["canonical"].strip()
    }
    if not canonical_texts:
        return set()
    exempt: set[int] = set()
    for lineno, line in enumerate(rendered_text.split("\n"), start=1):
        stripped = line.strip()
        # Strip a leading bullet marker if present
        if stripped.startswith(("- ", "* ")):
            stripped = stripped[2:].strip()
        if stripped in canonical_texts:
            exempt.add(lineno)
    return exempt
