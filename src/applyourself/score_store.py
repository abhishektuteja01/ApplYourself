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

import functools
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import pandas as pd
import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from applyourself import score_packet
from applyourself.score_filter import (
    SPONSORSHIP_FACTS,
    CountryRule,
    ScoringConfig,
    country_allowed,
    drop_reason,
    future_refusal,
    load_scoring,
    normalize,
    sponsorship_label,
    trim_jd,
    with_refusal,
    word_count,
    wrap,
    years_required,
)
from applyourself.shared import clock, paths, schemas, verticals
from applyourself.shared.parquet_io import locked, read_parquet, schema_version, write_parquet
from applyourself.state_io import load_state_index

SCORED_COLUMNS: list[str] = [
    "job_id",
    "fit_score",  # 0-100: the judge's fit less the lane's penalty, or 0
    "fit_subscores",  # JSON: {fit, penalty, level, years}; older rows {title, skills, seniority, domain}
    "vertical",  # a profile/verticals.yaml name | "" (no lane)
    "sponsorship_label",
    "sponsorship_evidence",
    "reasoning",
    "keywords_to_mirror",  # list[str]
    "suggested_action",
    "scored_at",
    "scored_by_model",
    "judge_answer",  # JSON: the judge's raw answer; empty on a legacy row
    "judge_facts",  # JSON: {years, words, contract, future_refusal} read from the JD at start
    "judge_input_hash",  # input_hash() of what was judged; empty on a legacy row
    "judge_profile_hash",  # profile_hash() of the digest the judge read; empty before v2
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

# What finish keeps of a slice's facts in judge_facts; derive reads them.
FACT_KEYS = ("years", "words", "contract", "future_refusal")

YEARS_GAP_LOG = 3  # regex and judge years this far apart are printed by finish

SLICE_ROWS = 10
SLICE_BYTES = 60_000
JUDGE_SLICES = 5
JUDGE_BYTES = 300_000

JUDGE_AGENT = paths.REPO_ROOT / ".claude" / "agents" / "score-judge.md"
SHARED_RUBRIC = paths.REPO_ROOT / ".claude" / "shared" / "scoring_rubric.md"
EMPLOYER_RUBRIC = paths.REPO_ROOT / ".claude" / "shared" / "scoring_employer.md"
# Answer fields no score reads, left out of input_hash so adding one stales nothing.
UNHASHED_FIELDS = ("employer",)


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


def is_v2(answer: Any) -> bool:
    """A stored judge answer of today's form (it grades `fit`)."""
    return isinstance(answer, dict) and "fit" in answer


def view(
    scored_path: Path | None = None,
    cfg: ScoringConfig | None = None,
    rule: CountryRule | None = None,
) -> pd.DataFrame:
    """Every live score, the one way to read scored.parquet. A row holding
    today's judge answer is derived again under today's rules (`basis`
    judged); an older answer keeps its stored score (`basis` v1), and so does
    a row with none (`basis` legacy). Filter drops are not scores and are
    left out."""
    df = read_scored(scored_path or paths.SCORED)
    df = df[~df["scored_by_model"].astype(str).str.startswith("filter:")].reset_index(drop=True)
    answers = [json.loads(v) if isinstance(v, str) and v else None for v in df["judge_answer"]]
    raw = [is_v2(a) for a in answers]
    basis = [
        "legacy" if a is None else "judged" if v2 else "v1"
        for a, v2 in zip(answers, raw, strict=True)
    ]
    if any(raw):
        cfg = cfg or load_scoring()
        if rule is None:
            from applyourself.discovery.config import load_config

            rule = CountryRule.from_discovery(load_config())
        lanes = verticals.get_config().verticals
        cols = {c: df[c].astype(object).tolist() for c in DERIVED_COLUMNS}
        for i, r in enumerate(cast(list[dict[str, Any]], df.to_dict("records"))):
            lane = lanes.get(r["vertical"])
            if not raw[i]:
                continue
            if lane is None:
                basis[i] = "legacy"  # a lane no longer configured keeps its stored score
                continue
            d = derive(
                cast(dict[str, Any], answers[i]),
                lane,
                json.loads(r["judge_facts"]),
                cfg,
                rule,
                r["scored_by_model"],
                r["scored_at"],
            )
            for c in DERIVED_COLUMNS:
                cols[c][i] = d[c]
        df = df.assign(**{c: pd.Series(v, index=df.index, dtype=object) for c, v in cols.items()})
        df["fit_score"] = df["fit_score"].astype("float64")
    df["basis"] = basis
    return df[VIEW_COLUMNS]


def judged_employers(scored_path: Path | None = None) -> dict[str, str | None]:
    """job_id -> the score judge's `employer` (a name, or None when the
    posting names none), for the rows whose answer has the key."""
    path = scored_path or paths.SCORED
    if not path.exists():
        return {}
    return dict(_employers(path, path.stat().st_mtime))


@functools.cache
def _employers(path: Path, mtime: float) -> tuple[tuple[str, str | None], ...]:
    scored = read_scored(path)
    out = []
    for jid, raw in zip(scored["job_id"], scored["judge_answer"], strict=True):
        answer = json.loads(raw) if isinstance(raw, str) and raw else None
        if isinstance(answer, dict) and "employer" in answer:
            name = answer["employer"]
            out.append((str(jid), name if isinstance(name, str) and name.strip() else None))
    return tuple(out)


def employer_for(job_id: str, saved: str, scored_path: Path | None = None) -> str | None:
    """The company to write for a role: the judge's employer when it differs
    from the saved name, the saved name when the judge gave none or the same
    one, None when the judge found that the posting names no hiring company."""
    from applyourself.shared.identity import normalize_company

    table = judged_employers(scored_path)
    if job_id not in table:
        return saved
    name = table[job_id]
    if name is None:
        return None
    return saved if normalize_company(name) == normalize_company(saved) else name


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
    score: float,
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
        "fit_score": float(score),
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
        "judge_profile_hash": "",
    }


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


def input_hash(packet: str, jd: str) -> str:
    """What a judgment was made from: the lane's packet without the digest
    (`packet_text(..., digest="", for_hash=True)`) and the trimmed JD."""
    return hashlib.sha1(f"{packet}\0{jd}".encode()).hexdigest()[:16]


def profile_hash(digest: str) -> str:
    """The candidate digest a judgment read, apart from input_hash: a profile
    edit is reported, never a reason to re-judge on its own."""
    return hashlib.sha1(digest.encode()).hexdigest()[:16]


def is_current(run: dict[str, Any]) -> bool:
    """An open run staged for today's answer form."""
    return bool(run.get("schema_version", 1) >= 2 and run.get("form", "v2") == "v2")


def schema_text(model: type[BaseModel], leave_out: tuple[str, ...] = ()) -> str:
    """The packet's answer-schema section for one answer model, without the
    `leave_out` fields."""
    schema = model.model_json_schema()
    for name in leave_out:
        schema["properties"].pop(name, None)
        if name in schema.get("required", []):
            schema["required"].remove(name)
    return (
        "## Answer schema\n\nEach object in your JSON array matches this JSON Schema.\n\n```json\n"
        + json.dumps(schema, indent=1)
        + "\n```\n"
    )


def packet_text(
    lane: verticals.Vertical,
    cfg: ScoringConfig,
    digest: str | None = None,
    *,
    for_hash: bool = False,
) -> str:
    """What every judge of the lane reads first, always in this order: the
    shared rubric, the employer section, the candidate digest, the lane, its
    calibration examples and the answer schema. `digest`:
    `score_packet.digest(cfg)`, when the caller already built it. `for_hash`:
    the text `input_hash` reads, without the employer section and field,
    which no score depends on."""
    parts = [
        f"# Scoring packet — lane {lane.name} ({lane.display_name})\n",
        SHARED_RUBRIC.read_text(encoding="utf-8"),
        f"`keywords_to_mirror`: up to {cfg.keywords_max}.\n",
        "" if for_hash else EMPLOYER_RUBRIC.read_text(encoding="utf-8"),
        score_packet.digest(cfg) if digest is None else digest,
        f"## Lane: {lane.display_name}\n\n"
        + (paths.VERTICALS_DIR / lane.name / "rubric.md").read_text(encoding="utf-8"),
        score_packet.examples_text(lane.name),
        schema_text(JudgeAnswer, UNHASHED_FIELDS if for_hash else ()),
    ]
    return "\n".join(p for p in parts if p)


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


def first_seen(
    store_path: Path | None, job_ids: set[str], newest: bool = False
) -> dict[str, float]:
    """job_id -> its earliest (`newest`: latest) first_seen in the jobs store,
    as a POSIX time; {} with no store."""
    if store_path is None or not Path(store_path).exists() or not job_ids:
        return {}
    store = read_parquet(store_path, schemas.STORE_SCHEMA_VERSION, columns=["job_id", "first_seen"])
    store = store[store["job_id"].isin(job_ids)]
    by_job = pd.to_datetime(store["first_seen"]).groupby(store["job_id"])
    first = (by_job.max() if newest else by_job.min()).dropna()
    return {str(j): t.timestamp() for j, t in first.items()}


_SENTENCE_END = re.compile(r"[.;\n]+")
_NOT_TOKEN = re.compile(r"[^\w+#]+")


def _sentences(text: str) -> list[str]:
    """The JD's sentences, each lowercased with only its word tokens (+ and #
    kept, so C++ and 3+ stay themselves), in sorted order: the same list means
    the text was only reformatted or its sentences moved."""
    out = (" ".join(_NOT_TOKEN.split(s.lower())).strip() for s in _SENTENCE_END.split(text))
    return sorted(s for s in out if s)


def same_jd_copies(
    rows: list[dict[str, Any]],
    hashes: dict[str, str],
    packets: dict[str, str],
    store_path: Path | None,
    cfg: ScoringConfig,
) -> set[str]:
    """The ids among changed `rows` whose JD only reformats the judged one: a
    stored posting from the row's own source whose JD gives the stored hash
    under today's packet, with the same sentences once boilerplate is trimmed
    (their order, case, punctuation and whitespace aside). Any other change,
    such as words swapped inside a sentence, re-judges."""
    if not rows or store_path is None or not Path(store_path).exists():
        return set()
    ids = {r["job_id"] for r in rows}
    store = read_parquet(
        store_path, schemas.STORE_SCHEMA_VERSION, columns=["job_id", "source", "jd_text"]
    )
    store = store[store["job_id"].isin(ids)]
    postings: dict[str, list[tuple[str, str]]] = {}
    for jid, source, jd in zip(store["job_id"], store["source"], store["jd_text"], strict=True):
        postings.setdefault(jid, []).append((source, jd))
    out = set()
    for r in rows:
        for source, jd in postings.get(r["job_id"], []):
            judged = trim_jd(jd, cfg)
            if (
                source != r.get("source")
                or input_hash(packets[r["vertical"]], judged) != hashes[r["job_id"]]
            ):
                continue
            if _sentences(judged) == _sentences(r["_jd"]):
                out.add(r["job_id"])
            break
    return out


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
    newest_first_seen: bool = False,
) -> dict[str, Any]:
    """Stage a run. Returns {"dropped": {code: n}, "first_dropped": {code: n},
    "judge": n, "lines": [...], "resumed": bool}. A dropped row is not stored,
    and loses any score it had. `first_dropped` counts only the drops the
    latest discovery run saw first (all of them without `store_path`).
    `stale_only` takes only judged rows whose input hash changed; a row with
    no hash is never stale; `profile_changed` counts the judged rows whose
    digest differs from today's. `count_only` returns the counts and writes
    nothing. An open run is resumed when no selector is given, only its
    `vertical` slices when that is the one selector, refused otherwise. A run staged for the old judge is closed instead: its answered
    slices are kept (`basis: v1`), the rest restaged under today's packet
    (`restaged`)."""
    run = load_run(staging)
    owed: set[str] = set()
    kept = 0
    if run is not None and not is_current(run):
        if count_only:
            raise RunOpen("a /score run for the old judge is open: run `/score` to restage it")
        from applyourself import score_ab
        from applyourself.discovery.config import load_config

        rows, owed = score_ab.close_v1_run(
            run, staging, cfg, CountryRule.from_discovery(load_config()), now or clock.now()
        )
        kept = merge_rows(scored_path, rows)
        for f in staging.glob("*"):
            f.unlink()
        run = None
    if run is not None:
        if rescore or stale_only or job_ids:
            raise RunOpen("a /score run is still open: run `/score` to finish it first")
        if vertical:
            run = {
                **run,
                "slices": {n: m for n, m in run["slices"].items() if m["vertical"] == vertical},
            }
        return {
            "dropped": {},
            "first_dropped": {},
            "judge": sum(len(m["job_ids"]) for m in run["slices"].values()),
            "lines": judge_lines(run, staging),
            "resumed": True,
            "restaged": 0,
            "kept_v1": 0,
            "profile_changed": 0,
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
    if owed:
        whole = read_parquet(clean_path, schemas.CLEAN_SCHEMA_VERSION)
        whole = whole[whole["job_id"].isin(owed - set(clean["job_id"]))]
        clean = pd.concat([clean, whole], ignore_index=True) if len(whole) else clean
    profiles = {
        j: h
        for j, h in zip(stored["job_id"], stored["judge_profile_hash"], strict=True)
        if j in hashes and isinstance(h, str) and h  # unknown before v2: not counted
    }

    dropped: set[str] = set()
    keep: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    codes: dict[str, str] = {}
    packets: dict[str, str] = {}
    hashed: dict[str, str] = {}  # each lane's packet without the digest
    digest: str | None = None
    profile = ""
    changed = 0
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
            if digest is None:
                digest = score_packet.digest(cfg, now.date())
                profile = profile_hash(score_packet.digest(cfg, now.date(), open_lengths=False))
            packets[lane] = packet_text(vcfg.verticals[lane], cfg, digest)
            hashed[lane] = packet_text(vcfg.verticals[lane], cfg, "", for_hash=True)
        row["_hash"] = input_hash(hashed[lane], row["_jd"])
        if row["job_id"] in profiles and profiles[row["job_id"]] != profile:
            changed += 1
        if stale_only and row["_hash"] == hashes.get(row["job_id"]):
            continue
        keep.append(row)
    if stale_only:
        copies = same_jd_copies(keep, hashes, hashed, store_path, cfg)
        keep = [r for r in keep if r["job_id"] not in copies]
    extra = {"restaged": len(owed), "kept_v1": kept, "profile_changed": changed}
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
            **extra,
        }
    remove_rows(scored_path, dropped)

    seen = first_seen(store_path, {r["job_id"] for r in keep}, newest_first_seen)
    keep.sort(  # newest first, so a judge stopped early has judged the newest
        key=lambda r: (
            -seen.get(r["job_id"], float("-inf")),
            str(r.get("company") or "").lower(),
            str(r.get("title") or "").lower(),
            r["job_id"],
        )
    )
    if not keep:
        return {
            "dropped": counts,
            "first_dropped": first,
            "judge": 0,
            "lines": [],
            "resumed": False,
            **extra,
        }
    run = stage_rows(staging, keep, packets, cfg, now, profile=profile)
    return {
        "dropped": counts,
        "first_dropped": first,
        "judge": len(keep),
        "lines": judge_lines(run, staging),
        "resumed": False,
        **extra,
    }


def stage_rows(
    staging: Path,
    rows: list[dict[str, Any]],
    packets: dict[str, str],
    cfg: ScoringConfig,
    now: datetime,
    *,
    profile: str = "",
    form: str = "v2",
) -> dict[str, Any]:
    """Write one packet per lane, the slices and run.json for `rows` (each
    with its trimmed `_jd` and `_hash`, in judging order) into `staging`.
    `profile`: the digest's profile_hash; `form`: the answer form the packets
    ask for. Returns the run."""
    staging.mkdir(parents=True, exist_ok=True)
    run: dict[str, Any] = {
        "schema_version": schemas.SCORE_STAGING_SCHEMA_VERSION,
        "form": form,
        "model": _judge_model(),
        "started": now.isoformat(),
        "slices": {},
    }
    for lane in verticals.get_config().verticals.values():
        lane_rows = [r for r in rows if r["vertical"] == lane.name]
        if not lane_rows:
            continue
        (staging / f"packet_{lane.name}.md").write_text(packets[lane.name], encoding="utf-8")
        for n, part in enumerate(_slices(lane_rows), 1):
            name = f"{lane.name}_{n:03d}"
            (staging / f"out_{name}.json").unlink(missing_ok=True)  # never a leftover answer
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
                        # the whole posting, as the hard-rule drops read it
                        "future_refusal": future_refusal(
                            f"{r.get('title') or ''}\n{r.get('jd_text') or ''}"
                        ),
                        "hash": r["_hash"],
                        "profile_hash": profile,
                        # everything the judge is shown, so a quote from the title or company counts
                        "jd": normalize(
                            f"{r.get('title') or ''}\n{r.get('company') or ''}\n{r['_jd']}"
                        ),
                    }
                    for r in part
                },
            }
    _save_run(staging, run)
    return run


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


def _employer_required(schema: dict[str, Any]) -> None:
    schema["properties"]["employer"].pop("default", None)
    schema.setdefault("required", []).append("employer")


class JudgeAnswer(BaseModel):
    """One role in a judge's answer file, facts first and the grade after.
    The scoring rubric says what each field means."""

    model_config = ConfigDict(extra="forbid", strict=True, json_schema_extra=_employer_required)

    job_id: str
    in_lane: bool
    level: Literal[verticals.LEVELS]  # type: ignore[valid-type]  # pydantic expands the tuple
    years: Annotated[int, Field(ge=0, le=50)]
    country: str
    sponsorship: Annotated[list[Literal[SPONSORSHIP_FACTS]], Field(min_length=1)]  # type: ignore[valid-type]  # pydantic expands the tuple
    evidence: str
    blocker: str | None
    fit: Annotated[int, Field(ge=0, le=100)]

    @field_validator("blocker", mode="before")
    @classmethod
    def _no_blank_blocker(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v

    keywords_to_mirror: list[str]
    reasoning: Annotated[str, Field(pattern=r"\S")]
    # The hiring company the posting names, or null. Optional here so a slice
    # staged before the field still checks; the packet lists it as required.
    employer: str | None = None

    @model_validator(mode="after")
    def _evidence(self) -> JudgeAnswer:
        if "silent" in self.sponsorship and len(self.sponsorship) > 1:
            raise ValueError("sponsorship cannot mix silent with other statements")
        if self.sponsorship != ["silent"] and not self.evidence.strip():
            raise ValueError(f"evidence must quote the JD for {', '.join(self.sponsorship)}")
        return self


def check_answer(
    answer: Any, meta: dict[str, Any], model: type[BaseModel] = JudgeAnswer
) -> list[str]:
    """Problems with one judge output file, [] if it can be merged. `model`:
    the answer form the slice was staged for."""
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
            model.model_validate(a)
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
    return errs


def checked_employer(a: dict[str, Any], jd: str | None) -> dict[str, Any]:
    """The answer with an `employer` name the posting does not show (its
    title, company line or JD, as staged in `jd`) left out, as if the judge
    had not answered it; a null employer stays."""
    name = a.get("employer")
    if not isinstance(name, str):
        return a
    letters = _letters(name)
    if len(letters) >= 2 and (jd is None or letters in _letters(jd)):
        return {**a, "employer": name.strip()}
    return {k: v for k, v in a.items() if k != "employer"}


def derive(
    a: dict[str, Any],
    lane: verticals.Vertical,
    facts: dict[str, Any],
    cfg: ScoringConfig,
    rule: CountryRule,
    model: str,
    now: datetime,
) -> dict[str, Any]:
    """The stored row for one judge answer. The judge reads the facts and
    grades fit; every rule is applied here. The score is 0 when the role is
    out of lane, ineligible, outside the lane's countries, asks the lane's
    `years_zero_at` years or more, or has a blocker; otherwise it is fit
    (capped at `jd_quality.fit_cap` for a short JD, when set) less the larger
    of the level and years penalties. A short JD is tagged either way."""
    rules = lane.score
    reasoning = a["reasoning"].strip()
    refusal = str(facts.get("future_refusal") or "")
    label = sponsorship_label(with_refusal(a["sponsorship"], refusal), a["country"], cfg)
    years = a["years"]  # the regex only reports a gap; the judge's reading counts
    zero = label == "ineligible"
    if not a["in_lane"]:
        reasoning += " [out of lane]"
        zero = True
    if years >= rules.years_zero_at:
        reasoning += f" [{years}+ yrs]"
        zero = True
    if not country_allowed(a["country"], lane.name, rule):
        reasoning += f" [out of scope: {a['country']}]"
        zero = True
    blocker = (a["blocker"] or "").strip()  # a blank one is none
    if blocker:
        reasoning += f" [blocked: {blocker}]"
        zero = True
    fit = a["fit"]
    if facts["words"] < cfg.min_words:
        if cfg.fit_cap is not None:
            fit = min(fit, cfg.fit_cap)
        reasoning += " [short JD]"
    if facts["contract"] and "[contract role]" not in reasoning:
        reasoning += " [contract role]"
    penalty = rules.penalty(a["level"], years)
    total = 0 if zero else max(0, fit - penalty)
    action = (
        "skip"
        if zero
        else "tailor"
        if total >= cfg.tailor_min
        else "manual-review"
        if total >= cfg.review_min
        else "skip"
    )
    return _row(
        a["job_id"],
        lane.name,
        total,
        {"fit": fit, "penalty": penalty, "level": a["level"], "years": years},
        label,
        a["evidence"].strip() or refusal,
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
    if not is_current(run):
        return {
            "merged": 0,
            "bad": {
                "run": [
                    "staged for the old judge: `score start` keeps its answers, restages the rest"
                ]
            },
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
        errs = check_answer(answer, meta)
        if errs:
            bad[name] = errs
            continue
        for a in answer:
            facts = meta["facts"][a["job_id"]]
            a = checked_employer(a, facts.get("jd"))
            row = derive(a, lane, facts, cfg, rule, run["model"], now)
            row["judge_answer"] = json.dumps(a)
            row["judge_facts"] = json.dumps({k: facts[k] for k in FACT_KEYS if k in facts})
            row["judge_input_hash"] = facts.get("hash", "")
            row["judge_profile_hash"] = facts.get("profile_hash", "")
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
