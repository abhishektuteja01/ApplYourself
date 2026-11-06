"""Two-tier linter.

Determinism boundary (R7): no LLM calls. The rewrite loop happens in the
writing command sessions (/tailor, /cover-letter, /company-answers, /outreach),
with find_phrase_violations as the deterministic referee.

Tier 1 (mechanical, auto-fix): em/en-dash, smart quotes, ellipsis, NBSP,
zero-width.

Tier 2 (phrase, flag-and-rewrite): the `rules:` table in de_ai_rules.yaml.
NEVER auto-rewritten. A `block` violation fails the build and the caller rewrites
the offending line until none is left; a `warn` one is reported only.

Sources (bullets.md, skills_master.md, resume.yaml) are linted once at the
source by `verticals-check`; builds lint only the text a session wrote.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal, TypedDict

import yaml

from src.shared import config_schema, paths


class Substitution(TypedDict):
    line: int
    column: int
    original: str
    replacement: str
    category: str


Level = Literal["block", "warn"]


class Violation(TypedDict):
    line: int
    column: int
    phrase: str
    category: str
    level: Level


# The keys de_ai_rules.yaml `mechanical:` must define. The file is the only
# source of the replacement values.
MECH_KEYS = (
    "em_dash",
    "en_dash_in_range",
    "en_dash_parenthetical",
    "hyphen",
    "smart_quote_single",
    "smart_quote_double",
    "ellipsis",
    "nbsp",
    "zero_width",
)


class LintConfigError(ValueError):
    """de_ai_rules.yaml is missing or incomplete."""


# =====================================================================
# Rule loading
# =====================================================================


def load_de_ai_rules(path: Path | None = None) -> dict[str, Any]:
    """Load profile/de_ai_rules.yaml, else the shipped data/defaults/ one, every
    key checked. A missing or malformed file raises LintConfigError."""
    p = path or next(
        (q for q in (paths.DE_AI_RULES, paths.default("de_ai_rules.yaml")) if q.is_file()),
        paths.DE_AI_RULES,
    )
    if not p.exists():
        raise LintConfigError(f"{p} is missing")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    try:
        config_schema.validate(config_schema.DeAiRules, data, p)
    except ValueError as exc:
        raise LintConfigError(str(exc)) from exc
    return data


def _mech_rules(rules: dict[str, Any] | None) -> dict[str, Any]:
    if rules is None:
        rules = load_de_ai_rules()
    m = rules.get("mechanical") or {}
    missing = [k for k in MECH_KEYS if k not in m]
    if missing:
        raise LintConfigError(f"de_ai_rules.yaml mechanical: missing keys {missing}")
    return m


# =====================================================================
# Shared token tests
# =====================================================================

_MONTHS = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_WEEKDAYS = (
    r"mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:rs(?:day)?)?|"
    r"fri(?:day)?|sat(?:urday)?|sun(?:day)?"
)
_DATE_WORD = re.compile(rf"(?:{_MONTHS}|{_WEEKDAYS})\.?", re.IGNORECASE)
# An open-ended range ends in a word, not a digit: "May 2022 – Present".
# The lookahead keeps an aside out: in "in 2024 – now the standard" the word
# continues into prose, where a real end-date terminates its field.
_OPEN_ENDED_RANGE = re.compile(
    r"\s*(present|current|now|ongoing|to date)\b(?!\s+\w)", re.IGNORECASE
)
_EDGE_PUNCT = "()[]{},;:'\"*"


def _is_range_end(token: str) -> bool:
    """A number ("2024", "40%", "'26") or a month/weekday name."""
    token = token.strip(_EDGE_PUNCT)
    return bool(re.fullmatch(r"[~$]?\d[\d.,]*%?|'\d\d", token) or _DATE_WORD.fullmatch(token))


def _dash_is_range(line: str, i: int) -> bool:
    """True if the dash at line[i] joins two dates, numbers or day names:
    "2022–2024", "May 2022 – Jul 2024", "Mon–Fri", "May 2022 – Present",
    "40%–60%". Anything else is an aside ("the plan – now revised")."""
    left = re.search(r"(\S+)\s*$", line[:i])
    right = re.match(r"\s*(\S+)", line[i + 1 :])
    if not left or not _is_range_end(re.split(r"[–—―]", left.group(1))[-1]):
        return False
    if _OPEN_ENDED_RANGE.match(line[i + 1 :]):
        return True
    return right is not None and _is_range_end(re.split(r"[–—―]", right.group(1))[0])


def _dash_is_minus(line: str, i: int) -> bool:
    """A dash glued to a number on its right with nothing on its left: "–5%"."""
    return i + 1 < len(line) and line[i + 1].isdigit() and (i == 0 or line[i - 1] in " ([")


# =====================================================================
# Tier 1 — mechanical auto-fix
# =====================================================================

_DASH_KIND = {"–": "en_dash", "—": "em_dash", "―": "em_dash"}

# (category, characters) for the plain character swaps. Column numbers are
# reported against the ORIGINAL line.
_CHAR_SUBSTITUTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("hyphen", ("‑", "‒", "−")),
    ("smart_quote_single", ("‘", "’")),
    ("smart_quote_double", ("“", "”")),
    ("ellipsis", ("…",)),
    ("nbsp", (" ", " ", " ", " ", " ")),
    ("zero_width", ("\u200b", "‌", "‍", "⁠", "﻿")),
)


def fix_mechanical(
    text: str, rules: dict[str, Any] | None = None
) -> tuple[str, list[Substitution]]:
    """Apply mechanical substitutions; return (fixed_text, subs).

    One dash rule for en and em dashes: a dash joining two dates, numbers or
    day names (or a leading minus) becomes `en_dash_in_range` with its spacing
    kept; any other dash is an aside and collapses to its parenthetical
    replacement. Column numbers in subs reference the ORIGINAL line."""
    m = _mech_rules(rules)
    subs: list[Substitution] = []
    out_lines: list[str] = []

    for lineno, line in enumerate(text.split("\n"), start=1):
        parts: list[str] = []
        last = 0
        for dash in re.finditer("[–—―]", line):
            i = dash.start()
            kind = _DASH_KIND[dash.group()]
            if _dash_is_range(line, i) or _dash_is_minus(line, i):
                parts.append(line[last:i])
                repl, category = m["en_dash_in_range"], f"{kind}_in_range"
                last = i + 1
            else:
                parts.append(line[last:i].rstrip())
                repl = m["em_dash"] if kind == "em_dash" else m["en_dash_parenthetical"]
                category = "em_dash" if kind == "em_dash" else "en_dash_parenthetical"
                j = i + 1
                while j < len(line) and line[j].isspace():
                    j += 1
                last = j
            parts.append(repl)
            subs.append(
                Substitution(
                    line=lineno,
                    column=i + 1,
                    original=dash.group(),
                    replacement=repl,
                    category=category,
                )
            )
        parts.append(line[last:])
        new_line = "".join(parts)

        for category, chars in _CHAR_SUBSTITUTIONS:
            for ch in chars:
                for match in re.finditer(re.escape(ch), line):
                    subs.append(
                        Substitution(
                            line=lineno,
                            column=match.start() + 1,
                            original=ch,
                            replacement=m[category],
                            category=category,
                        )
                    )
                new_line = new_line.replace(ch, m[category])

        out_lines.append(new_line)

    return "\n".join(out_lines), subs


# =====================================================================
# Tier 2 — phrase flag-and-rewrite
# =====================================================================

# A loose emoji range cover: this is the only emoji check, and it flags rather
# than normalizes. Ranges are deliberately partial — add to them as needed.
_EMOJI_RANGES = (
    (0x1F300, 0x1F9FF),
    (0x1FA00, 0x1FAFF),
    (0x2600, 0x27BF),
)


def _is_emoji(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES)


# Text that is never prose: link targets, URLs, emails, and a month name used
# as a date ("May 2022", "May – Jul 2024", "Jan – May", "May, 2026", "May '26").
_PROTECTED = (
    re.compile(r"\]\([^)]*\)"),
    re.compile(r"(?:https?://|www\.|mailto:|tel:)\S+", re.IGNORECASE),
    re.compile(r"[\w.+!-]+@[\w-]+\.[\w.]+"),
    re.compile(rf"\b(?:{_MONTHS})\b\.?(?=\s*(?:\d|'\d|[-–—]|,\s*\d))", re.IGNORECASE),
    re.compile(rf"(?<=[-–—])\s*\b(?:{_MONTHS})\b", re.IGNORECASE),
)
# Where a sentence starts: the line start after any list marker, or after
# sentence-ending punctuation.
_SENTENCE_START = re.compile(r"^\s*(?:[-*•]\s+|\d+[.)]\s+)?|[.!?;:][\"')\]]*\s+")
_NUMBER_AHEAD = re.compile(r"\s*[~$]?\d")


def _mask(line: str, keep: tuple[str, ...]) -> str:
    """Lowercased line with protected spans and keep-terms blanked to `_`,
    same length, so reported columns stay true to the original."""
    chars = list(line.lower())

    def blank(start: int, end: int) -> None:
        chars[start:end] = "_" * (end - start)

    for pat in _PROTECTED:
        for m in pat.finditer(line):
            blank(m.start(), m.end())
    for term in keep:
        term = term.strip()
        if not term:
            continue
        rx = re.escape(term)
        if term[0].isalnum() and term[-1].isalnum():
            rx = rf"\b{rx}\b"
        for m in re.finditer(rx, line, re.IGNORECASE):
            blank(m.start(), m.end())
    return "".join(chars)


def find_phrase_violations(
    text: str,
    *,
    context: Literal["resume", "outreach"],
    rules: dict[str, Any] | None = None,
    keep: Iterable[str] = (),
) -> list[Violation]:
    """Scan text against every rule whose `scope` holds `context`; one Violation
    per occurrence, carrying the rule's `label` as category and its `level`.

    Link targets, URLs, emails, month names used as dates, and every `keep`
    term (company, recipient, skill names and aliases, context titles and
    orgs: proper nouns the writer can't reword) are never scanned."""
    if rules is None:
        rules = load_de_ai_rules()
    keep = tuple(keep)
    active = [r for r in rules.get("rules") or [] if context in r["scope"]]

    violations: list[Violation] = []

    def hit(lineno: int, col: int, phrase: str, category: str, level: Level) -> None:
        violations.append(
            Violation(line=lineno, column=col + 1, phrase=phrase, category=category, level=level)
        )

    for lineno, line in enumerate(text.split("\n"), start=1):
        lower = _mask(line, keep)
        starts = sorted({m.end() for m in _SENTENCE_START.finditer(lower)})

        for rule in active:
            label, level, how = rule["label"], rule["level"], rule["match"]
            for phrase in rule["phrases"]:
                if how == "regex":
                    for m in re.finditer(phrase, lower):
                        hit(lineno, m.start(), line[m.start() : m.end()], label, level)
                    continue
                if how == "sentence_start":
                    for pos in starts:
                        if lower.startswith(phrase.lower(), pos):
                            hit(lineno, pos, phrase, label, level)
                    continue
                pat = re.escape(phrase.lower())
                # Word-boundary only when phrase starts AND ends with a word char.
                # Phrases ending in punctuation (e.g. "Notably,") would never match
                # with trailing \b since \b requires word/non-word transition.
                if phrase[0].isalnum() and phrase[-1].isalnum():
                    pat = r"\b" + pat + r"\b"
                for m in re.finditer(pat, lower):
                    # "approximately 40%" states precision, not doubt.
                    if rule.get("unless_before") == "number" and _NUMBER_AHEAD.match(
                        lower, m.end()
                    ):
                        continue
                    hit(lineno, m.start(), phrase, label, level)

        if rules.get("no_emoji"):
            for i, ch in enumerate(lower):
                if _is_emoji(ch):
                    hit(lineno, i, ch, "emoji", "block")
        if rules.get("no_exclamation"):
            for i, ch in enumerate(lower):
                if ch == "!":
                    hit(lineno, i, "!", "exclamation", "block")

    return violations


def blocking(violations: Iterable[Violation]) -> list[Violation]:
    """The violations that fail a build; the rest are warnings."""
    return [v for v in violations if v["level"] == "block"]
