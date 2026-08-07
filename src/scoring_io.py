"""Parquet plumbing for /score and /rescore: read, dump, merge, prune.

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
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src import verticals
from src.parquet_io import write_parquet
from src.prescreen import (
    AUTO_SKIP_SCORED_BY,
    HARD_INELIGIBLE_REASONING,
    HARD_INELIGIBLE_SCORED_BY,
    disqualify_reason,
    hard_ineligible_phrase,
    load_hard_ineligible,
)

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


def to_jsonable(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (str, bool, int)):
        return v
    if isinstance(v, float):
        return None if pd.isna(v) else v
    if isinstance(v, pd.Timestamp):
        return None if pd.isna(v) else v.isoformat()
    if isinstance(v, dict):
        return {k: to_jsonable(vv) for k, vv in v.items()}
    # list / tuple / numpy.ndarray etc. (numpy arrays come out of pyarrow-
    # backed parquet for list-typed columns; do NOT fall through to str(v),
    # which would render them as the array's repr and break consumers).
    if isinstance(v, (list, tuple)) or (hasattr(v, "__iter__")
                                         and not isinstance(v, (str, bytes))):
        return [to_jsonable(x) for x in v]
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
    only_job_id: str | None = None,
    skip_prescreens: bool = False,
    hard_ineligible: tuple[str, ...] | None = None,
) -> int:
    """Write a JSONL of in-lane rows the LLM needs to judge. Returns the
    count of LLM-judge rows (those written to out_path).

    Gates purely on the precomputed clean.parquet `vertical` column — set by
    sources/ (scraped rows, tagged by search term) / cleaning.py (manual
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

    only_job_id=<id> restricts to that one row (used by /ingest). Composes
    with only_vertical; an id absent from the selection yields 0.

    skip_prescreens=True suppresses the hard_ineligible and disqualifier
    branches so a deliberately ingested row reaches a judge and gets real
    subscores + keywords_to_mirror instead of a pre-filled skip. The
    out-of-lane branch still applies: split_by_vertical raises on a vertical
    it does not know, so an unconfigured row must never reach out_path.

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
    if only_job_id is not None:
        df = df[df["job_id"].astype(str) == only_job_id]
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
            d = {k: to_jsonable(v) for k, v in row.to_dict().items()}
            vertical = d.get("vertical") or ""
            if vertical not in cfg.verticals:
                f_skip.write(json.dumps(d, default=str) + "\n")
                n_skip += 1
                continue
            if not skip_prescreens:
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
                    continue
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
    # Two records for one job_id put two rows in scored.parquet, after which
    # scored.loc[job_id] returns a DataFrame: track_cli raises "truth value of
    # a Series is ambiguous", tailor_cli falls through to default_vertical, and
    # the row eats two shortlist slots. The interrupted-run recovery merge has
    # no coverage check at all, which is exactly when two judges overlap.
    seen: dict[str, str] = {}
    for i, s in enumerate(scores):
        if not isinstance(s, dict) or not isinstance(s.get("job_id"), str):
            continue
        where = s.get("_source", f"row {i}")
        if s["job_id"] in seen:
            errors.append(
                f"{where} (job_id={s['job_id']!r}): duplicate job_id, "
                f"already scored by {seen[s['job_id']]}"
            )
        else:
            seen[s["job_id"]] = where
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
    # validate_scores rejects duplicates in the incoming batch, so anything
    # caught here was already in scored.parquet — a file left corrupt by an
    # interrupted run. Repair it, but say so; silently dropping a row would
    # hide the corruption.
    dupes = combined["job_id"][combined["job_id"].duplicated()].unique()
    if len(dupes):
        log.warning("scored.parquet held duplicate job_ids, keeping the newest "
                    "of each: %s", ", ".join(map(str, dupes)))
        combined = combined.drop_duplicates(subset="job_id", keep="last")
    scored_path.parent.mkdir(parents=True, exist_ok=True)
    write_parquet(combined, scored_path)
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
    so partial-batch failures survive for debugging.

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
        write_parquet(scored[keep_mask], scored_path)
    return dropped

