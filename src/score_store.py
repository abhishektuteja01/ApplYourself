"""Steps 1 and 3 of /score: the files around the judges.

`start` picks the rows, drops what the hard rules reject, and writes the
judges' inputs into jobs/scored.staging/. `finish` checks each judge's output
against its slice, derives everything mechanical, and merges into
scored.parquet. No LLM (R7): the judging itself is the score-judge agent.

Staging holds only:
  run.json                 the open run: model stamp and every pending slice
  packet_<lane>.md         what every judge of that lane reads first
  slice_<lane>_NNN.md      up to SLICE_ROWS roles for one judge pass
  out_<lane>_NNN.json      the judge's answer for that slice
  rejected_<lane>_NNN.json an answer finish refused, kept until the run closes
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from src.score_filter import (
    SPONSORSHIP_FACTS,
    CountryRule,
    ScoringConfig,
    country_allowed,
    drop_reason,
    normalize,
    seniority_cap,
    sponsorship_label,
    trim_jd,
    word_count,
    wrap,
    years_required,
)
from src.shared import paths, verticals
from src.shared.parquet_io import locked, write_parquet
from src.state_io import ACTIVE_STATES, load_state_index

SCORED_COLUMNS: list[str] = [
    "job_id",
    "fit_score",  # sum of fit_subscores, 0-100
    "fit_subscores",  # JSON string: {title, skills, seniority, domain}
    "vertical",  # a profile/verticals.yaml name | "" (no lane)
    "sponsorship_label",
    "sponsorship_evidence",
    "reasoning",
    "keywords_to_mirror",  # list[str]
    "suggested_action",
    "scored_at",
    "scored_by_model",
]
AXIS_MAXIMA = {"title": 30, "skills": 30, "seniority": 20, "domain": 20}
ACTION_RANK = {"skip": 0, "manual-review": 1, "tailor": 2}

SLICE_ROWS = 10
SLICE_BYTES = 60_000
JUDGE_SLICES = 5
JUDGE_BYTES = 300_000

JUDGE_AGENT = paths.REPO_ROOT / ".claude" / "agents" / "score-judge.md"
SHARED_RUBRIC = paths.REPO_ROOT / ".claude" / "shared" / "scoring_rubric.md"


class RunOpen(RuntimeError):
    """A run is already staged; finish it before starting another kind."""


# ---------------------------------------------------------------------
# scored.parquet
# ---------------------------------------------------------------------


def read_scored(scored_path: Path) -> pd.DataFrame:
    if not scored_path.exists():
        return pd.DataFrame({c: pd.Series(dtype="object") for c in SCORED_COLUMNS})
    df = pd.read_parquet(scored_path)
    for col in SCORED_COLUMNS:
        if col not in df.columns:
            df[col] = pd.Series(dtype="object")
    return df[SCORED_COLUMNS]


def merge_rows(scored_path: Path, rows: list[dict]) -> int:
    """Write rows into scored.parquet, replacing any row with the same job_id."""
    if not rows:
        return 0
    new = pd.DataFrame(rows, columns=SCORED_COLUMNS)
    new["fit_score"] = new["fit_score"].astype("float64")
    new["scored_at"] = pd.to_datetime(new["scored_at"])
    with locked(scored_path):
        old = read_scored(scored_path)
        old = old[~old["job_id"].isin(set(new["job_id"]))]
        out = pd.concat([old, new], ignore_index=True) if len(old) else new
        write_parquet(out.drop_duplicates("job_id", keep="last"), scored_path)
    return len(rows)


def prune(scored_path: Path, clean_path: Path, pipeline_dir: Path) -> int:
    """Drop scored rows neither in clean.parquet nor in an active state.yaml."""
    if not scored_path.exists():
        return 0
    keep = set(pd.read_parquet(clean_path, columns=["job_id"])["job_id"])
    keep |= {
        j for j, d in load_state_index(pipeline_dir).items() if d.get("state") in ACTIVE_STATES
    }
    with locked(scored_path):
        scored = pd.read_parquet(scored_path)
        mask = scored["job_id"].isin(keep)
        if (~mask).any():
            write_parquet(scored[mask], scored_path)
    return int((~mask).sum())


def _row(
    job_id: str,
    vertical: str,
    subs: dict,
    label: str,
    evidence: str,
    reasoning: str,
    keywords: list,
    action: str,
    model: str,
    now: datetime,
) -> dict:
    return {
        "job_id": job_id,
        "fit_score": float(sum(subs.values())),
        "fit_subscores": json.dumps(subs),
        "vertical": vertical,
        "sponsorship_label": label,
        "sponsorship_evidence": evidence,
        "reasoning": reasoning,
        "keywords_to_mirror": list(keywords),
        "suggested_action": action,
        "scored_at": pd.Timestamp(now),
        "scored_by_model": model,
    }


def _zero() -> dict:
    return dict.fromkeys(AXIS_MAXIMA, 0)


def drop_row(row: dict, code: str, detail: str, now: datetime) -> dict:
    ineligible = code == "ineligible"
    return _row(
        row["job_id"],
        row.get("vertical") or "",
        _zero(),
        "ineligible" if ineligible else "unknown",
        detail if ineligible else "",
        f"Filtered ({code}): {detail}",
        [],
        "skip",
        f"filter:{code}",
        now,
    )


# ---------------------------------------------------------------------
# start
# ---------------------------------------------------------------------


def _judge_model() -> str:
    text = JUDGE_AGENT.read_text(encoding="utf-8")
    meta = yaml.safe_load(text.split("---")[1]) or {}
    return f"score-judge:{meta.get('model', 'unknown')}"


def _lane_resume(lane: verticals.Vertical) -> str:
    """The lane's baseline resume, built from the profile sources by the same
    assembler /tailor uses."""
    from src import resume_model

    if lane.resume is None:
        raise ValueError(f"lane {lane.name} has no resume: block in profile/verticals.yaml")
    return resume_model.baseline_markdown(lane.name)


def write_packet(staging: Path, lane: verticals.Vertical, cfg: ScoringConfig) -> None:
    parts = [
        f"# Scoring packet — lane {lane.name}\n",
        f"`keywords_to_mirror`: up to {cfg.keywords_max}.\n",
        f"Languages the candidate speaks: {', '.join(cfg.languages)}.\n" if cfg.languages else "",
        SHARED_RUBRIC.read_text(encoding="utf-8"),
        (verticals.VERTICALS_DIR / lane.name / "rubric.md").read_text(encoding="utf-8"),
        "## Skill weights\n\n```yaml\n"
        + yaml.safe_dump(lane.skill_weights, sort_keys=False).strip()
        + "\n```\n",
        "## Resume\n\n" + _lane_resume(lane),
    ]
    if lane.focus:
        f = lane.focus
        parts.insert(
            4,
            "## Focus\n\n"
            f"Add `focus`: true or false, and `focus_evidence`. {f.question}\n\n"
            "Default false. Answer true only when the JD itself shows it, and put "
            'that JD phrase, verbatim, in `focus_evidence`; leave it "" when false. '
            "A strong fit is not a reason for true.\n\n"
            f"Then `domain` is set for you: true lifts it to at least {f.yes_min}, "
            f"false holds it at {f.no_max} or below.\n",
        )
    (staging / f"packet_{lane.name}.md").write_text("\n".join(parts), encoding="utf-8")


def _slice_text(name: str, rows: list[dict], staging: Path) -> str:
    lane = rows[0]["vertical"]
    out = [
        f"# {name} — {len(rows)} role(s)\n",
        f"- packet: {staging / f'packet_{lane}.md'}",
        f"- write your answer to: {staging / f'out_{name}.json'}",
        f"- job_ids, exactly these: {', '.join(r['job_id'] for r in rows)}\n",
    ]
    for i, r in enumerate(rows, 1):
        salary = " - ".join(
            str(int(v))
            for v in (r.get("salary_min"), r.get("salary_max"))
            if isinstance(v, (int, float)) and v == v
        )
        facts = [
            ("title", r.get("title")),
            ("company", r.get("company")),
            ("location", r.get("location")),
            ("employment type", r.get("employment_type")),
            ("seniority as posted", r.get("seniority_raw")),
            ("salary", f"{salary} {r.get('salary_currency') or ''}".strip() if salary else ""),
        ]
        out.append(f"## {i}. job_id {r['job_id']}\n")
        out += [f"- {k}: {v}" for k, v in facts if isinstance(v, str) and v.strip()]
        out.append("\n" + wrap(r["_jd"]) + "\n")
    return "\n".join(out)


def _slices(rows: list[dict]) -> list[list[dict]]:
    out, cur, size = [], [], 0
    for r in rows:
        n = len(r["_jd"].encode("utf-8")) + 500
        if cur and (len(cur) >= SLICE_ROWS or size + n > SLICE_BYTES):
            out.append(cur)
            cur, size = [], 0
        cur.append(r)
        size += n
    return out + [cur] if cur else out


def judge_lines(run: dict, staging: Path) -> list[str]:
    """One `judge <slice file>...` line per judge, over the slices still unanswered."""
    lines, cur, size, lane = [], [], 0, None
    for name, meta in run["slices"].items():
        if (staging / f"out_{name}.json").exists():
            continue
        if cur and (
            len(cur) >= JUDGE_SLICES
            or size + meta["bytes"] > JUDGE_BYTES
            or meta["vertical"] != lane
        ):
            lines.append("judge " + " ".join(cur))
            cur, size = [], 0
        cur.append(str(staging / f"slice_{name}.md"))
        size += meta["bytes"]
        lane = meta["vertical"]
    return lines + ["judge " + " ".join(cur)] if cur else lines


def load_run(staging: Path) -> dict | None:
    p = staging / "run.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _save_run(staging: Path, run: dict) -> None:
    (staging / "run.json").write_text(json.dumps(run, indent=1), encoding="utf-8")


def start(
    clean_path: Path,
    scored_path: Path,
    staging: Path,
    cfg: ScoringConfig,
    *,
    rescore: bool = False,
    vertical: str | None = None,
    job_ids: list[str] | None = None,
    pipeline_dir: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Stage a run. Returns {"dropped": {code: n}, "judge": n, "lines": [...],
    "resumed": bool}.
    An open run is resumed when no selector is given, refused otherwise."""
    run = load_run(staging)
    if run is not None:
        if rescore or vertical or job_ids:
            raise RunOpen("a /score run is still open: run `/score` to finish it first")
        return {
            "dropped": {},
            "judge": sum(len(m["job_ids"]) for m in run["slices"].values()),
            "lines": judge_lines(run, staging),
            "resumed": True,
        }
    now = now or datetime.now()
    vcfg = verticals.get_config()
    clean = pd.read_parquet(clean_path)
    if not rescore and not job_ids:
        clean = clean[~clean["job_id"].isin(set(read_scored(scored_path)["job_id"]))]
    if rescore and not job_ids and pipeline_dir is not None:
        # Re-judging a role you have applied to, skipped or closed buys nothing.
        done = {
            j
            for j, d in load_state_index(pipeline_dir).items()
            if d.get("state") and d.get("state") not in cfg.pipeline_states
        }
        clean = clean[~clean["job_id"].isin(done)]
    if vertical is not None:
        clean = clean[clean["vertical"] == vertical]
    if job_ids:
        clean = clean[clean["job_id"].isin(job_ids)]

    dropped, keep = [], []
    counts: dict[str, int] = {}
    for row in clean.to_dict("records"):
        reason = drop_reason(row, cfg)
        if job_ids and reason and reason[0] != "no-lane":
            reason = None  # a deliberate ingest always reaches a judge
        if reason:
            dropped.append(drop_row(row, *reason, now))
            counts[reason[0]] = counts.get(reason[0], 0) + 1
            continue
        row["_jd"] = trim_jd(row.get("jd_text"), cfg)
        keep.append(row)
    merge_rows(scored_path, dropped)

    keep.sort(
        key=lambda r: (
            vcfg.names.index(r["vertical"]),
            str(r.get("company") or "").lower(),
            str(r.get("title") or "").lower(),
        )
    )
    if not keep:
        return {"dropped": counts, "judge": 0, "lines": [], "resumed": False}
    staging.mkdir(parents=True, exist_ok=True)
    run = {"model": _judge_model(), "started": now.isoformat(), "slices": {}}
    for lane in vcfg.verticals.values():
        rows = [r for r in keep if r["vertical"] == lane.name]
        if not rows:
            continue
        write_packet(staging, lane, cfg)
        for n, part in enumerate(_slices(rows), 1):
            name = f"{lane.name}_{n:03d}"
            text = _slice_text(name, part, staging)
            (staging / f"slice_{name}.md").write_text(text, encoding="utf-8")
            run["slices"][name] = {
                "vertical": lane.name,
                "bytes": len(text.encode("utf-8")),
                "job_ids": [r["job_id"] for r in part],
                "facts": {
                    r["job_id"]: {
                        "years": years_required(r.get("jd_text"), cfg.degree_rank),
                        "words": word_count(r.get("jd_text")),
                        "contract": "contract" in str(r.get("employment_type") or "").lower(),
                        # everything the judge is shown, so a quote from the title or company counts
                        "jd": normalize(
                            f"{r.get('title') or ''}\n{r.get('company') or ''}\n{r['_jd']}"
                        ),
                    }
                    for r in part
                },
            }
    _save_run(staging, run)
    return {
        "dropped": counts,
        "judge": len(keep),
        "lines": judge_lines(run, staging),
        "resumed": False,
    }


# ---------------------------------------------------------------------
# finish
# ---------------------------------------------------------------------


def _letters(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize(text))


def _quote_ok(evidence: str, jd: str) -> bool:
    """Each "..."-separated part of evidence appears in the JD word for word,
    ignoring spacing, punctuation and markdown."""
    body = _letters(jd)
    parts = [_letters(p) for p in re.split(r"\.\.\.|…", evidence)]
    return all(p in body for p in parts if len(p) >= 3)


def check_answer(answer: Any, meta: dict, focus: bool = False) -> list[str]:
    """Problems with one judge output file, [] if it can be merged. `focus`:
    the lane has a focus question, so every answer must carry `focus`."""
    if not isinstance(answer, list):
        return ["not a JSON array"]
    errs = []
    ids = [a.get("job_id") for a in answer if isinstance(a, dict)]
    if len(ids) != len(answer):
        errs.append("every item must be an object")
    if sorted(map(str, ids)) != sorted(meta["job_ids"]):
        missing = sorted(set(meta["job_ids"]) - set(map(str, ids)))
        extra = sorted(set(map(str, ids)) - set(meta["job_ids"]))
        errs.append(f"job_ids do not match the slice (missing {missing}, extra {extra})")
    for a in answer:
        if not isinstance(a, dict):
            continue
        where = f"{a.get('job_id')}:"
        for axis, top in AXIS_MAXIMA.items():
            v = a.get(axis)
            if type(v) is not int or not 0 <= v <= top:
                errs.append(f"{where} {axis} must be an int 0-{top}")
        if type(a.get("years")) is not int or not 0 <= a["years"] <= 50:
            errs.append(f"{where} years must be an int 0-50")
        if not isinstance(a.get("country"), str):
            errs.append(f"{where} country must be a string")
        facts, ev = a.get("sponsorship"), a.get("evidence")
        if (
            not isinstance(facts, list)
            or not facts
            or not all(f in SPONSORSHIP_FACTS for f in facts)
        ):
            errs.append(
                f"{where} sponsorship must be a nonempty list of: {', '.join(SPONSORSHIP_FACTS)}"
            )
            facts = []
        elif "silent" in facts and len(facts) > 1:
            errs.append(f"{where} sponsorship cannot mix silent with other statements")
        if not isinstance(ev, str):
            errs.append(f"{where} evidence must be a string")
        elif facts and facts != ["silent"] and not ev.strip():
            errs.append(f"{where} evidence must quote the JD for {', '.join(facts)}")
        elif (
            ev.strip()
            and a.get("job_id") in meta["facts"]
            and not _quote_ok(ev, meta["facts"][a["job_id"]]["jd"])
        ):
            errs.append(f"{where} evidence is not a quote from the JD: {ev[:100]!r}")
        kws = a.get("keywords_to_mirror")
        if not isinstance(kws, list) or not all(isinstance(k, str) for k in kws):
            errs.append(f"{where} keywords_to_mirror must be a list of strings")
        if not isinstance(a.get("reasoning"), str) or not a["reasoning"].strip():
            errs.append(f"{where} reasoning must be a nonempty string")
        if a.get("action") not in (None, "skip", "manual-review"):
            errs.append(f"{where} action may only be skip or manual-review")
        if focus and type(a.get("focus")) is not bool:
            errs.append(f"{where} focus must be true or false")
        elif focus and a["focus"]:
            fev = a.get("focus_evidence")
            if not isinstance(fev, str) or not fev.strip():
                errs.append(f"{where} focus true must quote the JD in focus_evidence")
            elif a.get("job_id") in meta["facts"] and not _quote_ok(
                fev, meta["facts"][a["job_id"]]["jd"]
            ):
                errs.append(f"{where} focus_evidence is not a quote from the JD: {fev[:100]!r}")
    return errs


def derive(
    a: dict,
    lane: verticals.Vertical,
    facts: dict,
    cfg: ScoringConfig,
    rule: CountryRule,
    model: str,
    now: datetime,
) -> dict:
    """The stored row for one judge answer. The judge reads the facts (years,
    country, what the JD says about sponsorship); every rule is applied here."""
    subs = {axis: a[axis] for axis in AXIS_MAXIMA}
    reasoning = a["reasoning"].strip()
    label = sponsorship_label(a["sponsorship"], a["country"], cfg)
    forced_skip = False
    years = max(facts["years"], a["years"])
    if lane.years and years > max(lane.years):
        reasoning += f" [{years}+ yrs]"
        forced_skip = True
    else:
        cap = seniority_cap(lane, years)
        if cap is not None:
            subs["seniority"] = min(subs["seniority"], cap)
    if not country_allowed(a["country"], lane.name, rule):
        reasoning += f" [out of scope: {a['country']}]"
        forced_skip = True
    if lane.focus:
        f = lane.focus
        subs["domain"] = (
            max(subs["domain"], f.yes_min) if a["focus"] else min(subs["domain"], f.no_max)
        )
    if facts["words"] < cfg.min_words:
        subs["skills"] = min(subs["skills"], cfg.skills_cap)
        reasoning += " [vague JD]"
    if facts["contract"] and "[contract role]" not in reasoning:
        reasoning += " [contract role]"
    if label == "ineligible" or forced_skip:
        subs = _zero()  # a role you cannot apply to scores 0
    total = sum(subs.values())
    action = (
        "skip"
        if label == "ineligible" or forced_skip
        else "tailor"
        if total >= cfg.tailor_min
        else "manual-review"
        if total >= cfg.review_min
        else "skip"
    )
    if a.get("action") and ACTION_RANK[a["action"]] < ACTION_RANK[action]:
        action = a["action"]
    return _row(
        a["job_id"],
        lane.name,
        subs,
        label,
        a["evidence"].strip(),
        reasoning,
        a["keywords_to_mirror"][: cfg.keywords_max],
        action,
        model,
        now,
    )


def finish(
    clean_path: Path,
    scored_path: Path,
    staging: Path,
    pipeline_dir: Path,
    cfg: ScoringConfig,
    rule: CountryRule,
    now: datetime | None = None,
) -> dict:
    """Merge every answered, valid slice. Returns {"merged", "pruned",
    "bad": {slice: [errors]}, "lines": judge lines still owed}."""
    run = load_run(staging)
    if run is None:
        return {
            "merged": 0,
            "pruned": prune(scored_path, clean_path, pipeline_dir),
            "bad": {},
            "lines": [],
        }
    now = now or datetime.now()
    lanes = verticals.get_config().verticals
    rows, bad, done = [], {}, []
    for name, meta in run["slices"].items():
        out = staging / f"out_{name}.json"
        if not out.exists():
            bad[name] = ["no answer file"]
            continue
        try:
            answer = json.loads(out.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            bad[name] = [f"not valid JSON: {e}"]
            continue
        lane = lanes[meta["vertical"]]
        errs = check_answer(answer, meta, focus=lane.focus is not None)
        if errs:
            bad[name] = errs
            continue
        rows += [
            derive(a, lane, meta["facts"][a["job_id"]], cfg, rule, run["model"], now)
            for a in answer
        ]
        done.append(name)
    merged = merge_rows(scored_path, rows)
    for name in done:
        del run["slices"][name]
        (staging / f"slice_{name}.md").unlink(missing_ok=True)
        (staging / f"out_{name}.json").unlink(missing_ok=True)
    for bad_name in bad:  # kept for diagnosis; the slice is owed again
        out = staging / f"out_{bad_name}.json"
        if out.exists():
            out.replace(staging / f"rejected_{bad_name}.json")
    if run["slices"]:
        _save_run(staging, run)
    else:
        for f in staging.glob("*"):
            f.unlink()
    return {
        "merged": merged,
        "pruned": prune(scored_path, clean_path, pipeline_dir),
        "bad": bad,
        "lines": judge_lines(run, staging) if run["slices"] else [],
    }
