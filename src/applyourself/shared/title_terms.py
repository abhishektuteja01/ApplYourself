"""Loose title-term matching: a term matches a title whatever its hyphens,
spaces, plurals and word endings, and any variant `title_variants` lists for
one of its words ("AI" for "artificial intelligence", "Ingenieur" for
"engineer"). A term never starts inside a word, so "ML Engineer" misses
"HTML Engineer".
"""

from __future__ import annotations

import re
from functools import lru_cache

Variants = tuple[tuple[str, ...], ...]

# Between two words of a term: any run of spaces, hyphens, slashes and other
# punctuation, or nothing ("full stack", "full-stack", "fullstack").
_SEP = r"[\W_]*"
_TOKEN_RE = re.compile(r"[\w&+.#']+")
# A word ending these is matched from its stem on ("deployed" reads "deploy\w*").
_SUFFIXES = ("ments", "ment", "ings", "ing", "ed", "es", "s")
_MIN_STEM = 4


def normalize(title: str | None) -> str:
    """The title as the matchers read it. "_" is a word character to \\b, so
    "IN_Manager_Tax" would hide "manager"."""
    return (title or "").replace("_", " ").replace("\xa0", " ")


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(t for t in _TOKEN_RE.findall(text.lower()) if t.strip(".'"))


def _stem(tok: str) -> str:
    if tok.isalpha() and len(tok) > _MIN_STEM:
        for suffix in _SUFFIXES:
            if tok.endswith(suffix) and len(tok) - len(suffix) >= _MIN_STEM:
                return tok[: -len(suffix)]
    return tok


def _word(tok: str, last: bool) -> str:
    """One word: a long word from its stem on, a short one with an optional
    plural and, at the end of the term, nothing after it."""
    if tok in ("&", "and"):
        return r"(?:&|and)"
    if tok.isalnum() and len(tok) >= _MIN_STEM:
        return re.escape(_stem(tok)) + r"\w*"
    if tok.isalnum():
        return re.escape(tok) + r"(?:e?s)?" + (r"(?!\w)" if last else "")
    return re.escape(tok) + (r"(?!\w)" if last and tok[-1:].isalnum() else "")


def _phrase(tokens: tuple[str, ...], last: bool) -> str:
    return _SEP.join(_word(t, last and i == len(tokens) - 1) for i, t in enumerate(tokens))


@lru_cache(maxsize=16)
def _groups(variants: Variants) -> tuple[tuple[tuple[str, ...], ...], ...]:
    return tuple(tuple(_tokens(m) for m in group if _tokens(m)) for group in variants)


def term_pattern(term: str, variants: Variants = ()) -> str:
    """The regex source for one term, from its first word on (the caller
    anchors it so it never starts inside a word). Words of the term that spell one of a
    variants group's members (longest first) match any member of that group."""
    toks = _tokens(term)
    groups = _groups(variants)
    stems = tuple(_stem(t) for t in toks)
    parts: list[str] = []
    i = 0
    while i < len(toks):
        best: tuple[int, tuple[tuple[str, ...], ...]] | None = None
        for group in groups:
            for member in group:
                k = len(member)
                if k > (best[0] if best else 0) and stems[i : i + k] == tuple(
                    _stem(t) for t in member
                ):
                    best = (k, group)
        last = (best[0] if best else 1) + i == len(toks)
        if best is None:
            parts.append(_word(toks[i], last))
            i += 1
            continue
        parts.append("(?:" + "|".join(_phrase(m, last) for m in best[1]) + ")")
        i += best[0]
    return _SEP.join(parts)


@lru_cache(maxsize=256)
def compile_terms(terms: tuple[str, ...], variants: Variants = ()) -> re.Pattern[str] | None:
    """One pattern matching any of `terms` loosely, or None with no terms."""
    alts = [p for p in (term_pattern(t, variants) for t in terms) if p]
    # One lookbehind for all terms: much faster than one per term.
    return re.compile(r"(?<!\w)(?:" + "|".join(alts) + ")", re.IGNORECASE) if alts else None


def exact_pattern(term: str) -> str:
    r"""`term` escaped, with \b only on a side that ends in a word character,
    so "c++" and ".net" still match. What a ban term matches: only itself."""
    pat = re.escape(term)
    if term[:1].isalnum():
        pat = r"\b" + pat
    if term[-1:].isalnum():
        pat = pat + r"\b"
    return pat


@lru_cache(maxsize=64)
def compile_exact(terms: tuple[str, ...]) -> re.Pattern[str] | None:
    if not terms:
        return None
    return re.compile("|".join(exact_pattern(t) for t in terms), re.IGNORECASE)


def first_exact(terms: tuple[str, ...], title: str) -> str:
    """The first of `terms` the title holds as written (any case), or ""."""
    rx = compile_exact(terms)
    m = rx.search(normalize(title)) if rx else None
    if m is None:
        return ""
    return next(t for t in terms if re.fullmatch(exact_pattern(t), m.group(0), re.IGNORECASE))
