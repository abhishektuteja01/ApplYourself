"""One keyword normalizer: two spellings of one keyword get one key.

Folds case (casefold, NFKC), punctuation, hyphens and spaces ("full-stack",
"full stack", "fullstack"), plurals ("LLMs", "evaluations") and the alias
groups in `data/defaults/keyword_variants.yaml`. No other ending folds
("marketing" never reads as "market"). A skill's `allowable_synonyms` are
display aliases and `title_variants` are title words ("ia"), so neither merges
keywords. Words of three letters or fewer keep their form, so "ai" never reads
as "a"; "c", "c++", "c#" and "r" stay apart. No LLM (R7).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import pairwise

import yaml

from applyourself.shared import paths

NAME = "keyword_variants.yaml"
Groups = Iterable[Iterable[str]]

_POSSESSIVE = re.compile(r"(?<=\w)['\u2019]s\b")
_TOKEN = re.compile(r"(?:(?<!\w)\.)?[^\W_](?:[\w+#]|&(?=[^\W_]))*")


def _singular(word: str, keep: frozenset[str]) -> str:
    """A lowercase ASCII word without its plural; never "-ss", "-us", "-is",
    a word of three letters or fewer, or one the shipped `keep_plural` lists."""
    if len(word) <= 3 or word in keep:
        return word
    if word.endswith("ies") and len(word) >= 5:
        return word[:-3] + "y"
    if word.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _fold(tok: str) -> str:
    low = tok.casefold()
    return _singular(low, _shipped()[1]) if low.isascii() and low.isalpha() else low


def forms(text: str | None) -> tuple[str, ...]:
    """The text's words, each folded; no aliases."""
    text = unicodedata.normalize("NFKC", str(text or "")).replace("_", " ")
    text = _POSSESSIVE.sub("", text)
    return tuple(_fold(t) for t in _TOKEN.findall(text))


@dataclass(frozen=True)
class Variants:
    """Alias groups: a run of folded words maps to its group's words."""

    table: dict[tuple[str, ...], tuple[str, ...]] = field(default_factory=dict)
    longest: int = 0


def build(groups: Groups = ()) -> Variants:
    """One table from alias groups. Groups sharing a member join, keyed by
    the first group's first member."""
    parent: dict[tuple[str, ...], tuple[str, ...]] = {}

    def find(x: tuple[str, ...]) -> tuple[str, ...]:
        while parent.setdefault(x, x) != x:
            x = parent[x]
        return x

    for g in groups:
        members = [f for f in (forms(m) for m in g) if f]
        for f in members[1:]:
            a, b = find(members[0]), find(f)
            if a != b:
                parent[b] = a
    table = {f: find(f) for f in parent}
    return Variants(table, max((len(f) for f in table), default=0))


def _substitute(words: tuple[str, ...], v: Variants) -> tuple[str, ...]:
    out: list[str] = []
    i = 0
    while i < len(words):
        for k in range(min(v.longest, len(words) - i), 0, -1):
            hit = v.table.get(words[i : i + k])
            if hit is not None:
                out += hit
                i += k
                break
        else:
            out.append(words[i])
            i += 1
    return tuple(out)


def words(text: str | None, variants: Variants | None = None) -> tuple[str, ...]:
    """The text's folded words, each alias run replaced by its group's words."""
    w = forms(text)
    return _substitute(w, variants) if variants and variants.table else w


def key(text: str | None, variants: Variants | None = None) -> str:
    """One key per keyword: its folded words joined, so spacing never counts."""
    return "".join(words(text, variants))


def _splits_ok(want: tuple[str, ...], cuts: set[int]) -> bool:
    """Whether the text's word breaks `cuts` (offsets into the joined keyword)
    split each keyword word only into one-letter pieces ("A.I.") or pieces of
    three letters or more ("full stack"), never "re act"."""
    start = 0
    for w in want:
        inner = sorted(c for c in cuts if start < c < start + len(w))
        if inner:
            edges = [start, *inner, start + len(w)]
            sizes = {b - a for a, b in pairwise(edges)}
            if sizes != {1} and min(sizes) < 3:
                return False
        start += len(w)
    return True


def contains(text: str | None, keyword: str | None, variants: Variants | None = None) -> bool:
    """Whether `keyword` appears in `text` as whole words, in any form. Words
    may join or split only as `_splits_ok` allows."""
    want = words(keyword, variants)
    whole = "".join(want)
    if not whole:
        return False
    hay = words(text, variants)
    for i in range(len(hay)):
        acc, cuts = "", set()
        for j in range(i, len(hay)):
            if acc:
                cuts.add(len(acc))
            acc += hay[j]
            if not whole.startswith(acc):
                break
            if acc == whole:
                if _splits_ok(want, cuts):
                    return True
                break
    return False


@lru_cache(maxsize=1)
def _shipped() -> tuple[tuple[tuple[str, ...], ...], frozenset[str]]:
    p = paths.default(NAME)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    groups = data.get("variants") if isinstance(data, dict) else None
    keep = data.get("keep_plural") if isinstance(data, dict) else None
    if not isinstance(groups, list) or not all(
        isinstance(g, list) and len(g) >= 2 and all(isinstance(m, str) and m.strip() for m in g)
        for g in groups
    ):
        raise ValueError(f"{p}: `variants` must be a list of groups of two or more spellings")
    if not isinstance(keep, list) or not all(isinstance(w, str) and w.strip() for w in keep):
        raise ValueError(f"{p}: `keep_plural` must be a list of words")
    return tuple(tuple(g) for g in groups), frozenset(w.casefold() for w in keep)


def variants() -> Variants:
    """The shipped groups as one table."""
    return build(_shipped()[0])
