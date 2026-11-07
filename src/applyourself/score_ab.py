"""`score ab-stage` and `score ab-compare`: the old (v1) and the new (v2)
judge packet over the same rows, staged into their own folders for the owner
to judge, then compared. Nothing here runs a judge (R7) or touches
jobs/scored.parquet or jobs/scored.staging/.

The v1 arm (its packet, answer form and score) lives only here, read from
the old keys a lane.yaml still holds; delete it once the A/B is done.

  <out>/sample.json      the rows, their lanes and stored scores, the seed
  <out>/v1/, <out>/v2/   one staging folder per arm, as `score start` writes
  <out>/v2_repeat/       a subset judged a second time with the v2 packet
"""

from __future__ import annotations

import json
import random
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from applyourself import score_packet, score_store
from applyourself.score_filter import (
    SPONSORSHIP_FACTS,
    CountryRule,
    ScoringConfig,
    country_allowed,
    drop_reason,
    lane_cutoffs,
    sponsorship_label,
    trim_jd,
)
from applyourself.shared import clock, paths, schemas, verticals
from applyourself.shared.parquet_io import read_parquet

ARMS = ("v1", "v2", "v2_repeat")
V1_RUBRIC = paths.REPO_ROOT / ".claude" / "shared" / "scoring_rubric_v1.md"
V1_LANE_RUBRIC = "rubric.v1.md"  # beside rubric.md once that holds the v2 lane
_V1_AXES = {"title": 30, "skills": 30, "seniority": 20, "domain": 20}
_V1_SKILLS_CAP = 12  # scoring.yaml jd_quality.skills_cap when it is gone


class V1Answer(BaseModel):
    """One role in a judge's answer file. The scoring rubric says what each field means."""

    model_config = ConfigDict(extra="forbid", strict=True, title="JudgeAnswer")

    job_id: str
    title: Annotated[int, Field(ge=0, le=_V1_AXES["title"])]
    skills: Annotated[int, Field(ge=0, le=_V1_AXES["skills"])]
    seniority: Annotated[int, Field(ge=0, le=_V1_AXES["seniority"])]
    domain: Annotated[int, Field(ge=0, le=_V1_AXES["domain"])]
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
    def _evidence(self) -> V1Answer:
        if "silent" in self.sponsorship and len(self.sponsorship) > 1:
            raise ValueError("sponsorship cannot mix silent with other statements")
        if self.sponsorship != ["silent"] and not self.evidence.strip():
            raise ValueError(f"evidence must quote the JD for {', '.join(self.sponsorship)}")
        return self


def raw_lane(name: str) -> dict[str, Any]:
    """The lane's lane.yaml merged over `defaults:`, as written: the v1 arm
    reads the old keys the loader no longer keeps."""
    top = yaml.safe_load(paths.VERTICALS_CONFIG.read_text(encoding="utf-8")) or {}
    lane = yaml.safe_load(
        verticals.lane_file(paths.VERTICALS_CONFIG, name).read_text(encoding="utf-8")
    )
    return verticals.merge_defaults(top.get("defaults") or {}, lane or {})


def _v1_focus(raw: dict[str, Any]) -> dict[str, Any] | None:
    f = raw.get("focus")
    return f if isinstance(f, dict) and f.get("question") else None


def v1_packet(lane: verticals.Vertical, cfg: ScoringConfig) -> str:
    """The packet the old judge read, from the old keys and the old rubrics."""
    from applyourself import resume_model

    raw = raw_lane(lane.name)
    rubric = paths.VERTICALS_DIR / lane.name / V1_LANE_RUBRIC
    if not rubric.is_file():
        rubric = paths.VERTICALS_DIR / lane.name / "rubric.md"
    parts = [
        f"# Scoring packet — lane {lane.name}\n",
        f"`keywords_to_mirror`: up to {cfg.keywords_max}.\n",
        f"Languages the candidate speaks: {', '.join(cfg.languages)}.\n" if cfg.languages else "",
        V1_RUBRIC.read_text(encoding="utf-8"),
        rubric.read_text(encoding="utf-8"),
        "## Skill weights\n\n```yaml\n"
        + yaml.safe_dump(raw.get("skill_weights") or {}, sort_keys=False).strip()
        + "\n```\n",
        "## Resume\n\n" + resume_model.baseline_markdown(lane.name),
        score_store.schema_text(V1Answer),
    ]
    f = _v1_focus(raw)
    if f:
        parts.insert(
            4,
            "## Focus\n\n"
            f"Add `focus`: true or false, and `focus_evidence`. {' '.join(str(f['question']).split())}\n\n"
            "Default false. Answer true only when the JD itself shows it, and put "
            'that JD phrase, verbatim, in `focus_evidence`; leave it "" when false. '
            "A strong fit is not a reason for true.\n\n"
            f"Then `domain` is set for you: true lifts it to at least {f['yes_min']}, "
            f"false holds it at {f['no_max']} or below.\n",
        )
    return "\n".join(parts)


def v1_warnings(lane: str) -> list[str]:
    """Why the v1 arm may not be the packet the old judge read."""
    raw = raw_lane(lane)
    out = []
    if not (paths.VERTICALS_DIR / lane / V1_LANE_RUBRIC).is_file():
        out.append(f"{lane}: no {V1_LANE_RUBRIC}; the v1 arm reads rubric.md")
    if not raw.get("skill_weights"):
        out.append(f"{lane}: no skill_weights in lane.yaml; stage the A/B before `config migrate`")
    return out


def v1_score(
    a: dict[str, Any], lane: str, facts: dict[str, Any], cfg: ScoringConfig, rule: CountryRule
) -> tuple[float, bool]:
    """(total, skipped) the old rules give one v1 answer."""
    raw = raw_lane(lane)
    subs = {k: a[k] for k in _V1_AXES}
    years_tbl = {int(k): int(v) for k, v in (raw.get("years") or {}).items()}
    forced = False
    if years_tbl and a["years"] > max(years_tbl):
        forced = True
    else:
        keys = [k for k in years_tbl if k <= a["years"]]
        if keys:
            subs["seniority"] = min(subs["seniority"], years_tbl[max(keys)])
    if not country_allowed(a["country"], lane, rule):
        forced = True
    f = _v1_focus(raw)
    if f:
        subs["domain"] = (
            max(subs["domain"], f["yes_min"])
            if a.get("focus")
            else min(subs["domain"], f["no_max"])
        )
    if facts["words"] < cfg.min_words:
        subs["skills"] = min(subs["skills"], _v1_skills_cap())
    if sponsorship_label(a["sponsorship"], a["country"], cfg) == "ineligible" or forced:
        return 0.0, True
    return float(sum(subs.values())), a.get("action") == "skip"


def _v1_skills_cap() -> int:
    data = yaml.safe_load(paths.SCORING.read_text(encoding="utf-8")) or {}
    cap = (data.get("jd_quality") or {}).get("skills_cap")
    return cap if type(cap) is int else _V1_SKILLS_CAP


# ---------------------------------------------------------------------
# ab-stage
# ---------------------------------------------------------------------


def sample(
    candidates: list[dict[str, Any]], scores: dict[str, float], rows: int, top: int, seed: int
) -> list[dict[str, Any]]:
    """Each lane's `top` best-scored rows, then random others (seeded,
    spread over the lanes by their share) up to `rows` in all."""
    by_lane: dict[str, list[dict[str, Any]]] = {}
    for r in sorted(candidates, key=lambda r: r["job_id"]):
        by_lane.setdefault(r["vertical"], []).append(r)
    picked: list[dict[str, Any]] = []
    rest: dict[str, list[dict[str, Any]]] = {}
    for lane, lane_rows in by_lane.items():
        ranked = sorted(
            (r for r in lane_rows if r["job_id"] in scores),
            key=lambda r: (-scores[r["job_id"]], r["job_id"]),
        )
        tops = ranked[:top]
        picked += tops
        taken = {r["job_id"] for r in tops}
        rest[lane] = [r for r in lane_rows if r["job_id"] not in taken]
    extra = max(0, rows - len(picked))
    pool = sum(len(v) for v in rest.values())
    rng = random.Random(seed)
    for lane in sorted(rest):
        k = min(len(rest[lane]), round(extra * len(rest[lane]) / pool)) if pool else 0
        picked += rng.sample(rest[lane], k)
    return picked


def stage(
    out: Path,
    cfg: ScoringConfig,
    *,
    rows: int,
    top: int,
    repeat: int,
    seed: int,
    now: datetime,
    vertical: str | None = None,
) -> dict[str, Any]:
    """Write the sample and the three arms under `out`. Returns
    {"rows": n, "lines": {arm: [judge lines]}, "warnings": [...]}."""
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"{out} is not empty: pick a new --out folder")
    vcfg = verticals.get_config()
    clean = read_parquet(paths.CLEAN, schemas.CLEAN_SCHEMA_VERSION)
    if vertical is not None:
        clean = clean[clean["vertical"] == vertical]
    live = [
        r
        for r in cast(list[dict[str, Any]], clean.to_dict("records"))
        if r.get("vertical") in vcfg.verticals and drop_reason(r, cfg) is None
    ]
    view = score_store.view(paths.SCORED, cfg) if paths.SCORED.exists() else None
    scores = {} if view is None else dict(zip(view["job_id"], view["fit_score"], strict=True))
    picked = sample(live, scores, rows, top, seed)
    picked.sort(key=lambda r: (r["vertical"], r["job_id"]))
    for r in picked:
        r["_jd"] = trim_jd(r.get("jd_text"), cfg)
    repeat_ids = set(
        random.Random(seed + 1).sample(
            sorted(r["job_id"] for r in picked), min(repeat, len(picked))
        )
    )
    lanes = sorted({r["vertical"] for r in picked}, key=list(vcfg.names).index)
    digest = score_packet.digest(cfg)
    packets = {
        "v1": {n: v1_packet(vcfg.verticals[n], cfg) for n in lanes},
        "v2": {n: score_store.packet_text(vcfg.verticals[n], cfg, digest) for n in lanes},
    }
    packets["v2_repeat"] = packets["v2"]
    lines: dict[str, list[str]] = {}
    for arm in ARMS:
        arm_rows = [
            {**r, "_hash": score_store.input_hash(packets[arm][r["vertical"]], r["_jd"])}
            for r in picked
            if arm != "v2_repeat" or r["job_id"] in repeat_ids
        ]
        if not arm_rows:
            continue
        run = score_store.stage_rows(out / arm, arm_rows, packets[arm], cfg, now)
        lines[arm] = score_store.judge_lines(run, out / arm)
    (out / "sample.json").write_text(
        json.dumps(
            {
                "seed": seed,
                "rows": rows,
                "top": top,
                "lanes": {r["job_id"]: r["vertical"] for r in picked},
                "titles": {
                    r["job_id"]: f"{r.get('company') or ''} — {r.get('title') or ''}"
                    for r in picked
                },
                "stored": {r["job_id"]: scores.get(r["job_id"]) for r in picked},
                "repeat": sorted(repeat_ids),
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    return {
        "rows": len(picked),
        "lines": lines,
        "warnings": [w for n in lanes for w in v1_warnings(n)],
    }


# ---------------------------------------------------------------------
# ab-compare
# ---------------------------------------------------------------------


def _v1_problems(answer: Any, meta: dict[str, Any], focus: bool) -> list[str]:
    errs = score_store.check_answer(answer, meta, V1Answer)
    if not errs and focus:
        errs += [
            f"{a['job_id']}: focus must be true or false" for a in answer if a.get("focus") is None
        ]
    return errs


def read_arm(
    out: Path, arm: str, cfg: ScoringConfig, rule: CountryRule
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """job_id -> {score, fit, skip, answer} for every answered, valid slice of
    one arm, and one problem line per slice left out."""
    run = score_store.load_run(out / arm)
    if run is None:
        return {}, [f"{arm}: not staged"]
    lanes = verticals.get_config().verticals
    got: dict[str, dict[str, Any]] = {}
    problems = []
    for name, meta in run["slices"].items():
        path = out / arm / f"out_{name}.json"
        if not path.is_file():
            problems.append(f"{arm} {name}: no answer file")
            continue
        try:
            answer = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            problems.append(f"{arm} {name}: not valid JSON: {e}")
            continue
        lane = meta["vertical"]
        errs = (
            _v1_problems(answer, meta, _v1_focus(raw_lane(lane)) is not None)
            if arm == "v1"
            else score_store.check_answer(answer, meta)
        )
        if errs:
            problems.append(f"{arm} {name}: {'; '.join(errs[:3])}")
            continue
        for a in answer:
            facts = meta["facts"][a["job_id"]]
            if arm == "v1":
                score, skip = v1_score(a, lane, facts, cfg, rule)
                got[a["job_id"]] = {"score": score, "skip": skip, "fit": None, "answer": a}
            else:
                row = score_store.derive(a, lanes[lane], facts, cfg, rule, "ab", clock.now())
                got[a["job_id"]] = {
                    "score": row["fit_score"],
                    "skip": row["suggested_action"] == "skip",
                    "fit": a["fit"],
                    "answer": a,
                }
    return got, problems


def _rho(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3:
        return None
    r = pd.Series(a).rank().corr(pd.Series(b).rank())
    return None if pd.isna(r) else round(float(r), 3)


def compare(
    out: Path, cfg: ScoringConfig, rule: CountryRule, v1_cutoff: int | None = None
) -> dict[str, Any]:
    """The A/B report: per lane and overall, rank correlation and top-k
    overlap of v1 and v2 scores, the rows one arm puts at or above the cutoff
    and the other does not, and the v2 consistency check (v2 vs v2_repeat)."""
    meta = json.loads((out / "sample.json").read_text(encoding="utf-8"))
    arms = {}
    problems: list[str] = []
    for arm in ARMS:
        arms[arm], p = read_arm(out, arm, cfg, rule)
        problems += p
    cuts = lane_cutoffs()
    v1, v2, rep = arms["v1"], arms["v2"], arms["v2_repeat"]
    both = sorted(set(v1) & set(v2))
    groups: dict[str, list[str]] = {"all": both}
    for j in both:
        groups.setdefault(meta["lanes"][j], []).append(j)
    lanes = []
    for name, ids in groups.items():
        k = min(100, len(ids) // 2)
        top1 = set(sorted(ids, key=lambda j: (-v1[j]["score"], j))[:k])
        top2 = set(sorted(ids, key=lambda j: (-v2[j]["score"], j))[:k])
        stored = [j for j in ids if meta["stored"].get(j) is not None]
        lanes.append(
            {
                "lane": name,
                "n": len(ids),
                "rho_v1_v2": _rho([v1[j]["score"] for j in ids], [v2[j]["score"] for j in ids]),
                "rho_stored_v2": _rho(
                    [meta["stored"][j] for j in stored], [v2[j]["score"] for j in stored]
                ),
                "top_k": k,
                "top_overlap": len(top1 & top2),
            }
        )
    crossing = []
    for j in both:
        lane = meta["lanes"][j]
        cut = cuts.get(lane, min(cuts.values()))
        on1 = v1[j]["score"] >= (v1_cutoff if v1_cutoff is not None else cut) and not v1[j]["skip"]
        on2 = v2[j]["score"] >= cut and not v2[j]["skip"]
        if on1 != on2:
            crossing.append(
                {
                    "job_id": j,
                    "lane": lane,
                    "v1": v1[j]["score"],
                    "v2": v2[j]["score"],
                    "role": meta["titles"][j],
                }
            )
    twice = sorted(set(v2) & set(rep))
    fit_d = [abs(v2[j]["fit"] - rep[j]["fit"]) for j in twice]
    score_d = [abs(v2[j]["score"] - rep[j]["score"]) for j in twice]
    same = {
        f: sum(v2[j]["answer"][f] == rep[j]["answer"][f] for j in twice)
        for f in ("in_lane", "level", "years")
    }
    return {
        "lanes": lanes,
        "crossing": crossing,
        "consistency": {
            "n": len(twice),
            "fit_mean": round(sum(fit_d) / len(fit_d), 2) if fit_d else None,
            "fit_max": max(fit_d, default=None),
            "score_mean": round(sum(score_d) / len(score_d), 2) if score_d else None,
            "score_max": max(score_d, default=None),
            "fit_over_3": sum(d > 3 for d in fit_d),
            "same": same,
        },
        "problems": problems,
    }
