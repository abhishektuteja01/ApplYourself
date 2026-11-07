"""The judge packet's two profile-built parts: the candidate digest and a
lane's calibration examples. Deterministic (R7): the digest is assembled
from resume.yaml, bullets.md, skills_master.md and the answer bank's needs
on every `score start`, so a change to those files changes the packet and
its input hash. Examples are read from profile/verticals/<lane>/examples.yaml,
which `score examples` writes from past scored rows.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from applyourself.score_filter import ScoringConfig, word_count
from applyourself.shared import clock, paths, schemas, verticals
from applyourself.shared.parquet_io import read_parquet

EXAMPLES_NAME = "examples.yaml"

_NEED = {
    "never": "authorized, needs no sponsorship",
    "later": "authorized now, will need sponsorship later",
    "now": "needs visa sponsorship now",
}
_MONTHS = {
    m: i for i, m in enumerate(("jan feb mar apr may jun jul aug sep oct nov dec").split(), 1)
}
_RANGE_RE = re.compile(
    r"^\s*([a-z]{3})[a-z]*\.?\s+(\d{4})\s*[-–]\s*"
    r"(?:([a-z]{3})[a-z]*\.?\s+(\d{4})|present|current|now)\s*$"
)


def _months(dates: Any, today: date) -> set[int]:
    """The months a "Mon YYYY - Mon YYYY" range covers, both ends in, as
    year * 12 + month; an open one ("Present") runs to `today`'s month.
    Empty for an unreadable range."""
    m = _RANGE_RE.match(str(dates or "").lower())
    if not m or m[1] not in _MONTHS or (m[3] is not None and m[3] not in _MONTHS):
        return set()
    first = int(m[2]) * 12 + _MONTHS[m[1]]
    last = today.year * 12 + today.month if m[3] is None else int(m[4]) * 12 + _MONTHS[m[3]]
    return set(range(first, last + 1))


def digest(cfg: ScoringConfig, today: date | None = None) -> str:
    """The candidate profile digest: education, every context with its span
    (an open one up to `today`, default the clock's), total work years,
    sponsorship need per country and languages, one line per bullet with its
    tags, and every skill with its aliases. A bullet's rephrasings stay out."""
    today = today or clock.today()
    from applyourself import resume_model

    src = resume_model.load_sources()
    out = [
        "## Candidate profile digest",
        "",
        "Built from the candidate's own files. Judge fit against these facts only.",
        "",
        "### Education",
        "",
    ]
    for e in src.resume.get("education") or []:
        parts = [str(e.get(k) or "").strip() for k in ("degree", "detail", "dates")]
        out.append("- " + ". ".join(p for p in parts if p))
    out += ["", "### Experience", ""]
    worked: set[int] = set()
    for ctx in src.contexts.values():
        section = "Work" if ctx.get("section") == "work" else "Project"
        head = ", ".join(
            str(ctx.get(k) or "").strip()
            for k in ("title", "org" if section == "Work" else "subtitle", "dates")
            if str(ctx.get(k) or "").strip()
        )
        months = _months(ctx.get("dates"), today)
        if section == "Work":
            worked |= months
        out.append(f"- {section}: {head}" + (f" ({len(months) / 12:.1f} years)" if months else ""))
    if worked:
        out.append(f"- Work experience in total: {len(worked) / 12:.1f} years.")
    out += ["", "### Work authorization and languages", ""]
    out += [f"- {country}: {_NEED[need]}" for country, need in cfg.country_needs]
    out.append(f"- Anywhere else: {_NEED[cfg.default_need]}")
    out.append(
        f"- Languages: {', '.join(cfg.languages)}"
        if cfg.languages
        else "- Languages: not listed (a language requirement is not a blocker)"
    )
    out += ["", "### Claims, one per bullet", ""]
    for bid, b in src.bullets.items():
        ctx = src.contexts.get(src.context_of(bid)) or {}
        tags = b.get("tags") or []
        where = str(
            (ctx.get("org") if ctx.get("section") == "work" else None)
            or ctx.get("title")
            or src.context_of(bid)
        )
        line = f"- [{where}] {' '.join(str(b.get('canonical') or '').split())}"
        out.append(line + (f" (tags: {', '.join(map(str, tags))})" if tags else ""))
    names = []
    for s in src.skills.values():
        name = str(s.get("name") or "").strip()
        aliases = [str(a).strip() for a in s.get("allowable_synonyms") or [] if str(a).strip()]
        if name:
            names.append(f"{name} ({', '.join(aliases)})" if aliases else name)
    out += ["", "### Skills (aliases in brackets)", "", "; ".join(names), ""]
    return "\n".join(out)


# ---------------------------------------------------------------------
# Calibration examples
# ---------------------------------------------------------------------


def examples_path(lane: str) -> Path:
    return paths.VERTICALS_DIR / lane / EXAMPLES_NAME


def load_examples(lane: str) -> list[dict[str, Any]]:
    """The lane's calibration examples, [] with no file. A malformed file
    fails loud, naming the entry."""
    path = examples_path(lane)
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items = data.get("examples") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError(f"{path}: needs an `examples:` list")
    for i, e in enumerate(items):
        ok = (
            isinstance(e, dict)
            and isinstance(e.get("title"), str)
            and type(e.get("in_lane")) is bool
            and type(e.get("fit")) is int
            and 0 <= e["fit"] <= 100
            and e.get("level", "entry") in verticals.LEVELS
        )
        if not ok:
            raise ValueError(
                f"{path}: examples[{i}] needs title, in_lane (true/false) and fit 0-100; "
                f"level, when set, is one of {', '.join(verticals.LEVELS)}"
            )
    return items


def examples_text(lane: str) -> str:
    """The packet's calibration section, "" when the lane has no examples."""
    items = load_examples(lane)
    if not items:
        return ""
    out = [
        "## Calibration examples",
        "",
        "Past roles in this lane and the answer each should get. Find the closest one "
        "first, then move from its fit.",
        "",
    ]
    for i, e in enumerate(items, 1):
        head = ", ".join(str(e[k]) for k in ("title", "company") if e.get(k))
        asks = e.get("asks") or []
        line = f"{i}. {head}." + (f" Asks: {', '.join(map(str, asks))}." if asks else "")
        level = f", level {e['level']}" if e.get("level") else ""
        line += f" → in_lane {str(e['in_lane']).lower()}{level}, fit {e['fit']}."

        if e.get("reasoning"):
            line += f' "{e["reasoning"]}"'
        out.append(line)
    return "\n".join(out) + "\n"


# The old judge's four axes, read once to turn a past row into an example.
_OLD_FIT_AXES = ("title", "skills", "domain")
_OLD_FIT_MAX = 80
# (in_lane, target fit): one example per upper band, and one out-of-lane role.
_TAG_RE = re.compile(r"\s*\[[^\]]*\]")


def _jds(store_path: Path, clean_path: Path, ids: set[str]) -> dict[str, Any]:
    """job_id -> its longest JD, from the store, else the live view."""
    out: dict[str, Any] = {}
    for path, version in (
        (store_path, schemas.STORE_SCHEMA_VERSION),
        (clean_path, schemas.CLEAN_SCHEMA_VERSION),
    ):
        if not path.exists():
            continue
        d = read_parquet(path, version, columns=["job_id", "jd_text", "title", "company"])
        d = d[d["job_id"].isin(ids - set(out))]
        d = d.assign(_n=d["jd_text"].fillna("").str.len()).sort_values(["_n", "job_id"])
        for r in d.drop_duplicates("job_id", keep="last").to_dict("records"):
            out[str(r["job_id"])] = r
    return out


def candidates(
    scored: pd.DataFrame, lane: str, cfg: ScoringConfig, store_path: Path, clean_path: Path
) -> list[dict[str, Any]]:
    """Every past row of the lane that can be an example: a long-enough JD
    and an answer the new form can be read from (today's answer, or the old
    four axes)."""
    rows = scored[
        (scored["vertical"] == lane)
        & ~scored["scored_by_model"].astype(str).str.startswith("filter:")
    ]
    jds = _jds(store_path, clean_path, set(rows["job_id"]))
    out = []
    for r in rows.to_dict("records"):
        jd = jds.get(str(r["job_id"]))
        if jd is None or word_count(jd["jd_text"]) < cfg.min_words:
            continue
        answer = (
            json.loads(r["judge_answer"])
            if isinstance(r["judge_answer"], str) and r["judge_answer"]
            else None
        )
        if isinstance(answer, dict) and "fit" in answer:
            if answer.get("blocker") is not None:
                continue
            got = {k: answer[k] for k in ("in_lane", "level", "fit")}
        else:
            # An old judge's raw axes; a legacy row has only the stored ones,
            # which a zero rule set all to 0.
            try:
                subs = answer if isinstance(answer, dict) else json.loads(r["fit_subscores"])
                axes = {k: int(subs[k]) for k in (*_OLD_FIT_AXES, "seniority")}
            except (TypeError, ValueError, KeyError):
                continue
            if r["fit_score"] == 0 and (axes["title"] > 0 or not any(axes.values())):
                continue  # zeroed by a rule, not out of lane: says nothing about fit
            fit = round(sum(axes[k] for k in _OLD_FIT_AXES) * 100 / _OLD_FIT_MAX)
            # Old seniority holds the years cap too, so it gives no level.
            got = {"in_lane": axes["title"] > 0, "fit": fit}
        out.append(
            {
                "job_id": str(r["job_id"]),
                "title": str(jd.get("title") or ""),
                "company": str(jd.get("company") or ""),
                "asks": [str(k) for k in list(r["keywords_to_mirror"])[:4]],
                **got,
                "reasoning": _TAG_RE.sub("", str(r["reasoning"] or "")).strip(),
            }
        )
    return out


def choose(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One example per scoring_defaults.yaml `example_targets` entry
    ([in_lane, fit]), by a stable rule: in lane, the
    row whose fit is nearest the target; out of lane, the one with the
    highest fit. Ties go to the lowest job_id; a row is used once."""
    picked: list[dict[str, Any]] = []
    from applyourself.shared import scoring_defaults

    for in_lane, target in scoring_defaults.load()["example_targets"]:
        used = {c["job_id"] for c in picked}
        pool = [c for c in cands if c["in_lane"] is in_lane and c["job_id"] not in used]

        def key(
            c: dict[str, Any], target: int = target, in_lane: bool = in_lane
        ) -> tuple[int, str]:
            return (abs(c["fit"] - target) if in_lane else -c["fit"], c["job_id"])

        if pool:
            picked.append(min(pool, key=key))
    return picked


def examples_yaml(picked: list[dict[str, Any]]) -> str:
    body = yaml.safe_dump(
        {"examples": [{k: v for k, v in c.items() if k != "job_id"} for c in picked]},
        sort_keys=False,
        allow_unicode=True,
        width=100,
    )
    ids = ", ".join(c["job_id"] for c in picked)
    return (
        "# Calibration examples for /score's judge: past roles in this lane and the\n"
        "# answer each should get. Written by `uv run score examples`; edit freely.\n"
        f"# From job_ids: {ids}\n" + body
    )
