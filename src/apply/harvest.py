"""Fetch application forms in bulk and tabulate the questions they ask.

Motivation: `/apply` parks a role on any question no rule answers, and a
parked role is a role you fill by hand. But the same questions recur across
boards — "How did you hear about us?", "Are you willing to relocate?",
"Desired salary" — so most parks are the same handful of questions met over
and over. Measuring which ones, over the corpus of roles already scored worth
applying to, turns a recurring park into a one-time Tier B rule.

This is the same method §2's 30-board census used, at corpus scale.

**Tabulation only — no judgment (R7).** This module emits exact normalized
labels and counts. It deliberately does NOT cluster near-duplicates ("how did
you hear about us" vs "how did you hear about this role"): deciding those are
the same question is judgment, it belongs in a command session, and burying a
fuzzy-match policy in deterministic plumbing would leave the next reader
unable to tell which side of R7 it sits on.

Read-only: GETs a public application form, exactly as `apply plan` already
does for one role. Nothing is filled, nothing is submitted, no browser opens.
Paced, because doing this to a few hundred employers back to back from one IP
is the thing that gets an IP flagged.
"""
from __future__ import annotations

import json
import logging
import time
from collections import Counter
from dataclasses import asdict, dataclass, field as dc_field
from pathlib import Path

from src.apply.answers import _norm
from src.apply.detect import detect_ats
from src.apply.greenhouse import PostingExpired
from src.discovery.sources.ats.http import CareersError

log = logging.getLogger(__name__)

DEFAULT_PACING_SECONDS = 2.0


@dataclass(frozen=True)
class HarvestedQuestion:
    """One field as one board rendered it."""
    job_id: str
    ats: str
    board: str
    field_id: str
    label: str
    norm_label: str
    kind: str
    section: str
    required: bool
    multi: bool
    options: tuple[str, ...]


@dataclass
class HarvestResult:
    questions: list[HarvestedQuestion] = dc_field(default_factory=list)
    ok: list[str] = dc_field(default_factory=list)
    expired: list[str] = dc_field(default_factory=list)
    failed: list[tuple[str, str]] = dc_field(default_factory=list)

    @property
    def boards(self) -> int:
        return len(self.ok)


def normalize_label(label: str) -> str:
    """Casefold, collapse whitespace, drop a trailing `*`.

    Exact normalization only — the same shape `answers._norm` uses for rule
    matching, so a census label lines up with what a rule would actually see.
    No stemming and no fuzzy grouping: see the module docstring.
    """
    text = (label or "").replace(" ", " ").strip()
    if text.endswith("*"):
        text = text[:-1]
    return " ".join(text.split()).casefold()


def _board_loader(ats: str):
    # Imported lazily so a harvest of one ATS does not drag in the others.
    if ats == "greenhouse":
        from src.apply.greenhouse import load_board
        return load_board
    if ats == "lever":
        from src.apply.lever import load_board
        return load_board
    if ats == "ashby":
        from src.apply.ashby import load_board
        return load_board
    return None


def harvest_one(job_id: str, url: str) -> list[HarvestedQuestion]:
    """Every field one posting's form renders. Raises on a fetch/parse
    failure so the caller can categorize it."""
    ats = detect_ats(url)
    loader = _board_loader(ats)
    if loader is None:
        raise CareersError(f"{url}: not a board this harvester reads")
    board = loader(url)
    reconciled = board.reconciled
    return [
        HarvestedQuestion(
            job_id=job_id,
            ats=ats,
            board=getattr(board, "slug", "") or "",
            field_id=f.id,
            label=f.label or "",
            norm_label=normalize_label(f.label),
            kind=f.kind,
            section=f.section,
            required=bool(f.required),
            multi=bool(f.multi),
            options=tuple(o.label for o in f.options),
        )
        for f in reconciled.fields
    ]


def harvest(postings: list[tuple[str, str]], *, pacing: float = DEFAULT_PACING_SECONDS,
            sleeper=time.sleep, progress=None) -> HarvestResult:
    """Walk `(job_id, url)` pairs, paced. One bad board never stops the walk.

    Expect a meaningful expired rate — 7 of 35 top-scored Greenhouse roles
    already 404 on the embed URL (§13). That is an ordinary outcome of
    harvesting a corpus scored days or weeks ago, not a harvester bug.
    """
    result = HarvestResult()
    for i, (job_id, url) in enumerate(postings):
        if i:
            sleeper(pacing)
        try:
            result.questions.extend(harvest_one(job_id, url))
            result.ok.append(job_id)
        except PostingExpired:
            result.expired.append(job_id)
        except Exception as exc:  # noqa: BLE001 - one board must not stop the walk
            result.failed.append((job_id, f"{type(exc).__name__}: {exc}"))
        if progress is not None:
            progress(i + 1, len(postings), result)
    return result


def census(result: HarvestResult) -> list[dict]:
    """Per distinct normalized label: how many boards ask it, how often it is
    required, which widget kinds it renders as, and every option set seen.

    Sorted by board count so the questions worth a rule come first. Option
    sets are kept *separate* rather than unioned: §4 found eight boards asking
    "how did you hear about this job" with eight disjoint option lists, which
    is precisely why a rule's answer is a candidate list.
    """
    by_label: dict[str, list[HarvestedQuestion]] = {}
    for q in result.questions:
        by_label.setdefault(q.norm_label, []).append(q)

    rows = []
    for norm, qs in by_label.items():
        boards = {q.job_id for q in qs}
        # tuple(): a census can be recomputed from a reloaded JSON dump,
        # where every options tuple came back as an unhashable list.
        option_sets = sorted({tuple(q.options) for q in qs if q.options})
        rows.append({
            "norm_label": norm,
            "boards": len(boards),
            "required_on": len({q.job_id for q in qs if q.required}),
            "kinds": sorted({q.kind for q in qs}),
            "sections": sorted({q.section for q in qs}),
            "ats": sorted({q.ats for q in qs}),
            "multi": sorted({q.multi for q in qs}),
            "example_label": Counter(q.label for q in qs).most_common(1)[0][0],
            "distinct_option_sets": len(option_sets),
            "option_sets": [list(o) for o in option_sets],
        })
    rows.sort(key=lambda r: (-r["boards"], r["norm_label"]))
    return rows


def write_census(result: HarvestResult, out_dir: Path) -> tuple[Path, Path]:
    """Raw rows plus the aggregated census, both JSON."""
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / "harvest_raw.json"
    agg = out_dir / "harvest_census.json"
    raw.write_text(
        json.dumps([asdict(q) for q in result.questions], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    agg.write_text(
        json.dumps({
            "boards_ok": len(result.ok),
            "boards_expired": len(result.expired),
            "boards_failed": len(result.failed),
            "failures": result.failed,
            "census": census(result),
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return raw, agg
