"""The merge predicate: are these two rows the same posting?

Deterministic and LLM-free (R7). Board identity comes from
`sources/ats/registry.py`, never from a fourth hostname list.

Three independent paths to a merge, in the order they are cheap:
the same url; the same board tenant plus a near-identical title; or a
near-identical title, a close company name and genuinely similar JD text. The
third path exists because two spellings of one company ("Hilbert" /
"Hilbert's AI") are indistinguishable from two different companies that share a
word ("Shakti Solutions" / "Goal Solutions") on the name alone — the JD is the
evidence. No JD text on either side means no evidence, so no merge.

Blocking, group resolution and the cleaning wiring land with the dedupe
rewrite; this module holds the pairwise decision the alias migration also needs.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

from rapidfuzz import fuzz

from src.discovery.sources.ats.registry import board_slug

TITLE_MIN = 90
COMPANY_MIN = 85
#: Blocking is deliberately looser than the merge itself.
BLOCK_COMPANY_MIN = 70
JD_MIN = 70
JD_WINDOW = 4000

_SQUASH_RE = re.compile(r"[^a-z0-9]")


def squash_company(company_normalized: str | None) -> str:
    """The company key with every separator removed, so `observeai` and
    `Observe.AI` land on one key. token_set_ratio scores that pair at 74 —
    below any threshold that still rejects `Shakti Solutions`."""
    return _SQUASH_RE.sub("", (company_normalized or "").lower())


def company_matches(a: str, b: str) -> bool:
    return (
        squash_company(a) == squash_company(b) and bool(squash_company(a))
    ) or fuzz.token_set_ratio(a or "", b or "") >= COMPANY_MIN


def blocks_together(a: Mapping, b: Mapping) -> bool:
    """Cheap candidate test: same normalized title, plausibly same company."""
    if not a.get("title_normalized") or a["title_normalized"] != b.get("title_normalized"):
        return False
    return fuzz.token_set_ratio(
        a.get("company_normalized") or "", b.get("company_normalized") or ""
    ) >= BLOCK_COMPANY_MIN


def same_posting(a: Mapping, b: Mapping) -> bool:
    """Whether two clean-shaped rows are one posting."""
    url_a, url_b = (a.get("url") or "").strip(), (b.get("url") or "").strip()
    if url_a and url_a == url_b:
        return True

    title_score = fuzz.WRatio(a.get("title_normalized") or "", b.get("title_normalized") or "")
    if title_score < TITLE_MIN:
        return False

    slug_a, slug_b = board_slug(url_a), board_slug(url_b)
    if slug_a and slug_a == slug_b:
        return True

    if not company_matches(a.get("company_normalized") or "", b.get("company_normalized") or ""):
        return False

    jd_a = (a.get("jd_text") or "")[:JD_WINDOW]
    jd_b = (b.get("jd_text") or "")[:JD_WINDOW]
    if not jd_a or not jd_b:
        return False
    return fuzz.ratio(jd_a, jd_b) >= JD_MIN
