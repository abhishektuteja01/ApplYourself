"""The merge predicate: are these two rows the same posting?

Deterministic and LLM-free (R7). Board identity comes from
`sources/ats/registry.py`, never from a fourth hostname list.

Paths to a merge, in the order they are cheap: the same url; the same
(company_normalized, title_normalized) pair, which is what job_id already
hashes; the same board tenant plus a near-identical title; or a
near-identical title, a close company name and genuinely similar JD text. The
third path exists because two spellings of one company ("Hilbert" /
"Hilbert's AI") are indistinguishable from two different companies that share a
word ("Shakti Solutions" / "Goal Solutions") on the name alone — the JD is the
evidence. No JD text on either side means no evidence, so no merge.

Blocking only proposes candidate pairs; `same_posting` alone decides. Group
resolution keeps one survivor per merge group and hands the group's job_id to
`aliases.select_sticky_id`, which is the sole authority on which id survives —
the id is never recomputed from the canonical company name.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

import pandas as pd
from rapidfuzz import fuzz

from src.discovery import aliases
from src.discovery.sources.ats.registry import board_slug

TITLE_MIN = 90
COMPANY_MIN = 85
#: Blocking is deliberately looser than the merge itself.
BLOCK_COMPANY_MIN = 70
JD_MIN = 70
JD_WINDOW = 4000

_SQUASH_RE = re.compile(r"[^a-z0-9]")

# Level, seniority and track tokens, in canonical spelling. Two titles in one
# company that disagree on these are different roles however close their title
# ratio AND however similar their jd_text: a level-numbered pair scores 98 on
# title and routinely shares the same boilerplate JD, so evidence alone merges
# two real postings. Only an identical url overrides this.
LEVEL_TOKENS = frozenset({
    "i", "ii", "iii", "iv",
    "intern", "coop", "trainee", "apprentice",
    "junior", "entry", "graduate", "associate",
    "senior", "staff", "principal", "distinguished", "fellow",
    "lead", "manager", "director", "head", "vp", "chief", "president",
})

# Compared as canonical sets, never raw tokens: the same level is routinely
# spelled two ways across boards, and treating the spellings as different
# levels would exempt a genuine duplicate from collapsing.
_LEVEL_SYNONYMS = {
    "jr": "junior", "sr": "senior",
    "mgr": "manager", "mgmt": "manager", "management": "manager",
    "interns": "intern", "internship": "intern", "internships": "intern",
    "grad": "graduate", "apprenticeship": "apprentice",
    "1": "i", "2": "ii", "3": "iii", "4": "iv",
    "vice": "vp",
}
# normalize_title turns "Co-op" into two tokens, so rejoin it before matching.
_CO_OP_RE = re.compile(r"\bco op\b")


def level_tokens(title: str) -> frozenset[str]:
    tokens = (_LEVEL_SYNONYMS.get(t, t) for t in _CO_OP_RE.sub("coop", title or "").split())
    return frozenset(LEVEL_TOKENS.intersection(tokens))


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

    # job_id hashes exactly this pair, so two rows agreeing on it are one
    # posting by construction — letting them both through would write one
    # job_id twice. Evidence cannot overrule an identity the hash already
    # asserts.
    company_a = a.get("company_normalized") or ""
    if (
        company_a
        and company_a == (b.get("company_normalized") or "")
        and a.get("title_normalized")
        and a.get("title_normalized") == b.get("title_normalized")
    ):
        return True

    title_a = a.get("title_normalized") or ""
    title_b = b.get("title_normalized") or ""
    if fuzz.WRatio(title_a, title_b) < TITLE_MIN:
        return False
    if level_tokens(title_a) != level_tokens(title_b):
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


# ---------------------------------------------------------------------
# Blocking — propose candidate pairs, decide nothing
# ---------------------------------------------------------------------

#: A squashed company key shorter than this is too short to carry containment
#: evidence, and indexing it would bucket half the window together.
CONTAINMENT_MIN_CHARS = 4
_NGRAM = 4


def _containment_pairs(keys: list[str]) -> list[tuple[str, str]]:
    """Squashed-key pairs where one contains the other, found through a
    4-gram index so the cost is linear in total key length rather than
    quadratic in the number of companies."""
    postings: dict[str, set[str]] = {}
    for key in keys:
        for i in range(len(key) - _NGRAM + 1):
            postings.setdefault(key[i:i + _NGRAM], set()).add(key)
    pairs = set()
    for key in keys:
        if len(key) < CONTAINMENT_MIN_CHARS:
            continue
        for other in postings.get(key[:_NGRAM], ()):  # noqa: SIM118
            if other != key and key in other:
                pairs.add((key, other) if key < other else (other, key))
    return sorted(pairs)


def block(df: pd.DataFrame) -> list[list]:
    """Candidate buckets of index labels, on the five keys that can carry
    company identity: the resolved url, the board tenant slug, the squashed
    company key, one squashed key containing another, and an identical
    normalized title. Buckets overlap by design."""
    if df.empty:
        return []
    urls = df["url"].fillna("").astype(str).str.strip() if "url" in df.columns \
        else pd.Series("", index=df.index)
    companies = df["company_normalized"].fillna("").astype(str)
    titles = df["title_normalized"].fillna("").astype(str)

    buckets: list[list] = []
    for series, transform in (
        (urls, None),
        (urls, board_slug),
        (companies, squash_company),
        (titles, None),
    ):
        keyed: dict[str, list] = {}
        for idx, value in zip(df.index, series):
            key = transform(value) if transform else value
            if key:
                keyed.setdefault(key, []).append(idx)
        buckets.extend(members for members in keyed.values() if len(members) > 1)

    by_squash: dict[str, list] = {}
    for idx, company in zip(df.index, companies):
        key = squash_company(company)
        if key:
            by_squash.setdefault(key, []).append(idx)
    for short, long in _containment_pairs(list(by_squash)):
        buckets.append(by_squash[short] + by_squash[long])
    return buckets


# ---------------------------------------------------------------------
# Group resolution — one survivor per merge group
# ---------------------------------------------------------------------

#: Survivor tie-break, best first: an applyable url, then a location that
#: resolved inside the allowlist, then the longest jd_text.
SURVIVOR_KEYS = ["_not_applyable", "_loc_unresolved", "_jd_len"]
SURVIVOR_ASCENDING = [True, True, False]
LOCATION_CAP = 10


def _merge_groups(df: pd.DataFrame) -> list[list]:
    """Connected components over the pairs that survive `same_posting`.

    The two exact keys — an identical url, and an identical
    (company_normalized, title_normalized) pair — are equivalence relations, so
    they collapse by grouping and need no pairwise test at all. Only one
    representative per exact group reaches the fuzzy pass. On a real 14-day
    window that is the difference between ~27M candidate pairs and a few
    thousand: the window holds ~50k rows but far fewer distinct postings.
    """
    parent: dict = {idx: idx for idx in df.index}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    urls = df["url"].fillna("").astype(str).str.strip() if "url" in df.columns \
        else pd.Series("", index=df.index)
    companies = df["company_normalized"].fillna("").astype(str)
    titles = df["title_normalized"].fillna("").astype(str)
    exact: dict[tuple, object] = {}
    for idx, url, company, title in zip(df.index, urls, companies, titles):
        for key in (("url", url) if url else None,
                    ("pair", company, title) if company and title else None):
            if key is None:
                continue
            first = exact.setdefault(key, idx)
            if first is not idx:
                union(first, idx)

    # df is pre-sorted best-first, so the first member of each exact group is
    # also the group's tie-break winner.
    seen_roots: set = set()
    representatives: list = []
    for idx in df.index:
        root = find(idx)
        if root not in seen_roots:
            seen_roots.add(root)
            representatives.append(idx)

    reps = df.loc[representatives]
    rows = reps.to_dict("index")
    compared: set[tuple] = set()
    for bucket in block(reps):
        for i, a in enumerate(bucket):
            for b in bucket[i + 1:]:
                pair = (a, b) if a <= b else (b, a)
                if pair in compared:
                    continue
                compared.add(pair)
                if same_posting(rows[a], rows[b]):
                    union(a, b)

    groups: dict = {}
    for idx in df.index:
        groups.setdefault(find(idx), []).append(idx)
    return list(groups.values())


def _locations(df: pd.DataFrame, members: list) -> tuple[int, str]:
    seen = sorted({
        loc for loc in df.loc[members, "location"].fillna("").astype(str)
        if loc.strip()
    })
    return len(seen), " | ".join(seen[:LOCATION_CAP])


def resolve(
    df: pd.DataFrame,
    canonical: Mapping[str, str] | None = None,
    state_index: Mapping[str, Mapping] | None = None,
    seen_ids=(),
) -> tuple[pd.DataFrame, list[dict]]:
    """Collapse the window to one row per posting.

    `df` already carries `job_id`. The survivor row is the group's tie-break
    winner, but its id comes from `aliases.select_sticky_id` — the id follows
    whichever member is already tracked, so a merge never orphans a
    pipeline/<job_id>/state.yaml. Returns (survivors, merges) where each merge
    describes a group of more than one row, for the alias ledger and the run
    report.
    """
    if df.empty:
        out = df.copy()
        out["location_count"] = pd.Series(dtype=int)
        out["all_locations"] = pd.Series(dtype=str)
        return out, []

    canonical = dict(canonical or {})
    state_index = state_index or {}
    seen_ids = set(seen_ids)

    # A positional index, because the streamed window concatenates shards and
    # its labels repeat — duplicate labels silently collapse both the
    # union-find parent map and .loc lookups below.
    work = df.copy()
    work.index = pd.RangeIndex(len(work))
    work["_jd_len"] = work["jd_text"].fillna("").astype(str).str.len()
    if "_not_applyable" not in work.columns:
        work["_not_applyable"] = True
    if "_loc_unresolved" not in work.columns:
        work["_loc_unresolved"] = False
    work = work.sort_values(SURVIVOR_KEYS, ascending=SURVIVOR_ASCENDING, kind="stable")

    order = {idx: rank for rank, idx in enumerate(work.index)}
    survivors: list = []
    counts: dict = {}
    location_lists: dict = {}
    sticky: dict = {}
    merges: list[dict] = []

    for members in _merge_groups(work):
        members = sorted(members, key=lambda idx: order[idx])
        survivor = members[0]
        survivors.append(survivor)
        counts[survivor], location_lists[survivor] = _locations(work, members)
        if len(members) == 1:
            continue

        job_ids = list(dict.fromkeys(work.loc[members, "job_id"]))
        candidates = [
            aliases.AliasCandidate(
                variant_normalized=str(company),
                source_of_truth=aliases.source_of_truth(url),
            )
            for company, url in zip(
                work.loc[members, "company_normalized"], work.loc[members, "url"]
            )
        ]
        # A variant seen twice keeps the origin of its first, best-ranked row.
        origins: dict[str, str] = {}
        for cand in candidates:
            if cand.variant_normalized not in origins:
                origins[cand.variant_normalized] = cand.source_of_truth
        variants = list(origins)
        pinned = next((canonical[v] for v in variants if v in canonical), "")
        winner = pinned or aliases.choose_canonical(candidates)
        # The canonical spelling's own rows go first, so a group with no
        # tracked and no seen member still lands on the same id next run even
        # when jd_text length flips the tie-break.
        ordered_ids = sorted(
            job_ids,
            key=lambda j: j not in set(
                work.loc[members][work.loc[members, "company_normalized"] == winner]["job_id"]
            ),
        )
        chosen = aliases.select_sticky_id(ordered_ids, state_index, seen_ids)
        if chosen not in job_ids:  # pragma: no cover — guards the R10 contract
            raise AssertionError(
                f"sticky id {chosen!r} is not a member of merge group {job_ids}"
            )
        sticky[survivor] = chosen
        merges.append({
            "job_id": chosen,
            "job_ids": job_ids,
            "canonical": winner,
            "variants": variants,
            "origins": origins,
            "rows": len(members),
        })

    out = work.loc[sorted(survivors, key=lambda idx: order[idx])].copy()
    out["job_id"] = [sticky.get(idx, jid) for idx, jid in zip(out.index, out["job_id"])]
    out["location_count"] = [counts[idx] for idx in out.index]
    out["all_locations"] = [location_lists[idx] for idx in out.index]
    return out.drop(columns=["_jd_len"], errors="ignore"), merges
