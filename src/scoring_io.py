"""Pure parquet plumbing for /score and /rescore.

Determinism boundary: no LLM calls in this module. /score and
/rescore invoke these helpers via Bash; the LLM judging happens inside the
.claude/commands/ slash-command session, not here. A bug in this module
could silently drop or duplicate scored rows, so it carries pytest coverage
even though the scoring prompt itself does not.
"""
from __future__ import annotations

import contextlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from src import verticals

log = logging.getLogger(__name__)


# Closed schema for scored.parquet.
SCORED_COLUMNS: list[str] = [
    "job_id",
    "fit_score",
    "fit_subscores",       # JSON string: {title, skills, seniority, domain}
    "vertical",            # a profile/verticals.yaml name | "" (title-out-of-lane rows)
    "sponsorship_label",
    "sponsorship_evidence",
    "reasoning",
    "keywords_to_mirror",  # list[str]
    "suggested_action",
    "shortlist_rank",      # int | NaN (stored as float64)
    "scored_at",
    "scored_by_model",
]

# Fields the LLM needs to judge a row. Written to jobs/scored.staging/unscored.jsonl
# by dump_unscored.
UNSCORED_FIELDS: list[str] = [
    "job_id", "source", "company", "title", "location",
    "posted_date", "remote_flag", "url", "jd_text",
    "salary_min", "salary_max", "salary_currency",
    "employment_type", "seniority_raw", "vertical",
]

VALID_LABELS = {"sponsors", "opt_ok", "ineligible", "unknown"}
VALID_ACTIONS = {"tailor", "skip", "manual-review"}
SUBSCORE_AXES = ("title", "skills", "seniority", "domain")
AXIS_MAXIMA = {"title": 30, "skills": 30, "seniority": 20, "domain": 20}

# Higher = preferred. ineligible never reaches the main list, but included
# for completeness so the sort key never sees a missing label.
SPONSORSHIP_PREF = {"sponsors": 3, "opt_ok": 2, "unknown": 1, "ineligible": 0}

# Pipeline states at or past the point of application — exclude from the
# shortlist main list because the user has already acted on these roles.
_APPLIED_STATES = frozenset({
    "applied", "recruiter_contact", "screen", "interview",
    "offer", "rejected", "withdrawn", "ghosted",
})


# Out-of-lane pre-screen for /score: rows whose `vertical` matches no
# configured vertical (vertical="") are auto-skipped with fit_score=0 — this
# is a deterministic outcome anyway, so
# paying an LLM call to confirm it is waste. dump_unscored gates on the
# precomputed clean.parquet `vertical` column; the stamp below is the generic
# mechanism, the
# human-facing reasoning text lives in profile/verticals.yaml
# `out_of_lane.reasoning`.
AUTO_SKIP_SCORED_BY = "rubric:title-out-of-lane"

# Hard-ineligible pre-label (carve-out, added 2026-07-14):
# rows whose JD contains an unambiguous clearance/citizenship phrase from
# `sponsorship_rules.yaml: hard_ineligible` are labeled ineligible before
# any judge runs — identical shortlist-exclusion routing to a judge-assigned
# label (never a `skip`; a suppressed row is not an ineligible row).
# Judgment-free substring
# match only; every nuanced sponsorship case stays with the LLM judge.
SPONSORSHIP_RULES_PATH = Path("profile/sponsorship_rules.yaml")
HARD_INELIGIBLE_SCORED_BY = "rubric:hard-ineligible-pre-screen"
HARD_INELIGIBLE_REASONING = (
    "Auto-labeled ineligible by deterministic pre-screen: JD text contains "
    "a hard_ineligible phrase (citizenship/clearance bar) from "
    "profile/sponsorship_rules.yaml. Excluded from the shortlist and "
    "never fit-scored."
)


def load_hard_ineligible(path: Path = SPONSORSHIP_RULES_PATH) -> tuple[str, ...]:
    """Lowercased `hard_ineligible` phrases from sponsorship_rules.yaml.
    Missing key -> () (the pre-label is opt-in); missing file fails loud."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    phrases = data.get("hard_ineligible") or []
    if not isinstance(phrases, list) or not all(isinstance(p, str) and p for p in phrases):
        raise ValueError(f"{path}: hard_ineligible must be a list of nonempty strings")
    return tuple(p.lower() for p in phrases)


def hard_ineligible_phrase(phrases: tuple[str, ...], jd_text: Any) -> str | None:
    """First matching hard_ineligible phrase in jd_text (case-insensitive
    substring), else None."""
    if not isinstance(jd_text, str) or not jd_text:
        return None
    lowered = jd_text.lower()
    for p in phrases:
        if p in lowered:
            return p
    return None


def _normalize_jd(jd_text: Any) -> str:
    """Lowercase jd_text and strip markdown noise (backslash-escaped
    punctuation like "5\\+ years", "\\-", "\\&"; "**" bold markers) that
    would otherwise break a "number directly followed by years/experience"
    or "degree clause" regex match. Returns "" for non-string/empty input."""
    if not isinstance(jd_text, str) or not jd_text:
        return ""
    text = jd_text.lower()
    text = re.sub(r"\\([+\-&])", r"\1", text)
    text = text.replace("**", "")
    return text


# Explicit minimum-years-of-experience disqualifier (locked 2026-06-17).
# Scoped to phrases that pair a year-count with the word "experience"
# within a few words ("5+ years of experience", "2+ years of direct
# experience") so generic year mentions elsewhere in the JD (e.g. "150-year
# legacy") never false-positive. Takes the highest N found anywhere in the
# JD; >=5 years stated anywhere disqualifies outright — folds the
# "5+ yrs -> cap 4 / 6+ yrs -> cap 0+skip" seniority overrides into a single
# hard pre-screen instead of relying on the LLM judge to apply the cap.
# The optional "-M"/"–M" group handles ranges ("3-6 years of experience")
# by capturing the LOWER bound (the actual minimum) instead of matching the
# upper bound as its own standalone number — without it, "3-6 years" would
# wrongly read as a flat 6-year requirement.
EXPERIENCE_YEARS_RE = re.compile(
    r"(\d+)\s*(?:\+|[-–—]\s*\d+\+?)?\s*years?\s+(?:of\s+)?(?:[a-z][\w/&,-]*\s+){0,4}experience",
)


def max_years_required(jd_text: Any) -> int:
    """Return the highest N found in an explicit "N+ years ... experience"
    phrase in jd_text (0 if none found)."""
    normalized = _normalize_jd(jd_text)
    if not normalized:
        return 0
    return max((int(n) for n in EXPERIENCE_YEARS_RE.findall(normalized)), default=0)


def disqualify_reason(
    vertical: verticals.Vertical, jd_text: Any, title: Any = None
) -> str | None:
    """Return 'title', 'phrase' or 'years' if the row trips one of the
    vertical's configured disqualifier checks (in that priority order),
    else None. `title_phrases` match the job title, `phrases` and
    `max_years` match jd_text; all come from the vertical's `disqualifier`
    block in profile/verticals.yaml. All lists are optional —
    a vertical with empty lists gets the years check only.

    Semantic degree-requirement cases (e.g. a closed quant-only degree list
    with no CS path) deliberately do NOT live here — distinguishing one from
    an ordinary "or related field" listing is a judgment call a keyword
    regex over-triggers on (tested against a full 14-day vertical window:
    flagged ~70 generic business-degree JDs alongside the few genuine
    closed-list ones). Those cases are hard rubric rules for the LLM judge
    in the vertical's rubric.md instead (locked 2026-06-17)."""
    if isinstance(title, str) and title:
        title_lowered = title.lower()
        if any(p in title_lowered for p in vertical.disqualifier_title_phrases):
            return "title"
    if not isinstance(jd_text, str) or not jd_text:
        return None
    lowered = jd_text.lower()
    if any(phrase in lowered for phrase in vertical.disqualifier_phrases):
        return "phrase"
    if max_years_required(jd_text) > vertical.disqualifier_max_years:
        return "years"
    return None


# =====================================================================
# Internal helpers
# =====================================================================

def _empty_scored() -> pd.DataFrame:
    df = pd.DataFrame({c: pd.Series(dtype="object") for c in SCORED_COLUMNS})
    df["fit_score"] = df["fit_score"].astype("float64")
    df["shortlist_rank"] = df["shortlist_rank"].astype("float64")
    df["scored_at"] = pd.to_datetime(df["scored_at"])
    return df


def _read_scored(scored_path: Path) -> pd.DataFrame:
    if not scored_path.exists():
        return _empty_scored()
    df = pd.read_parquet(scored_path)
    for col in SCORED_COLUMNS:
        if col not in df.columns:
            df[col] = pd.Series(dtype="object")
    return df[SCORED_COLUMNS]


def _to_jsonable(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (str, bool, int)):
        return v
    if isinstance(v, float):
        return None if pd.isna(v) else v
    if isinstance(v, pd.Timestamp):
        return None if pd.isna(v) else v.isoformat()
    if isinstance(v, dict):
        return {k: _to_jsonable(vv) for k, vv in v.items()}
    # list / tuple / numpy.ndarray etc. (numpy arrays come out of pyarrow-
    # backed parquet for list-typed columns; do NOT fall through to str(v),
    # which would render them as the array's repr and break consumers).
    if isinstance(v, (list, tuple)) or (hasattr(v, "__iter__")
                                         and not isinstance(v, (str, bytes))):
        return [_to_jsonable(x) for x in v]
    return str(v)


# =====================================================================
# Selection / dump
# =====================================================================

def select_unscored(clean_path: Path, scored_path: Path) -> pd.DataFrame:
    """Rows from clean.parquet whose job_id is not in scored.parquet.
    Returns all of clean if scored.parquet does not exist."""
    clean = pd.read_parquet(clean_path)
    if not scored_path.exists():
        return clean.copy()
    scored = pd.read_parquet(scored_path)
    scored_ids = set(scored["job_id"].astype(str))
    return clean[~clean["job_id"].astype(str).isin(scored_ids)].copy()


def dump_unscored(
    clean_path: Path,
    scored_path: Path,
    out_path: Path,
    *,
    force_all: bool = False,
    only_vertical: str | None = None,
    hard_ineligible: tuple[str, ...] | None = None,
) -> int:
    """Write a JSONL of in-lane rows the LLM needs to judge. Returns the
    count of LLM-judge rows (those written to out_path).

    Gates purely on the precomputed clean.parquet `vertical` column — set by
    discovery.py (scraped rows, tagged by search term) / cleaning.py (manual
    clips + legacy-row title fallback). This function does NOT reclassify
    titles itself.

    Side effects (the skill ALWAYS processes every one of these — a file
    left unprocessed strands its rows unscored):
    - vertical="" rows (no configured vertical) ->
      `out_path.parent / 'auto_skip.jsonl'`, merged by
      `auto_score_out_of_lane`.
    - in-lane rows whose jd_text contains a `hard_ineligible` phrase
      (checked BEFORE the vertical disqualifiers so a clearance-walled row
      is recorded as ineligible, not as a lane mismatch) ->
      `out_path.parent / 'auto_skip_ineligible.jsonl'` (always written even
      if empty), merged by `auto_score_ineligible`. `hard_ineligible=None`
      loads the list from SPONSORSHIP_RULES_PATH; tests inject a tuple.
    - rows tripping their vertical's configured disqualifier (a title
      phrase, a jd_text phrase, or an explicit years requirement — see
      `disqualify_reason`)
      -> `out_path.parent / 'auto_skip_<vertical>.jsonl'` (one file per
      configured vertical, always written even if empty), merged by
      `auto_score_disqualified`.
    - everything else -> `out_path` for LLM judging, carrying its `vertical`
      field through so the judge applies the matching rubric file.

    only_vertical=<name> restricts ALL of the above to rows of that vertical
    — rows of every other vertical (including vertical="" out-of-lane rows,
    which match no name) are left untouched this run: not judged, not
    auto-skipped. Their existing scored.parquet rows (if any) carry forward
    unchanged; brand-new rows of the excluded verticals stay unscored until
    a future run without this filter.

    force_all=True ignores scored.parquet (used by /rescore)."""
    cfg = verticals.get_config()
    if hard_ineligible is None:
        hard_ineligible = load_hard_ineligible()
    if force_all or not scored_path.exists():
        df = pd.read_parquet(clean_path)
    else:
        df = select_unscored(clean_path, scored_path)
    if only_vertical is not None:
        df = df[df["vertical"] == only_vertical]
    available = [c for c in UNSCORED_FIELDS if c in df.columns]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    auto_skip_path = out_path.parent / "auto_skip.jsonl"
    n_judge = 0
    n_skip = 0
    n_ineligible = 0
    n_skip_by_vertical = dict.fromkeys(cfg.names, 0)
    with contextlib.ExitStack() as stack:
        f_judge = stack.enter_context(out_path.open("w", encoding="utf-8"))
        f_skip = stack.enter_context(auto_skip_path.open("w", encoding="utf-8"))
        f_inel = stack.enter_context(
            (out_path.parent / "auto_skip_ineligible.jsonl").open("w", encoding="utf-8")
        )
        skip_files = {
            name: stack.enter_context(
                (out_path.parent / f"auto_skip_{name}.jsonl").open("w", encoding="utf-8")
            )
            for name in cfg.names
        }
        for _, row in df[available].iterrows():
            d = {k: _to_jsonable(v) for k, v in row.to_dict().items()}
            vertical = d.get("vertical") or ""
            if vertical not in cfg.verticals:
                f_skip.write(json.dumps(d, default=str) + "\n")
                n_skip += 1
                continue
            matched = hard_ineligible_phrase(hard_ineligible, d.get("jd_text", ""))
            if matched is not None:
                d["_ineligible_phrase"] = matched
                f_inel.write(json.dumps(d, default=str) + "\n")
                n_ineligible += 1
                continue
            reason = disqualify_reason(
                cfg.verticals[vertical], d.get("jd_text", ""), d.get("title", "")
            )
            if reason is not None:
                d["_disqualify_reason"] = reason
                skip_files[vertical].write(json.dumps(d, default=str) + "\n")
                n_skip_by_vertical[vertical] += 1
            else:
                f_judge.write(json.dumps(d, default=str) + "\n")
                n_judge += 1
    log.info(
        "dump_unscored: to_judge=%d auto_skip=%d auto_ineligible=%d %s",
        n_judge, n_skip, n_ineligible,
        " ".join(f"auto_skip_{v}={n}" for v, n in n_skip_by_vertical.items()),
    )
    return n_judge


def auto_score_ineligible(
    skip_path: Path,
    scored_path: Path,
    scored_at: datetime | None = None,
) -> int:
    """Read auto_skip_ineligible.jsonl (rows whose jd_text matched a
    hard_ineligible phrase in dump_unscored) and merge them into
    scored.parquet as pre-labeled ineligible records: fit_score=0 (never
    ranked), sponsorship_evidence =
    the matched phrase, shortlist-exclusion routing via the label exactly as
    a judge-assigned ineligible. No LLM call (carve-out). Returns
    count of merged rows. No-op when the file is missing or empty."""
    if not skip_path.exists():
        return 0
    new_scores: list[dict] = []
    with skip_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            new_scores.append({
                "job_id": row["job_id"],
                "fit_subscores": {"title": 0, "skills": 0, "seniority": 0, "domain": 0},
                "vertical": row.get("vertical", ""),
                "sponsorship_label": "ineligible",
                "sponsorship_evidence": row.get("_ineligible_phrase", ""),
                "reasoning": HARD_INELIGIBLE_REASONING,
                "keywords_to_mirror": [],
                "suggested_action": "skip",
            })
    if not new_scores:
        return 0
    return merge_scores(
        scored_path, new_scores,
        scored_by_model=HARD_INELIGIBLE_SCORED_BY,
        scored_at=scored_at,
    )


def auto_score_out_of_lane(
    auto_skip_path: Path,
    scored_path: Path,
    scored_at: datetime | None = None,
) -> int:
    """Read auto_skip.jsonl (out-of-lane title rows from dump_unscored) and
    merge them into scored.parquet as pre-filled skip records. No LLM call.
    Stamps scored_by_model='rubric:title-out-of-lane' for auditability; the
    reasoning text comes from profile/verticals.yaml `out_of_lane.reasoning`.
    Returns count of merged rows. No-op when the file is missing or empty."""
    if not auto_skip_path.exists():
        return 0
    reasoning = verticals.get_config().out_of_lane_reasoning
    new_scores: list[dict] = []
    with auto_skip_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            new_scores.append({
                "job_id": row["job_id"],
                "fit_subscores": {"title": 0, "skills": 0, "seniority": 0, "domain": 0},
                "vertical": "",
                "sponsorship_label": "unknown",
                "sponsorship_evidence": "",
                "reasoning": reasoning,
                "keywords_to_mirror": [],
                "suggested_action": "skip",
            })
    if not new_scores:
        return 0
    return merge_scores(
        scored_path, new_scores,
        scored_by_model=AUTO_SKIP_SCORED_BY,
        scored_at=scored_at,
    )


def auto_score_disqualified(
    vertical: verticals.Vertical,
    skip_path: Path,
    scored_path: Path,
    scored_at: datetime | None = None,
) -> int:
    """Read auto_skip_<vertical>.jsonl (rows whose jd_text tripped the
    vertical's configured disqualifier in `disqualify_reason`, from
    dump_unscored) and merge them into scored.parquet as pre-filled skip
    records, using the vertical's configured reasoning text that matches
    each row's `_disqualify_reason` category. No LLM call. Stamps the
    vertical's configured `scored_by` for auditability. Returns count of
    merged rows. No-op when the file is missing or empty."""
    if not skip_path.exists():
        return 0
    default_reasoning = vertical.reasoning_phrase or vertical.reasoning_years
    reasoning_by_category = {
        "title": vertical.reasoning_title,
        "phrase": vertical.reasoning_phrase,
        "years": vertical.reasoning_years,
    }
    new_scores: list[dict] = []
    with skip_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            reasoning = reasoning_by_category.get(
                row.get("_disqualify_reason", "phrase")
            ) or default_reasoning
            new_scores.append({
                "job_id": row["job_id"],
                "fit_subscores": {"title": 0, "skills": 0, "seniority": 0, "domain": 0},
                "vertical": vertical.name,
                "sponsorship_label": "unknown",
                "sponsorship_evidence": "",
                "reasoning": reasoning,
                "keywords_to_mirror": [],
                "suggested_action": "skip",
            })
    if not new_scores:
        return 0
    return merge_scores(
        scored_path, new_scores,
        scored_by_model=vertical.disqualifier_scored_by,
        scored_at=scored_at,
    )


# =====================================================================
# Validation
# =====================================================================

def fit_score_from_subscores(subs: dict) -> int:
    """The single source of truth for a row's total. Judges emit only the four
    per-axis subscores; the total is derived here so a judge can never anchor
    on a preordained number and back-fill axes to justify it, and so
    `sum != fit_score` is unrepresentable rather than merely validated.
    Axis maxima sum to 100, so the result is always in [0,100]."""
    return sum(int(subs[a]) for a in SUBSCORE_AXES)


def validate_scores(scores: list[dict]) -> list[str]:
    """Return a list of error strings (one per problem). Empty = all valid."""
    errors: list[str] = []
    for i, s in enumerate(scores):
        if not isinstance(s, dict):
            errors.append(f"row {i}: expected an object, got {type(s).__name__}")
            continue
        # _source is stamped by merge_scores_from_dir so the operator can find
        # the offending row without grepping every batch file.
        where = s.get("_source", f"row {i}")
        prefix = f"{where} (job_id={s.get('job_id', '?')!r})"
        # Required fields first; if missing, skip further checks for this row.
        # fit_score is NOT required — it is derived from fit_subscores by
        # fit_score_from_subscores, so judges never author a total.
        missing = [f for f in (
            "job_id", "fit_subscores", "vertical",
            "sponsorship_label", "sponsorship_evidence",
            "reasoning", "keywords_to_mirror", "suggested_action",
        ) if f not in s]
        if missing:
            for f in missing:
                errors.append(f"{prefix}: missing required field {f!r}")
            continue
        if not isinstance(s["job_id"], str) or len(s["job_id"]) != 8:
            errors.append(f"{prefix}: job_id must be 8-char hex string")
        valid_verticals = verticals.get_config().valid_verticals
        if s["vertical"] not in valid_verticals:
            errors.append(
                f"{prefix}: vertical {s['vertical']!r} not in {sorted(valid_verticals)}"
            )
        if s["sponsorship_label"] not in VALID_LABELS:
            errors.append(
                f"{prefix}: sponsorship_label {s['sponsorship_label']!r} "
                f"not in {sorted(VALID_LABELS)}"
            )
        if s["suggested_action"] not in VALID_ACTIONS:
            errors.append(
                f"{prefix}: suggested_action {s['suggested_action']!r} "
                f"not in {sorted(VALID_ACTIONS)}"
            )
        if not isinstance(s["keywords_to_mirror"], list):
            errors.append(f"{prefix}: keywords_to_mirror must be a list")
        subs = s["fit_subscores"]
        if not isinstance(subs, dict):
            errors.append(f"{prefix}: fit_subscores must be a dict")
        else:
            axes_ok = True
            for axis in SUBSCORE_AXES:
                if axis not in subs:
                    errors.append(f"{prefix}: fit_subscores missing axis {axis!r}")
                    axes_ok = False
                elif not isinstance(subs[axis], int):
                    errors.append(
                        f"{prefix}: fit_subscores[{axis!r}] must be int, "
                        f"got {type(subs[axis]).__name__}"
                    )
                    axes_ok = False
                elif subs[axis] < 0 or subs[axis] > AXIS_MAXIMA[axis]:
                    errors.append(
                        f"{prefix}: fit_subscores[{axis!r}]={subs[axis]} "
                        f"out of range [0,{AXIS_MAXIMA[axis]}]"
                    )
                    axes_ok = False
            # A judge should omit fit_score entirely. If one emits it anyway
            # that is prompt drift, so require it to agree rather than
            # silently overriding it — the disagreement is the signal.
            if axes_ok and "fit_score" in s:
                total = sum(subs[a] for a in SUBSCORE_AXES)
                if total != s["fit_score"]:
                    errors.append(
                        f"{prefix}: fit_score {s['fit_score']} disagrees with "
                        f"fit_subscores sum {total} — omit fit_score, it is derived"
                    )
        if s["sponsorship_label"] == "ineligible":
            ev = s["sponsorship_evidence"]
            if not isinstance(ev, str) or not ev.strip():
                errors.append(
                    f"{prefix}: ineligible label requires non-empty sponsorship_evidence "
                    "(quoted JD phrase)"
                )
    return errors


# =====================================================================
# Merge / prune
# =====================================================================

def merge_scores(
    scored_path: Path,
    new_scores: list[dict],
    scored_by_model: str,
    scored_at: datetime | None = None,
) -> int:
    """Merge new_scores into scored.parquet. New rows OVERWRITE existing rows
    on job_id collision (so /rescore can replace prior scores). Stamps
    scored_by_model and scored_at on the new rows; preserves the prior values
    on rows kept from the existing parquet. Returns count of new/updated rows.

    Raises ValueError if validate_scores returns any errors — fail loud."""
    if scored_at is None:
        scored_at = datetime.now()
    errors = validate_scores(new_scores)
    if errors:
        raise ValueError(
            f"Cannot merge {len(errors)} invalid score(s):\n  "
            + "\n  ".join(errors)
        )
    existing = _read_scored(scored_path)
    new_ids = {s["job_id"] for s in new_scores}
    kept = existing[~existing["job_id"].astype(str).isin(new_ids)].copy()
    new_rows = [{
        "job_id": s["job_id"],
        "fit_score": float(fit_score_from_subscores(s["fit_subscores"])),
        "fit_subscores": json.dumps(s["fit_subscores"]),
        "vertical": s["vertical"],
        "sponsorship_label": s["sponsorship_label"],
        "sponsorship_evidence": s["sponsorship_evidence"],
        "reasoning": s["reasoning"],
        "keywords_to_mirror": list(s["keywords_to_mirror"]),
        "suggested_action": s["suggested_action"],
        "shortlist_rank": float("nan"),
        "scored_at": pd.Timestamp(scored_at),
        "scored_by_model": scored_by_model,
    } for s in new_scores]
    new_df = pd.DataFrame(new_rows, columns=SCORED_COLUMNS)
    combined = (
        pd.concat([kept, new_df], ignore_index=True)
        if not kept.empty else new_df
    )
    scored_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(scored_path, index=False)
    return len(new_rows)


def merge_scores_from_dir(
    scored_path: Path,
    staging_dir: Path,
    scored_by_model: str,
    scored_at: datetime | None = None,
) -> tuple[int, list[Path]]:
    """Read all batch_*.json files from staging_dir as JSON arrays of score
    dicts, then call merge_scores. Returns (count of merged rows, list of
    batch files that could not be read).

    Unreadable batches are skipped, never merged — but they are also RETURNED,
    because each one holds ~100 judged rows. The caller must refuse to clear
    staging while that list is non-empty, or those rows are destroyed while a
    healthy `merged=` count prints. /score writes batches to scored.staging/
    so partial-batch failures survive for debugging (user Q5 call: not /tmp).

    validate_scores errors name their source file, so the operator can repair
    the named row in the named batch and re-run merge (idempotent: rows
    overwrite by job_id)."""
    if not staging_dir.exists():
        return 0, []
    all_scores: list[dict] = []
    skipped: list[Path] = []
    for f in sorted(staging_dir.glob("batch_*.json")):
        try:
            batch = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            log.error("Skipping malformed %s: %s", f, e)
            skipped.append(f)
            continue
        if not isinstance(batch, list):
            log.error("Skipping %s: expected JSON array, got %s", f, type(batch).__name__)
            skipped.append(f)
            continue
        for i, s in enumerate(batch):
            if isinstance(s, dict):
                s.setdefault("_source", f"{f.name}[{i}]")
        all_scores.extend(batch)
    if not all_scores:
        return 0, skipped
    return merge_scores(scored_path, all_scores, scored_by_model, scored_at), skipped


def prune_scored(scored_path: Path, clean_path: Path) -> int:
    """Drop scored rows whose job_id is no longer in clean.parquet.
    Returns count dropped."""
    if not scored_path.exists():
        return 0
    scored = pd.read_parquet(scored_path)
    clean = pd.read_parquet(clean_path)
    clean_ids = set(clean["job_id"].astype(str))
    keep_mask = scored["job_id"].astype(str).isin(clean_ids)
    dropped = int((~keep_mask).sum())
    if dropped:
        scored[keep_mask].to_parquet(scored_path, index=False)
    return dropped


# =====================================================================
# Shortlist computation (deterministic — sort / cap / exclusion split)
# =====================================================================

def _count_skips(state_history: Any) -> int:
    if not isinstance(state_history, list):
        return 0
    return sum(
        1 for h in state_history
        if isinstance(h, dict) and h.get("state") == "skip"
    )


def _load_state_metadata(pipeline_dir: Path) -> dict[str, dict]:
    """{job_id: {state, skip_count}} from pipeline/*/state.yaml."""
    out: dict[str, dict] = {}
    if not pipeline_dir.exists():
        return out
    for f in pipeline_dir.glob("*/state.yaml"):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError) as e:
            log.warning("Skipping unreadable %s: %s", f, e)
            continue
        jid = data.get("job_id")
        if not isinstance(jid, str):
            continue
        out[jid] = {
            "state": data.get("state", ""),
            "skip_count": _count_skips(data.get("state_history")),
        }
    return out


def compute_shortlist(
    scored_path: Path,
    clean_path: Path,
    pipeline_dir: Path,
    top_n: int = 25,
    min_fit: int = 50,
    write_ranks: bool = True,
) -> dict:
    """Deterministic sort + cap + exclusion split.

    Returns:
      {
        "main": {<vertical>: [row, ...] for each configured vertical,
                 in profile/verticals.yaml config order}
            # top_n per vertical, each section sorted/capped independently;
            # excludes ineligible/suppressed (locked 2026-06-16:
            # independently-ranked sections, not one blended list — a
            # vertical's fit_score is never compared against another's)
        "excluded":   [row, ...]   # sponsorship_label == 'ineligible' (sponsorship is the only exclusion gate)
        "suppressed": [row, ...]   # state_history skip count >= 1 (a skip is not an ineligible)
      }

    Sort key within each vertical's section (locked):
      fit_score DESC,
      sponsorship_pref DESC (sponsors > opt_ok > unknown),
      posted_date DESC.

    Side effect (write_ranks=True): writes shortlist_rank back to scored.parquet
    (1..N within each vertical's section for main rows, NaN otherwise)."""
    cfg = verticals.get_config()
    if not scored_path.exists():
        return {"main": {v: [] for v in cfg.names}, "excluded": [], "suppressed": []}
    scored = pd.read_parquet(scored_path)
    clean = pd.read_parquet(clean_path)
    state_meta = _load_state_metadata(pipeline_dir)

    df = scored.merge(clean, on="job_id", how="left", suffixes=("", "_clean"))
    df["skip_count"] = df["job_id"].map(
        lambda j: state_meta.get(j, {}).get("skip_count", 0)
    )
    df["current_state"] = df["job_id"].map(
        lambda j: state_meta.get(j, {}).get("state", "")
    )
    df["sponsorship_pref"] = (
        df["sponsorship_label"].map(SPONSORSHIP_PREF).fillna(0).astype(int)
    )
    excluded = df[df["sponsorship_label"] == "ineligible"].copy()
    suppressed = df[df["skip_count"] >= 1].copy()

    main_pool = df[
        (df["sponsorship_label"] != "ineligible")
        & (df["skip_count"] < 1)
        & (df["fit_score"] >= min_fit)
        & (~df["current_state"].isin(_APPLIED_STATES))
    ].copy()

    new_ranks: dict[str, int] = {}
    main: dict[str, list[dict]] = {}
    for vertical in cfg.names:
        section = main_pool[main_pool["vertical"] == vertical].sort_values(
            by=["fit_score", "sponsorship_pref", "posted_date"],
            ascending=[False, False, False],
            kind="stable",
        ).head(top_n)
        for rank, jid in enumerate(section["job_id"]):
            new_ranks[jid] = rank + 1
        main[vertical] = [{k: _to_jsonable(v) for k, v in r.items()} for r in section.to_dict("records")]

    if write_ranks:
        scored2 = scored.copy()
        scored2["shortlist_rank"] = (
            scored2["job_id"].map(new_ranks).astype("float64")
        )
        scored2.to_parquet(scored_path, index=False)

    return {
        "main":       main,
        "excluded":   [{k: _to_jsonable(v) for k, v in r.items()} for r in excluded.to_dict("records")],
        "suppressed": [{k: _to_jsonable(v) for k, v in r.items()} for r in suppressed.to_dict("records")],
    }


def render_shortlist_markdown(
    shortlist: dict, cfg: "verticals.VerticalsConfig",
    date_str: str, n_scored: int, n_clean: int,
) -> str:
    """Render shortlist/<date>.md from compute_shortlist()'s output.
    Asserts invariants inline (fit>=50, subscore sum, no ineligible/skip
    leakage, no cross-vertical leakage) — raises AssertionError, never
    silently drops a row."""
    def subscores(row):
        s = row["fit_subscores"]
        return json.loads(s) if isinstance(s, str) else s

    sections = []
    total_keepers = 0
    for v in cfg.names:
        rows = shortlist["main"].get(v, [])
        assert len(rows) <= 25, f"{v}: {len(rows)} rows exceeds cap 25"
        lines = [f"## {cfg.verticals[v].display_name} ({len(rows)})", ""]
        if not rows:
            lines.append("No keepers today in this vertical.")
        for i, row in enumerate(rows, 1):
            assert row["vertical"] == v, f"{row['job_id']} leaked into {v} section"
            assert row["fit_score"] >= 50, f"{row['job_id']}: fit {row['fit_score']} < 50"
            sub = subscores(row)
            assert sum(sub[a] for a in SUBSCORE_AXES) == row["fit_score"], \
                f"{row['job_id']}: subscores {sub} != fit_score {row['fit_score']}"
            assert row["sponsorship_label"] != "ineligible", f"{row['job_id']}: ineligible in main"
            status = row["application_status"] if row.get("already_seen") else "new"
            kws = ", ".join(row.get("keywords_to_mirror", [])[:3])
            lines.append(
                f"### {i}. {row['fit_score']} — {row['company']} — {row['title']}\n"
                f"- **job_id:** `{row['job_id']}`\n"
                f"- **location:** {row['location']} · **source:** {row['source']} "
                f"· **posted:** {row['posted_date']}\n"
                f"- **fit:** {row['fit_score']} (title {sub['title']} / skills {sub['skills']} "
                f"/ seniority {sub['seniority']} / domain {sub['domain']})\n"
                f"- **sponsorship:** {row['sponsorship_label']} — \"{row['sponsorship_evidence']}\"\n"
                f"- **why:** {row['reasoning']}\n"
                f"- **mirror in tailoring:** {kws}\n"
                f"- **status:** {status}\n"
                f"- **suggested:** {row['suggested_action']}\n"
                f"- **verify E-Verify** before submitting (manual v1 step)\n"
                f"- {row['url']}\n"
            )
        sections.append("\n".join(lines))
        total_keepers += len(rows)

    header = (f"# Shortlist — {date_str}\n\n"
              f"({n_scored} of {n_clean} scored, top 25 per vertical with fit >= 50)\n")
    if total_keepers == 0:
        return header + "\nNo keepers today in this vertical.\n"
    return header + "\n" + "\n".join(sections)


