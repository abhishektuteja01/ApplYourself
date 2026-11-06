"""Loose title-term matching: a term matches a title whatever its hyphens,
spaces, plurals and word endings, and any variant `title_variants` lists for
one of its words ("AI" for "artificial intelligence", "Ingenieur" for
"engineer"). A term that starts with a letter or digit never starts inside a
word, so "ML Engineer" misses "HTML Engineer" (".net" still finds "ASP.NET").
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

Variants = tuple[tuple[str, ...], ...]

# Between two words of a term: any run of spaces, hyphens, slashes and other
# punctuation, or nothing ("full stack", "full-stack", "fullstack").
_SEP = r"[\W_]*"
_TOKEN_RE = re.compile(r"[\w&+.#']+")
# A word ending these is matched from its stem on ("deployed" reads "deploy\w*").
_SUFFIXES = ("ments", "ment", "ings", "ing", "ed", "es", "s")
_MIN_STEM = 4
# Not inside a Latin word; a CJK letter right before is fine ("のForward").
_NOT_IN_WORD = r"(?<![0-9A-Za-z\u00c0-\u024f])"
# A short last word that may run on inside a compound ("LLM" in "LLMOps").
_OPEN_MIN = 3


def nfc(text: str | None) -> str:
    """One spelling for every accented letter ("é" composed, never e + accent)."""
    return unicodedata.normalize("NFC", text or "")


def normalize(title: str | None) -> str:
    """The title as the matchers read it: NFC, and "_" read as a space ("_" is
    a word character to \\b, so "IN_Manager_Tax" would hide "manager")."""
    return unicodedata.normalize("NFC", (title or "").replace("_", " ").replace("\xa0", " "))


def tokens(text: str) -> tuple[str, ...]:
    """A term's words, lowercased."""
    return tuple(
        t for t in _TOKEN_RE.findall(unicodedata.normalize("NFC", text).lower()) if t.strip(".'")
    )


def _stem(tok: str) -> str:
    if tok.isalpha() and len(tok) > _MIN_STEM:
        for suffix in _SUFFIXES:
            if tok.endswith(suffix) and len(tok) - len(suffix) >= _MIN_STEM:
                return tok[: -len(suffix)]
    return tok


def _word(tok: str, last: bool, open_end: bool) -> str:
    """One word: a long word from its stem on, a short one with an optional
    plural and, at the end of the term, nothing after it (unless `open_end`)."""
    if tok in ("&", "and"):
        return r"(?:&|and)"
    if tok.isalnum() and len(tok) >= _MIN_STEM:
        return re.escape(_stem(tok)) + r"\w*"
    if tok.isalnum():
        if last and open_end and len(tok) >= _OPEN_MIN:
            return re.escape(tok) + r"\w*"
        return re.escape(tok) + r"(?:e?s)?" + (r"(?!\w)" if last else "")
    return re.escape(tok) + (r"(?!\w)" if last and tok[-1:].isalnum() else "")


def _phrase(toks: tuple[str, ...], last: bool, open_end: bool) -> str:
    return _SEP.join(_word(t, last and i == len(toks) - 1, open_end) for i, t in enumerate(toks))


@lru_cache(maxsize=16)
def _groups(variants: Variants) -> tuple[tuple[tuple[str, ...], ...], ...]:
    return tuple(tuple(tokens(m) for m in group if tokens(m)) for group in variants)


def _parts(term: str, variants: Variants) -> list[tuple[int, tuple[tuple[str, ...], ...] | None]]:
    """The term's words as (word count, variants group or None), each group
    taking the longest run of words that spells one of its members."""
    toks = tokens(term)
    groups = _groups(variants)
    stems = tuple(_stem(t) for t in toks)
    out: list[tuple[int, tuple[tuple[str, ...], ...] | None]] = []
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
        out.append(best if best else (1, None))
        i += best[0] if best else 1
    return out


def word_count(term: str, variants: Variants = ()) -> int:
    """How many words a term needs in a title: a variants group counts once,
    so "artificial intelligence" is one word when "AI" is its variant."""
    return len(_parts(term, variants))


def term_pattern(term: str, variants: Variants = (), open_end: bool = False) -> str:
    """The regex source for one term, from its first word on (the caller
    anchors it so it never starts inside a word). Words of the term that
    spell one of a variants group's members (longest first) match any member
    of that group."""
    toks = tokens(term)
    parts: list[str] = []
    i = 0
    for k, group in _parts(term, variants):
        last = i + k == len(toks)
        if group is None:
            parts.append(_word(toks[i], last, open_end))
        else:
            parts.append("(?:" + "|".join(_phrase(m, last, open_end) for m in group) + ")")
        i += k
    return _SEP.join(parts)


@lru_cache(maxsize=256)
def compile_terms(
    terms: tuple[str, ...], variants: Variants = (), open_end: bool = False
) -> re.Pattern[str] | None:
    """One pattern matching any of `terms` loosely, or None with no terms.
    `open_end` lets a short last word run on ("LLM" finds "LLMOps")."""
    anchored: list[str] = []
    free: list[str] = []
    for t in terms:
        pat = term_pattern(t, variants, open_end)
        if pat:
            (anchored if tokens(t)[0][:1].isalnum() else free).append(pat)
    # One lookbehind for all word-start terms: much faster than one per term.
    alts = ([_NOT_IN_WORD + "(?:" + "|".join(anchored) + ")"] if anchored else []) + free
    return re.compile("|".join(alts), re.IGNORECASE) if alts else None


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
    return re.compile(
        "|".join(exact_pattern(unicodedata.normalize("NFC", t)) for t in terms), re.IGNORECASE
    )


def first_exact(terms: tuple[str, ...], title: str) -> str:
    """The first of `terms` the title holds as written (any case), or ""."""
    rx = compile_exact(terms)
    m = rx.search(normalize(title)) if rx else None
    if m is None:
        return ""
    return next(
        t
        for t in terms
        if re.fullmatch(exact_pattern(unicodedata.normalize("NFC", t)), m.group(0), re.IGNORECASE)
    )
