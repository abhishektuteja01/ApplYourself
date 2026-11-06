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

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from applyourself.score_filter import (
    SPONSORSHIP_FACTS,
    CountryRule,
    ScoringConfig,
    country_allowed,
    drop_reason,
    load_scoring,
    normalize,
    seniority_cap,
    sponsorship_label,
    trim_jd,
    word_count,
    wrap,
    years_required,
)
from applyourself.shared import clock, paths, schemas, verticals
from applyourself.shared.parquet_io import locked, read_parquet, schema_version, write_parquet
from applyourself.state_io import load_state_index

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
    "judge_answer",  # JSON: the judge's raw answer; empty on a legacy row
    "judge_facts",  # JSON: {years, words, contract} read from the JD at start
    "judge_input_hash",  # input_hash() of what was judged; empty on a legacy row
]
# What view() derives again from judge_answer under today's rules.
DERIVED_COLUMNS = (
    "fit_score",
    "fit_subscores",
    "sponsorship_label",
    "sponsorship_evidence",
    "reasoning",
    "keywords_to_mirror",
    "suggested_action",
)
VIEW_COLUMNS: list[str] = [c for c in SCORED_COLUMNS if not c.startswith("judge_")] + ["basis"]
AXIS_MAXIMA = {"title": 30, "skills": 30, "seniority": 20, "domain": 20}
ACTION_RANK = {"skip": 0, "manual-review": 1, "tailor": 2}

YEARS_GAP_LOG = 3  # regex and judge years this far apart are printed by finish

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
    schemas.check(schema_version(scored_path), schemas.SCORED_SCHEMA_VERSION, scored_path)
    df = pd.read_parquet(scored_path)
    for col in SCORED_COLUMNS:
        if col not in df.columns:
            df[col] = pd.Series(dtype="object")
    return df[SCORED_COLUMNS]


def view(
    scored_path: Path | None = None,
    cfg: ScoringConfig | None = None,
    rule: CountryRule | None = None,
) -> pd.DataFrame:
    """Every live score, the one way to read scored.parquet. A row holding the
    judge's raw answer is derived again under today's rules (`basis` judged);
    an older row keeps its stored score (`basis` legacy). Filter drops are not
    scores and are left out."""
    df = read_scored(scored_path or paths.SCORED)
    df = df[~df["scored_by_model"].astype(str).str.startswith("filter:")].reset_index(drop=True)
    raw = [isinstance(v, str) and v != "" for v in df["judge_answer"]]
    basis = ["legacy"] * len(df)
    if any(raw):
        cfg = cfg or load_scoring()
        if rule is None:
            from applyourself.discovery.config import load_config

            rule = CountryRule.from_discovery(load_config())
        lanes = verticals.get_config().verticals
        cols = {c: df[c].astype(object).tolist() for c in DERIVED_COLUMNS}
        for i, r in enumerate(cast(list[dict[str, Any]], df.to_dict("records"))):
            lane = lanes.get(r["vertical"])
            if not raw[i] or lane is None:
                continue  # a lane no longer configured keeps its stored score
            d = derive(
                json.loads(r["judge_answer"]),
                lane,
                json.loads(r["judge_facts"]),
                cfg,
                rule,
                r["scored_by_model"],
                r["scored_at"],
            )
            for c in DERIVED_COLUMNS:
                cols[c][i] = d[c]
            basis[i] = "judged"
        df = df.assign(**{c: pd.Series(v, index=df.index, dtype=object) for c, v in cols.items()})
        df["fit_score"] = df["fit_score"].astype("float64")
    df["basis"] = basis
    return df[VIEW_COLUMNS]


def merge_rows(scored_path: Path, rows: list[dict[str, Any]]) -> int:
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
        write_parquet(
            out.drop_duplicates("job_id", keep="last"),
            scored_path,
            schema_version=schemas.SCORED_SCHEMA_VERSION,
        )
    return len(rows)


def _row(
    job_id: str,
    vertical: str,
    subs: dict[str, Any],
    label: str,
    evidence: str,
    reasoning: str,
    keywords: list[Any],
    action: str,
    model: str,
    now: datetime,
) -> dict[str, Any]:
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
        "judge_answer": "",
        "judge_facts": "",
        "judge_input_hash": "",
    }


def _zero() -> dict[str, Any]:
    return dict.fromkeys(AXIS_MAXIMA, 0)


def remove_rows(scored_path: Path, job_ids: set[str]) -> int:
    """Delete these job_ids' scores: a role today's hard rules drop has none."""
    if not job_ids or not scored_path.exists():
        return 0
    with locked(scored_path):
        schemas.check(schema_version(scored_path), schemas.SCORED_SCHEMA_VERSION, scored_path)
        scored = pd.read_parquet(scored_path)
        mask = scored["job_id"].isin(job_ids)
        if mask.any():
            write_parquet(scored[~mask], scored_path, schema_version=schemas.SCORED_SCHEMA_VERSION)
    return int(mask.sum())


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
    from applyourself import resume_model

    if lane.resume is None:
        raise ValueError(
            f"lane {lane.name} has no resume: block in profile/verticals/{lane.name}/lane.yaml"
        )
    return resume_model.baseline_markdown(lane.name)


def input_hash(packet: str, jd: str) -> str:
    """What a judgment was made from: the lane's packet and the trimmed JD."""
    return hashlib.sha1(f"{packet}\0{jd}".encode()).hexdigest()[:16]


def packet_text(lane: verticals.Vertical, cfg: ScoringConfig) -> str:
    """What every judge of the lane reads first."""
    parts = [
        f"# Scoring packet — lane {lane.name}\n",
        f"`keywords_to_mirror`: up to {cfg.keywords_max}.\n",
        f"Languages the candidate speaks: {', '.join(cfg.languages)}.\n" if cfg.languages else "",
        SHARED_RUBRIC.read_text(encoding="utf-8"),
        (paths.VERTICALS_DIR / lane.name / "rubric.md").read_text(encoding="utf-8"),
        "## Skill weights\n\n```yaml\n"
        + yaml.safe_dump(lane.skill_weights, sort_keys=False).strip()
        + "\n```\n",
        "## Resume\n\n" + _lane_resume(lane),
        "## Answer schema\n\nEach object in your JSON array matches this JSON Schema.\n\n```json\n"
        + json.dumps(JudgeAnswer.model_json_schema(), indent=1)
        + "\n```\n",
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
    return "\n".join(parts)


def _slice_text(name: str, rows: list[dict[str, Any]], staging: Path) -> str:
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


def _slices(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    out: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    size = 0
    for r in rows:
        n = len(r["_jd"].encode("utf-8")) + 500
        if cur and (len(cur) >= SLICE_ROWS or size + n > SLICE_BYTES):
            out.append(cur)
            cur, size = [], 0
        cur.append(r)
        size += n
    return out + [cur] if cur else out


def judge_lines(run: dict[str, Any], staging: Path) -> list[str]:
    """One `judge <slice file>...` line per judge, over the slices still unanswered."""
    lines: list[str] = []
    cur: list[str] = []
    size, lane = 0, None
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


def load_run(staging: Path) -> dict[str, Any] | None:
    p = staging / "run.json"
    if not p.exists():
        return None
    run: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    schemas.check(run.get("schema_version"), schemas.SCORE_STAGING_SCHEMA_VERSION, p)
    return run


def _save_run(staging: Path, run: dict[str, Any]) -> None:
    (staging / "run.json").write_text(json.dumps(run, indent=1), encoding="utf-8")


def first_seen_last_run(store_path: Path | None, job_ids: set[str]) -> set[str] | None:
    """The ids among `job_ids` the latest discovery run saw first: earliest
    first_seen on or after the store's newest last_seen. None: no store."""
    if store_path is None or not Path(store_path).exists():
        return None
    store = read_parquet(
        store_path, schemas.STORE_SCHEMA_VERSION, columns=["job_id", "first_seen", "last_seen"]
    )
    latest = pd.to_datetime(store["last_seen"]).max()
    first = pd.to_datetime(store["first_seen"]).groupby(store["job_id"]).min()
    return {j for j in job_ids if j in first.index and first[j] >= latest}


def start(
    clean_path: Path,
    scored_path: Path,
    staging: Path,
    cfg: ScoringConfig,
    *,
    rescore: bool = False,
    stale_only: bool = False,
    count_only: bool = False,
    vertical: str | None = None,
    job_ids: list[str] | None = None,
    pipeline_dir: Path | None = None,
    now: datetime | None = None,
    store_path: Path | None = None,
) -> dict[str, Any]:
    """Stage a run. Returns {"dropped": {code: n}, "first_dropped": {code: n},
    "judge": n, "lines": [...], "resumed": bool}. A dropped row is not stored,
    and loses any score it had. `first_dropped` counts only the drops the
    latest discovery run saw first (all of them without `store_path`).
    `stale_only` takes only judged rows whose input hash changed; a row with
    no hash is never stale. `count_only` returns the counts and writes nothing.
    An open run is resumed when no selector is given, refused otherwise."""
    run = load_run(staging)
    if run is not None:
        if rescore or stale_only or vertical or job_ids:
            raise RunOpen("a /score run is still open: run `/score` to finish it first")
        return {
            "dropped": {},
            "first_dropped": {},
            "judge": sum(len(m["job_ids"]) for m in run["slices"].values()),
            "lines": judge_lines(run, staging),
            "resumed": True,
        }
    now = now or clock.now()
    vcfg = verticals.get_config()
    clean = read_parquet(clean_path, schemas.CLEAN_SCHEMA_VERSION)
    stored = read_scored(scored_path)
    hashes = {
        j: h
        for j, h in zip(stored["job_id"], stored["judge_input_hash"], strict=True)
        if isinstance(h, str) and h
    }
    if stale_only:
        clean = clean[clean["job_id"].isin(set(hashes))]
    elif not rescore and not job_ids:
        clean = clean[~clean["job_id"].isin(set(stored["job_id"]))]
    if (rescore or stale_only) and not job_ids and pipeline_dir is not None:
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

    dropped: set[str] = set()
    keep: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    codes: dict[str, str] = {}
    packets: dict[str, str] = {}
    for row in cast(list[dict[str, Any]], clean.to_dict("records")):
        reason = drop_reason(row, cfg)
        if job_ids and reason and reason[0] != "no-lane":
            reason = None  # a deliberate ingest always reaches a judge
        if reason:
            dropped.add(row["job_id"])
            codes[row["job_id"]] = reason[0]
            counts[reason[0]] = counts.get(reason[0], 0) + 1
            continue
        row["_jd"] = trim_jd(row.get("jd_text"), cfg)
        lane = row["vertical"]
        if lane not in packets:
            packets[lane] = packet_text(vcfg.verticals[lane], cfg)
        row["_hash"] = input_hash(packets[lane], row["_jd"])
        if stale_only and row["_hash"] == hashes.get(row["job_id"]):
            continue
        keep.append(row)
    fresh = first_seen_last_run(store_path, dropped)
    first: dict[str, int] = {}
    for jid, code in codes.items():
        if fresh is None or jid in fresh:
            first[code] = first.get(code, 0) + 1
    if count_only:
        return {
            "dropped": counts,
            "first_dropped": first,
            "judge": len(keep),
            "lines": [],
            "resumed": False,
        }
    remove_rows(scored_path, dropped)

    keep.sort(
        key=lambda r: (
            vcfg.names.index(r["vertical"]),
            str(r.get("company") or "").lower(),
            str(r.get("title") or "").lower(),
        )
    )
    if not keep:
        return {
            "dropped": counts,
            "first_dropped": first,
            "judge": 0,
            "lines": [],
            "resumed": False,
        }
    staging.mkdir(parents=True, exist_ok=True)
    run = {
        "schema_version": schemas.SCORE_STAGING_SCHEMA_VERSION,
        "model": _judge_model(),
        "started": now.isoformat(),
        "slices": {},
    }
    for lane in vcfg.verticals.values():
        rows = [r for r in keep if r["vertical"] == lane.name]
        if not rows:
            continue
        (staging / f"packet_{lane.name}.md").write_text(packets[lane.name], encoding="utf-8")
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
                        "hash": r["_hash"],
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
        "first_dropped": first,
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


class JudgeAnswer(BaseModel):
    """One role in a judge's answer file. The scoring rubric says what each field means."""

    model_config = ConfigDict(extra="forbid", strict=True)

    job_id: str
    title: Annotated[int, Field(ge=0, le=AXIS_MAXIMA["title"])]
    skills: Annotated[int, Field(ge=0, le=AXIS_MAXIMA["skills"])]
    seniority: Annotated[int, Field(ge=0, le=AXIS_MAXIMA["seniority"])]
    domain: Annotated[int, Field(ge=0, le=AXIS_MAXIMA["domain"])]
    years: Annotated[int, Field(ge=0, le=50)]
    country: str
    sponsorship: Annotated[list[Literal[SPONSORSHIP_FACTS]], Field(min_length=1)]  # type: ignore[valid-type]  # pydantic expands the tuple
    evidence: str
    keywords_to_mirror: list[str]
    reasoning: Annotated[str, Field(pattern=r"\S")]
    action: Literal["skip", "manual-review"] | None = None
    focus: bool | None = None
    focus_evidence: str | None = None

    @model_validator(mode="after")
    def _evidence(self) -> JudgeAnswer:
        if "silent" in self.sponsorship and len(self.sponsorship) > 1:
            raise ValueError("sponsorship cannot mix silent with other statements")
        if self.sponsorship != ["silent"] and not self.evidence.strip():
            raise ValueError(f"evidence must quote the JD for {', '.join(self.sponsorship)}")
        return self


def check_answer(answer: Any, meta: dict[str, Any], focus: bool = False) -> list[str]:
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
        try:
            JudgeAnswer.model_validate(a)
        except ValidationError as e:
            errs += [
                f"{where} {'.'.join(map(str, x['loc'])) or 'answer'}: {x['msg']}"
                for x in e.errors()
            ]
            continue
        jd = meta["facts"].get(a["job_id"], {}).get("jd")
        ev = a["evidence"]
        if ev.strip() and jd is not None and not _quote_ok(ev, jd):
            errs.append(f"{where} evidence is not a quote from the JD: {ev[:100]!r}")
        if focus and a.get("focus") is None:
            errs.append(f"{where} focus must be true or false")
        elif focus and a["focus"]:
            fev = a.get("focus_evidence") or ""
            if not fev.strip():
                errs.append(f"{where} focus true must quote the JD in focus_evidence")
            elif jd is not None and not _quote_ok(fev, jd):
                errs.append(f"{where} focus_evidence is not a quote from the JD: {fev[:100]!r}")
    return errs


def derive(
    a: dict[str, Any],
    lane: verticals.Vertical,
    facts: dict[str, Any],
    cfg: ScoringConfig,
    rule: CountryRule,
    model: str,
    now: datetime,
) -> dict[str, Any]:
    """The stored row for one judge answer. The judge reads the facts (years,
    country, what the JD says about sponsorship); every rule is applied here."""
    subs = {axis: a[axis] for axis in AXIS_MAXIMA}
    reasoning = a["reasoning"].strip()
    label = sponsorship_label(a["sponsorship"], a["country"], cfg)
    forced_skip = False
    years = a["years"]  # the regex only pre-filters; the judge's reading counts
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
    scored_path: Path,
    staging: Path,
    cfg: ScoringConfig,
    rule: CountryRule,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Merge every answered, valid slice. Returns {"merged",
    "bad": {slice: [errors]}, "lines": judge lines still owed,
    "years_gaps": [(job_id, regex years, judge years)] that differ by YEARS_GAP_LOG+}."""
    run = load_run(staging)
    if run is None:
        return {
            "merged": 0,
            "bad": {},
            "lines": [],
            "years_gaps": [],
        }
    now = now or clock.now()
    lanes = verticals.get_config().verticals
    rows, bad, done = [], {}, []
    gaps: list[tuple[str, int, int]] = []
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
        for a in answer:
            facts = meta["facts"][a["job_id"]]
            row = derive(a, lane, facts, cfg, rule, run["model"], now)
            row["judge_answer"] = json.dumps(a)
            row["judge_facts"] = json.dumps({k: facts[k] for k in ("years", "words", "contract")})
            row["judge_input_hash"] = facts.get("hash", "")
            rows.append(row)
            if abs(facts["years"] - a["years"]) >= YEARS_GAP_LOG:
                gaps.append((a["job_id"], facts["years"], a["years"]))
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
        "bad": bad,
        "lines": judge_lines(run, staging) if run["slices"] else [],
        "years_gaps": gaps,
    }
