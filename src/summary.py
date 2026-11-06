"""`summary` — the morning summary.md, written from the night's records.

One file at the repo root, rewritten every night. Nothing here calls an LLM: each
section is a pure function of records already on disk. "Tonight" is everything
since the pipeline run started (the stamp in `logs/pipeline_<stamp>/`), not a
calendar date, because a night crosses midnight.

  uv run summary [--run-dir logs/pipeline_<stamp>] [--since ISO] [--out PATH]
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

from src import night, state_io
from src.apply import sheet
from src.discovery import digest
from src.discovery.store import load_maybe_duplicates
from src.material import folders as app_folders
from src.score_store import view
from src.shared import clock, paths
from src.shared.files import atomic_write_text

# One short "what to do" line per section, written only into summary.md (the
# session's copy) and only when the section has something to act on.
CUES = {
    "discovery": "→ Lane errors are in `jobs/runs/<run_id>.json`. Re-run one lane with `uv run discover --source <name>`.",
    "dupes": (
        "→ Open both postings; if they are one role, `uv run track <copy> skip` the one not in "
        "play. Nothing was merged."
    ),
    "scoring": "→ Run `/score` to resume the open run.",
    "score_limit": (
        "→ Usage limit: nothing is lost. The next night resumes the owed slices, or run `/score` "
        "once the limit resets."
    ),
    "synonyms": (
        "→ Per question id: `uv run synonyms answer <id> yes|no` (a yes means the user writes the bullet or "
        'skill block). Approve a declined keyword: `uv run synonyms approve "<keyword>"`. Undo an added '
        'phrase: `uv run synonyms undo <target> "<phrase>"`.'
    ),
    "tailoring": (
        "→ PDF missing: `uv run app build resume <id>` (no model); if Word is stuck, ask the user to quit "
        "Word. Posting gone: `uv run track <id> skip`."
    ),
    "ready": (
        "→ Show the role and its resume (`uv run app dir <id>`), then run its `uv_run_command` (`uv run apply "
        "sheet --list Submittable`; the sheet's column holds it). Only with the user's typed OK; one submit "
        "per company per run."
    ),
    "apply_paths": (
        "→ Manual / No apply URL: apply by hand (`uv run apply sheet --list 'Manual apply'`). "
        "`uv run apply resolve-urls --dry-run` lists roles whose company has a board /apply drives; "
        "`--apply` looks the title up there (network, only with the user's OK)."
    ),
    "apply_expired": "→ Expired: `uv run track <id> skip`.",
    "apply_failed": "→ Failed: `uv run apply plan <id> --questions` shows what the form needs now.",
    "apply_unsent": "→ Unsent: the click fired no submit request or the board refused it; check the board by hand.",
    "apply_rejected": (
        "→ Rejected by board: the board declined the application; clicking again will not change it. "
        "Read its message on the board."
    ),
    "sheet": "→ If the posting is gone: `uv run track <id> skip`, and it leaves the sheet.",
    "answers": (
        '→ For each answer the user approves: `uv run apply teach --question-id <bank id> --label "<label>" '
        '--value "<value>" --job <id>` (one per distinct label). Then `uv run apply triage --fill --job-id <id> '
        "...` for the roles it unblocks (no model, never submits), then `uv run apply sheet`."
    ),
    "missing": (
        "→ Set `salary` or `start_in_days` on the country's row in the answer bank "
        "(`work_authorization.countries`), then `uv run apply plan <id> --questions`."
    ),
    "missing_other": (
        "→ Any other missing value: answer it in `/apply <id>`; the session writes the entry and the "
        "user confirms it."
    ),
}
CUES = {name: "**FIX:** " + text.removeprefix("→ ") for name, text in CUES.items()}

STAGES = ("discover", "score", "synonyms", "tailor", "apply_prep", "cleanup")
ERRORS_SHOWN = 5
LABEL_CHARS = 110
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}_\d{6}")


def _ts(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text))
    except ValueError:
        return None


def since_of(run_dir: Path | None) -> datetime:
    m = _STAMP.search(run_dir.name) if run_dir else None
    if m:
        return datetime.strptime(m.group(0), "%Y-%m-%d_%H%M%S")  # noqa: DTZ007 - run-dir stamps are local time
    return datetime.combine(clock.today(), datetime.min.time())


def latest_run_dir() -> Path | None:
    dirs = sorted(
        d
        for d in paths.LOGS.glob("pipeline_*")
        if d.is_dir() and re.fullmatch(r"pipeline_" + _STAMP.pattern, d.name)
    )
    return dirs[-1] if dirs else None


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


def _took(seconds: int) -> str:
    if seconds >= 3600:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}"
    return f"{seconds // 60}m" if seconds >= 60 else f"{seconds}s"


def _cut(text: str, n: int = LABEL_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _n(count: int, word: str, plural: str = "") -> str:
    return f"{count} {word if count == 1 else (plural or word + 's')}"


def _shown(items: list[str], limit: int = ERRORS_SHOWN) -> list[str]:
    extra = len(items) - limit
    return [*items[:limit], f"…and {extra} more"] if extra > 0 else items


def _run_json(run_dir: Path | None) -> dict | None:
    return night.load(run_dir) if run_dir else None


def stage_lines(run_dir: Path | None) -> dict[str, dict]:
    """{stage: {code, seconds, status, deferred, detail, usage_limit}} of the stages that
    ran, from the run folder's run.json (src/night.py); a night from before it, from
    summary.txt, where a line with no status= takes it from its code. An older night
    with no usage_limit field reads it from the detail text."""
    out: dict[str, dict] = {}
    if (run := _run_json(run_dir)) is not None:
        for name, st in run.get("stages", {}).items():
            if st.get("rc") is not None and st.get("status") in ("ok", "partial", "fail"):
                out[name] = {
                    "code": st["rc"],
                    "seconds": st.get("seconds", 0),
                    "status": st["status"],
                    "deferred": (st.get("counts") or {}).get("deferred", 0),
                    "detail": st.get("detail", ""),
                    "usage_limit": bool(
                        st.get("usage_limit", _old_usage_limit(st.get("detail", "")))
                    ),
                }
        return out
    for line in _read_lines(run_dir / "summary.txt") if run_dir else []:  # before run.json
        m = re.match(
            r"(\w+) code=(\d+) seconds=(\d+)(?: status=(\w+))?(?: deferred=(\d+))?\s*(.*)", line
        )
        if m:
            code = int(m[2])
            out[m[1]] = {
                "code": code,
                "seconds": int(m[3]),
                "status": m[4] or ("ok" if code == 0 else "fail"),
                "deferred": int(m[5] or 0),
                "detail": m[6],
                "usage_limit": _old_usage_limit(m[6]),
            }
    return out


def tailor_lines(run_dir: Path | None) -> list[str]:
    """`<job_id> ok|fail|deferred` per role tailor took, from run.json, else tailor_results.txt."""
    if (run := _run_json(run_dir)) is not None:
        items = run.get("stages", {}).get("tailor", {}).get("items") or {}
        return [f"{j} {s}" for j, s in items.items()]
    return _read_lines(run_dir / "tailor_results.txt") if run_dir else []


# ------------------------------------------------------------------ sections


def run_section(stages: dict[str, dict]) -> list[str]:
    parts, failed, partial, deferred = [], [], [], []
    for name in STAGES:
        st = stages.get(name)
        if st is None:
            parts.append(f"{name} not run")
        elif st["code"] != 0:
            parts.append(f"{name} FAILED")
            failed.append(f"**{name} failed** (code {st['code']}) {st['detail']}".strip())
        elif st.get("status") == "partial":
            parts.append(f"{name} partial {_took(st['seconds'])}")
            partial.append(f"**{name} partial** {st['detail']}".strip())
        else:
            parts.append(f"{name} ok {_took(st['seconds'])}")
        if st and st.get("deferred"):
            deferred.append(f"{name} {st['deferred']}")
    lines = [*failed, *partial, "Stages: " + " · ".join(parts)]
    if deferred:
        lines.append(
            "Deferred (usage limit or nightly cap; next night takes them): " + ", ".join(deferred)
        )
    return lines


def spend_line(run_dir: Path | None) -> list[str]:
    """`Spend: $X (stage $Y · ...)` from run.json; none for a night from before it."""
    run = _run_json(run_dir)
    if run is None or "cost_usd" not in run:
        return []
    stages = run.get("stages", {})
    parts = [f"{n} ${c:.2f}" for n, st in stages.items() if (c := st.get("cost_usd") or 0) > 0]
    line = f"Spend: ${run['cost_usd']:.2f}" + (f" ({' · '.join(parts)})" if parts else "")
    capped = [n for n, st in stages.items() if st.get("detail") == night.SPEND_CAP]
    if capped:
        line += f"; spend cap reached, deferred: {', '.join(capped)}"
    return [line]


def discovery_section(reports: list[dict], since: datetime) -> list[str]:
    stamp = since.strftime("%Y-%m-%d_%H%M")
    tonight = [r for r in reports if str(r.get("run_id", "")) >= stamp]
    if not tonight:
        return ["No discovery report for this run."]
    new = {j for r in tonight for j in r.get("new_job_ids") or []}
    live = tonight[0].get("view", {}).get("clean", "?")
    errors: list[str] = []
    for r in tonight:
        for lane, d in (r.get("lanes") or {}).items():
            if d.get("status") == "crashed":
                last = (str(d.get("crash", "")).strip().splitlines() or [""])[-1]
                errors.append(f"{lane} crashed: {last}")
                continue
            errors += [f"{lane}: {str(e)[:100]}" for e in d.get("errors") or []]
            if d.get("truncated"):
                errors.append(f"{lane}: cut short by the deadline")
            http = d.get("http") or {}
            for code in ("403", "429"):
                if http.get(code):
                    errors.append(f"{lane}: {http[code]} x HTTP {code}")
    return [f"{len(new)} new postings, {live} live.", *_errors(errors)]


DUPES_SHOWN = 10


def duplicates_section(rows: list[dict], states: dict[str, dict]) -> list[str]:
    """Tonight's maybe-duplicate pairs (never the backfill), tracked first."""
    rows = [r for r in rows if not str(r.get("reason", "")).startswith("backfill_")]
    if not rows:
        return ["None tonight."]

    def side(jid: str, company: str) -> str:
        st = state_io.shown_state(states[jid]) if jid in states else None
        return f"`{jid}` {company}" + (f" ({st})" if st else "")

    rows = sorted(
        rows,
        key=lambda r: (
            -((r["job_id"] in states) + (r["other"] in states)),
            -(r.get("jd_score") or 0),
            r["pair"],
        ),
    )
    lines = [
        f"- {side(r['job_id'], r['company'])} / {side(r['other'], r['other_company'])} — "
        f"{_cut(r['title'], 60)} (JD {r.get('jd_score', 0):g}, {r.get('reason', '')})"
        for r in rows
    ]
    return [f"{_n(len(rows), 'pair')} not merged:", *_shown(lines, DUPES_SHOWN)]


def _errors(errors: list[str], cap: bool = True) -> list[str]:
    if not errors:
        return ["Errors: none"]
    return ["Errors:", *[f"- {e}" for e in (_shown(errors) if cap else errors)]]


def scoring_section(
    scored: pd.DataFrame | None,
    since: datetime,
    detail: str,
    open_slices: int,
    code: int | None,
    usage_limit: bool = False,
) -> list[str]:
    lines = []
    if scored is not None and len(scored):
        new = scored[pd.to_datetime(scored["scored_at"]) >= since]
        judged = new[new["scored_by_model"].astype(str).str.startswith("score-judge")]
        actions = judged["suggested_action"].value_counts()
        # score start's first-time drops; a drop is never stored
        dropped = dict(re.findall(r"\bfiltered_(\S+)=(\d+)", detail))
        if not len(judged) and not dropped:
            text = "Nothing new to score"
        else:
            text = f"{len(judged)} judged: " + ", ".join(
                f"{actions.get(a, 0)} {a}" for a in ("tailor", "manual-review", "skip")
            )
            if len(dropped):
                text += ". Filtered: " + ", ".join(f"{c} {n}" for c, n in dropped.items())
        keepers = re.search(r"keepers=(\d+)", detail)
        lines.append(text + (f". Keepers {keepers[1]}." if keepers else "."))
    else:
        lines.append("Nothing scored.")
    errors = []
    if open_slices:
        errors.append(f"score run still open: {open_slices} slices owed (next /score resumes it)")
    if code:
        errors.append(detail if usage_limit else f"stage exited {code}: {detail}")
    return [*lines, *_errors(errors)]


def _old_usage_limit(detail: str) -> bool:
    """A night from before run.json's usage_limit field: its detail says so."""
    return detail.startswith("usage limit hit")


def synonyms_section(added: list[dict], declined: list[dict], opened: list[dict]) -> list[str]:
    by_target: dict[str, list[str]] = {}
    for row in added:
        by_target.setdefault(row["target"], []).append(row["phrase"])
    lines = [f"**Added ({len(added)})**" if added else "**Added:** none"]
    lines += [f"- {t}: {', '.join(p)}" for t, p in by_target.items()]
    lines += [
        "",
        f"**Declined, shown once ({len(declined)})**" if declined else "**Declined:** none new",
    ]
    lines += [f"- {d['keyword']}: {d.get('reason', '')}" for d in declined]
    lines += ["", f"**Questions for you ({len(opened)})**" if opened else "**Questions:** none"]
    lines += [
        f"- [{o['id']}] Did you work on {o['thing']} during {o['context']}? "
        f"You may have used it via {o['via']}."
        for o in opened
    ]
    return lines


def tailoring_section(
    total: int, unresolved: dict[str, str], names: dict[str, str], detail: str, code: int | None
) -> list[str]:
    """`unresolved`: job_id -> why it has no finished resume now. Every one is listed."""
    if not total and not unresolved:
        head = detail or "Nothing to tailor."
    else:
        head = f"{total - len(unresolved)} of {total} tailored."
    errors = [f"{j} {names.get(j, '')}".strip() + f": {why}" for j, why in unresolved.items()]
    if code:
        errors.append(f"stage exited {code}: {detail}")
    return [head, *_errors(errors, cap=False)]


def tailor_reason(job: str, run_dir: Path | None, live: set[str]) -> str:
    """Why a role has no finished resume now; "" when it has one."""
    folder = app_folders.role_folder(job)
    if folder:
        has = lambda pat: any(folder.glob(pat))  # noqa: E731
        if has("*_Resume.docx") and has("*_Resume.pdf"):
            state = state_io.load_state(state_io.state_path_for(paths.PIPELINE, job)) or {}
            if folder == app_folders.built_folder(state):
                return ""
            return "resume built but not recorded: it renders to more than one page"
        if has("*_Resume.docx"):
            return "resume built, PDF missing (Word conversion failed)"
    if job not in live:
        return "no longer in clean.parquet: the posting is gone"
    said = run_dir / f"tailor_{job}.json" if run_dir else None
    if said and said.is_file():
        try:
            text = str(json.loads(said.read_text(encoding="utf-8")).get("result") or "")
        except ValueError:
            text = ""
        if text.strip():
            return "tailor stopped: " + _cut(text, 220)
    return "no resume built and no reason recorded"


def open_items(snapshots: list[tuple[str, dict]]) -> dict:
    """What is still open after tonight's /apply, in three kinds, plus a tally per role:
    guesses (one yes covers a bank question), new questions, missing values."""
    guesses: dict[str, dict] = {}
    asks: dict[str, dict] = {}
    facts: dict[str, dict] = {}
    by_role: dict[str, dict] = {}
    roles: dict[str, dict] = {}

    def ask(job, label, options):
        a = asks.setdefault(label, {"label": label, "roles": [], "options": options})
        if job not in a["roles"]:
            a["roles"].append(job)
        by_role[job]["asks"] += 1
        roles[job]["asks"].append({"label": label, "options": options})

    for job, plan in snapshots:
        country = (plan.get("work_authorization") or {}).get("job_country") or ""
        by_role[job] = {"guesses": 0, "asks": 0, "facts": 0}
        roles[job] = {
            "company": plan.get("company") or "",
            "title": plan.get("title") or "",
            "url": plan.get("form_url") or "",
            "asks": [],
            "facts": [],
        }
        for u in plan.get("draftable") or []:
            ask(job, u["label"], u.get("options") or [])
        for u in plan.get("unmapped") or []:
            if not u.get("required"):
                continue
            if u.get("tier") == "C":
                ask(job, u["label"], u.get("options") or [])
                continue
            why = u.get("reason") or u["label"]
            f = facts.setdefault(why, {"reason": why, "roles": []})
            if job not in f["roles"]:
                f["roles"].append(job)
            by_role[job]["facts"] += 1
            roles[job]["facts"].append({"label": u["label"], "reason": why})
        for f in plan.get("fields") or []:
            source = str(f.get("source", ""))
            if "~" not in source:
                continue
            qid = source.removeprefix("bank:").split("~")[0]
            raw = f.get("value")
            value = ", ".join(map(str, raw)) if isinstance(raw, (list, tuple)) else str(raw)
            label = " ".join(str(f["label"]).replace("✱", "").replace("*", "").split())
            g = guesses.setdefault(qid, {"qid": qid, "label": label, "values": {}})
            if label and len(label) < len(g["label"]):
                g["label"] = label
            v = g["values"].setdefault(
                value, {"roles": [], "countries": [], "lanes": [], "labels": {}}
            )
            named = v["labels"].setdefault(label, [])
            if job not in named:
                named.append(job)
            if job not in v["roles"]:
                v["roles"].append(job)
            if country and country not in v["countries"]:
                v["countries"].append(country)
            lane = Path(plan.get("out_dir") or "").parent.name
            if lane and lane not in v["lanes"]:
                v["lanes"].append(lane)
            by_role[job]["guesses"] += 1
    return {
        "guesses": list(guesses.values()),
        "asks": list(asks.values()),
        "facts": list(facts.values()),
        "by_role": by_role,
        "roles": roles,
    }


def _why(tally: dict) -> str:
    parts = []
    if tally["guesses"]:
        parts.append(_n(tally["guesses"], "guess", "guesses") + " to confirm")
    if tally["asks"]:
        parts.append(_n(tally["asks"], "question") + " to answer")
    if tally["facts"]:
        parts.append(_n(tally["facts"], "value") + " missing")
    return (
        ", ".join(parts)
        or "drafted answers wait in its folder (answers_override.json), nothing else open"
    )


def _why_waiting(items: dict, job: str) -> str:
    """A role re-read tonight says what is open; one that was not cannot say
    "nothing else open", so it says how to check."""
    tally = items["by_role"].get(job)
    if tally is None:
        return (
            "drafted answers wait in its folder (answers_override.json); not re-checked tonight: "
            f"`uv run apply plan {job} --questions --answers <that file>`"
        )
    return _why(tally)


_FACT_PATTERNS = (
    (
        r"salary: no figure for a role in (.+?) \(",
        "Salary for a role in {0}: no figure set. Add salary to its row under work_authorization.countries in the answer bank",
    ),
    (
        r"start date: no start_in_days set for (.+?) \(",
        "Start date for {0}: no start_in_days set. Add it under work_authorization.countries in the answer bank",
    ),
    (
        r"salary: the job's country could not be read",
        "Salary: the job's country could not be read from its location",
    ),
    (
        r"export control: yours to answer",
        "Export-control question: always yours to answer, by design",
    ),
    (
        r"(\w[\w ]*): none of \[.*?\] is offered",
        "The saved {0} answer is not one of the options offered",
    ),
)
#: The missing values a config key fills: the "missing" cue names those keys.
_CONFIG_FACT = re.compile(r"salary: no figure for |start date: no start_in_days ")


def _fact_text(reason: str) -> str:
    for pattern, text in _FACT_PATTERNS:
        m = re.match(pattern, reason)
        if m:
            return text.format(*m.groups())
    return _cut(reason, 130)


def _who(items: dict, job: str) -> str:
    name = (items.get("names") or {}).get(job)
    if name:
        return f"{job} {name}"
    r = items["roles"].get(job) or {}
    return f"{job} {r.get('company', '')} — {r.get('title', '')}".strip(" —") if r else job


_FOLLOW_UP = re.compile(
    r"^(if\s+(yes|so|applicable|you\s+(replied|answered|selected|said|checked|chose))"
    r"|please\s+(provide|describe|explain|specify))",
    re.I,
)


def _follow_up_guess(label: str, value: str) -> bool:
    """A free-text follow-up ("If yes, please provide details") the bank answered with Yes/No."""
    return bool(_FOLLOW_UP.match(label.strip())) and value.strip().lower() in ("yes", "no")


def _answers(items: dict) -> list[str]:
    """Everything still open, nothing cut, so a session needs no other file: every
    guess with its exact form labels and roles; every new question and missing
    value under its role."""
    lines = ["", "**Questions to add to the answer bank**"]
    guesses = items["guesses"]
    if guesses:
        lines += ["", f"**Guesses to confirm ({len(guesses)}): the bank matched these by keyword**"]
        for g in guesses:
            for value, d in g["values"].items():
                where = f" [{', '.join(d['countries'])}]" if d["countries"] else ""
                lines.append(f"- `{g['qid']}` ({_cut(g['label'], 60)}) → {value}{where}")
                for label, jobs in d["labels"].items():
                    flag = (
                        f' — **CHECK:** a follow-up that needs text, not "{value}"'
                        if _follow_up_guess(label, value)
                        else ""
                    )
                    lines.append(f'    - "{label}": {", ".join(jobs)}{flag}')
    per_role = {j: r for j, r in items["roles"].items() if r["asks"] or r["facts"]}
    if per_role:
        order = sorted(
            per_role, key=lambda j: (-(len(per_role[j]["asks"]) + len(per_role[j]["facts"])), j)
        )
        lines += ["", "**New questions and missing values, by role**"]
        for job in order:
            r = per_role[job]
            lines.append(f"- **{_who(items, job)}**" + (f" — {r['url']}" if r["url"] else ""))
            for a in r["asks"]:
                opts = f" (options: {', '.join(a['options'])})" if a["options"] else ""
                lines.append(f'    - ask: "{a["label"]}"{opts}')
            missing: dict[str, list[str]] = {}
            for f in r["facts"]:
                missing.setdefault(_fact_text(f["reason"]), []).append(f'"{_cut(f["label"], 90)}"')
            for why, labels in missing.items():
                lines.append(f"    - missing: {why} ({'; '.join(labels)})")
    lines += ["", CUES["answers"]]
    reasons = [f["reason"] for r in per_role.values() for f in r["facts"]]
    if any(_CONFIG_FACT.match(why) for why in reasons):
        lines += ["", CUES["missing"]]
    if any(not _CONFIG_FACT.match(why) for why in reasons):
        lines += ["", CUES["missing_other"]]
    return lines


def _apply_paths(rows: list[dict]) -> list[str]:
    """Submittable / manual / no-URL counts over the roles with a resume, and
    the commonest reasons, when any role cannot be submitted."""
    manual, no_url = (
        [r for r in rows if r["status"] == s] for s in ("Manual apply", "No apply URL")
    )
    if not manual and not no_url:
        return []
    can = [r for r in rows if r["status"] not in ("New", "Cleaned", "Manual apply", "No apply URL")]
    why: dict[str, int] = {}
    for r in manual + no_url:
        why[r["note"]] = why.get(r["note"], 0) + 1
    top = sorted(why.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    return [
        "",
        f"Apply paths: {len(can)} can submit, {len(manual)} manual, {len(no_url)} no URL",
        *[f"- {n}: {note}" for note, n in top],
        "",
        CUES["apply_paths"],
    ]


def _report_time(stem: str) -> datetime | None:
    try:
        return datetime.strptime(stem, "%Y-%m-%dT%H-%M-%S")  # noqa: DTZ007 - report stamps are local time
    except ValueError:
        return None


def apply_section(
    rows: list[dict],
    items: dict,
    unreadable: dict[str, str],
    outcomes: dict | None = None,
    since: datetime | None = None,
) -> list[str]:
    """`outcomes` (job_id -> (category, detail, report stem)) dates each error by
    the run report it came from; one older than `since` is listed apart."""

    def of(*statuses):
        return [r for r in rows if r["status"] in statuses]

    ready = [r["name"] for r in of("Submittable")]
    waiting = of("Needs your OK")
    lines = ["Ready to submit: " + (", ".join(ready) if ready else "none")]
    if ready:
        lines += ["", CUES["ready"]]
    lines += _apply_paths(rows)

    errors: dict[str, tuple[str, str]] = {}  # job -> (kind, why)
    errors.update({r["name"]: ("unsent", r["note"] or r["status"]) for r in of("Unsent")})
    errors.update(
        {r["name"]: ("rejected", r["note"] or r["status"]) for r in of("Rejected by board")}
    )
    errors.update(
        {r["name"]: ("failed", r["note"]) for r in of("Parked") if r["note"].startswith("failed:")}
    )
    errors.update(
        {
            r["name"]: ("expired", "posting gone (expired), mark it skip or leave it")
            for r in of("Expired")
        }
    )
    errors.update(
        {j: ("expired" if "expired" in why else "failed", why) for j, why in unreadable.items()}
    )

    def when(job: str) -> datetime | None:
        stem = (outcomes or {}).get(job, ("", "", ""))[2]
        at = _report_time(stem) if job not in unreadable else None
        return at if at is not None and since is not None and at < since else None

    fresh = {j: e for j, e in errors.items() if when(j) is None}
    older = {j: e for j, e in errors.items() if j not in fresh}
    lines += ["", *_errors([f"{j}: {why}" for j, (_, why) in fresh.items()], cap=False)]
    if older:
        lines += [
            "",
            f"**Still open from earlier nights ({len(older)})**",
            *[f"- {j} ({when(j):%Y-%m-%d}): {why}" for j, (_, why) in older.items()],
        ]
    for kind in ("expired", "failed", "unsent", "rejected"):
        if any(k == kind for k, _ in errors.values()):
            lines += ["", CUES[f"apply_{kind}"]]

    if waiting:
        lines += ["", f"**Needs your OK ({len(waiting)})**"]
        for r in waiting:
            lines.append(f"- {_who(items, r['name'])}: {_why_waiting(items, r['name'])}")

    if not (items["guesses"] or items["asks"] or items["facts"]):
        return [*lines, "", "**Answer bank:** nothing to add"]
    return [*lines, *_answers(items)]


def sheet_line(check: dict[str, list[str]]) -> list[str]:
    said = {
        "finished": "Applied or Closed rows still on it (should not happen)",
        "duplicates": "listed twice",
        "not_live": "no longer in the live listing, the posting may be gone",
        "wrong_status": "status differs from the records (run `uv run apply sheet`)",
    }
    problems = [f"{said[k]}: {', '.join(v)}" for k, v in check.items() if v]
    if not problems:
        return ["Sheet check: clean."]
    return ["Sheet check: " + "; ".join(problems)] + (
        ["", CUES["sheet"]] if check["not_live"] else []
    )


def _snapshots(since: datetime) -> tuple[list[tuple[str, dict]], dict[str, str]]:
    plans, bad = [], {}
    for path in sorted(paths.APPLICATIONS.glob("*/*/plan_after.json")):
        if datetime.fromtimestamp(path.stat().st_mtime) < since:  # noqa: DTZ006 - local, like `since`
            continue
        job = re.search(r"_([0-9a-f]{8})(?:_v\d+)?$", path.parent.name)
        job = job[1] if job else path.parent.name
        try:
            plans.append((job, json.loads(path.read_text(encoding="utf-8"))))
        except ValueError:
            err = _read_lines(path.with_name("plan_after.err"))
            if any("EXPIRED" in line for line in err):
                bad[job] = "posting gone (expired)"
            else:
                bad[job] = "could not re-read the form" + (f" ({_cut(err[-1], 80)})" if err else "")
    return plans, bad


def _tonight(rows: list[dict], since: datetime, date_key: str) -> list[dict]:
    keep = []
    for r in rows:
        when = _ts(r.get("at", ""))
        if when is not None:
            if when >= since:
                keep.append(r)
        elif str(r.get(date_key, "")) >= since.date().isoformat():
            keep.append(r)
    return keep


def build(run_dir: Path | None, since: datetime) -> str:
    stages = stage_lines(run_dir)
    reports = digest.load_reports()
    scored = view(paths.SCORED) if paths.SCORED.exists() else None
    clean = (
        pd.read_parquet(paths.CLEAN, columns=["job_id", "company", "title"])
        if paths.CLEAN.exists()
        else pd.DataFrame(columns=["job_id", "company", "title"])
    )
    names = {r.job_id: f"{r.company} — {r.title}" for r in clean.itertuples()}
    run_json = paths.STAGING / "run.json"
    open_slices = (
        len(json.loads(run_json.read_text(encoding="utf-8")).get("slices", {}))
        if run_json.is_file()
        else 0
    )

    log = [json.loads(x) for x in _read_lines(paths.SYNONYM_LOG)]
    declined = _tonight(
        yaml.safe_load(paths.SYNONYM_DECLINED.read_text(encoding="utf-8")) or []
        if paths.SYNONYM_DECLINED.is_file()
        else [],
        since,
        "first_seen",
    )
    opened = (
        (yaml.safe_load(paths.SYNONYM_OPEN.read_text(encoding="utf-8")) or {}).get("open") or []
        if paths.SYNONYM_OPEN.is_file()
        else []
    )

    snaps, unreadable = _snapshots(since)
    tail = tailor_lines(run_dir)
    tried = [r.split()[0] for r in tail if r.strip() and not r.strip().endswith(" deferred")]
    live = set(clean["job_id"])
    failed = [r.split()[0] for r in tail if r.strip().endswith(" fail")]
    unresolved = {j: why for j in failed if (why := tailor_reason(j, run_dir, live))}
    score, tailor = stages.get("score", {}), stages.get("tailor", {})

    score_limit = bool(score.get("code")) and bool(score.get("usage_limit"))

    def cue(name: str, lines: list[str], when: bool) -> list[str]:
        return ["", CUES[name]] if when else []

    def has_errors(lines: list[str]) -> bool:
        return "Errors: none" not in lines and any(ln.startswith("Errors:") for ln in lines)

    items = open_items(snaps)
    items["names"] = names
    discovery = discovery_section(reports, since)
    dupes = duplicates_section(
        _tonight(load_maybe_duplicates(paths.MAYBE_DUPES), since, "date"),
        state_io.load_state_index(paths.PIPELINE),
    )
    scoring = scoring_section(
        scored,
        since,
        score.get("detail", ""),
        open_slices,
        score.get("code"),
        bool(score.get("usage_limit")),
    )
    synonyms = synonyms_section(_tonight(log, since, "date"), declined, opened)
    tailoring = tailoring_section(
        len(tried), unresolved, names, tailor.get("detail", ""), tailor.get("code")
    )
    out = [
        f"# Night summary — {since:%Y-%m-%d} (started {since:%H:%M})",
        "",
        *run_section(stages),
        *spend_line(run_dir),
        "",
        "## Discovery",
        *discovery,
        *cue("discovery", discovery, has_errors(discovery)),
        "",
        "## Possible duplicates",
        *dupes,
        *cue("dupes", dupes, dupes != ["None tonight."]),
        "",
        "## Scoring",
        *scoring,
        *cue("scoring", scoring, bool(open_slices) and not score_limit),
        *cue("score_limit", scoring, score_limit),
        "",
        "## Synonyms",
        *synonyms,
        *cue("synonyms", synonyms, bool(declined or opened or _tonight(log, since, "date"))),
        "",
        "## Tailoring",
        *tailoring,
        *cue("tailoring", tailoring, has_errors(tailoring)),
        "",
        "## Apply",
        *apply_section(
            sheet.rows(),
            items,
            unreadable,
            sheet.latest_outcomes(paths.APPLICATIONS / "apply_runs"),
            since,
        ),
        *([f"Cleanup: {stages['cleanup']['detail']}"] if stages.get("cleanup") else []),
        "",
        *sheet_line(sheet.check()),
    ]
    return "\n".join(out) + "\n"


_CSS = (
    "body{font:16px/1.5 -apple-system,Helvetica,Arial,sans-serif;margin:0;padding:16px;"
    "max-width:680px;color:#1c1c1e;background:#fff}h1{font-size:22px;margin:0 0 6px}"
    "h2{font-size:18px;margin:26px 0 6px;padding:4px 0 4px 10px;border-left:4px solid #0a84ff;"
    "background:#f2f7ff}"
    "p{margin:6px 0}ul{margin:4px 0 8px;padding-left:20px}ul ul{margin:2px 0 4px}"
    "li{margin:3px 0}code{background:#f2f2f7;padding:0 4px;border-radius:4px;font-size:14px;"
    "word-break:break-word}"
    ".fail{color:#b00020}.sub{margin-top:18px;font-weight:600}"
    ".cue{color:#5a5a5f;font-size:14px;background:#f7f7f9;border-radius:6px;padding:6px 8px}"
)


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    return re.sub(r"`(.+?)`", r"<code>\1</code>", text)


def to_html(md: str) -> str:
    """The summary as one mobile-width HTML page. Only the shapes this module
    writes: `#`, `##`, `- ` bullets (one nested level, four spaces), `**bold**`,
    `code`, `**FIX:**` lines, plain lines."""
    body: list[str] = []
    depth = 0  # 0 not in a list, 1 in a list, 2 in a nested list
    open_li = False  # a top-level <li> still waiting for its nested list or its end

    def close_list() -> None:
        nonlocal depth, open_li
        if depth == 2:
            body.append("</ul></li>")
        elif open_li:
            body.append("</li>")
        if depth:
            body.append("</ul>")
        depth, open_li = 0, False

    for line in md.splitlines():
        m = re.match(r"( {4})?- (.*)", line)
        if m:
            item = _inline(m.group(2))
            if m.group(1) and depth:
                if depth == 1:
                    body.append("<ul>")
                    depth = 2
                body.append(f"<li>{item}</li>")
                continue
            if depth == 2:
                body.append("</ul></li>")
                depth = 1
            elif open_li:
                body.append("</li>")
            if depth == 0:
                body.append("<ul>")
                depth = 1
            body.append(f"<li>{item}")
            open_li = True
            continue
        close_list()
        if line.startswith("## "):
            body.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line.startswith("# "):
            body.append(f"<h1>{_inline(line[2:])}</h1>")
        elif line.startswith("**FIX:**"):
            body.append(f'<p class="cue">{_inline(line)}</p>')
        elif line.strip():
            fail = (
                ' class="fail"'
                if re.match(r"\*\*\w+ failed\*\*", line)
                else (' class="sub"' if re.fullmatch(r"\*\*.+\*\*", line) else "")
            )
            body.append(f"<p{fail}>{_inline(line)}</p>")
    close_list()
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<style>{_CSS}</style></head><body>{''.join(body)}</body></html>\n"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="summary", description=__doc__.split("\n\n")[0])
    ap.add_argument("--run-dir", type=Path, default=None)
    ap.add_argument(
        "--since", default=None, help="ISO time the run started (default: from the run dir)"
    )
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    run_dir = args.run_dir or latest_run_dir()
    since = _ts(args.since) if args.since else since_of(run_dir)
    if since is None:
        ap.error(f"--since: not an ISO time: {args.since!r}")
    try:
        text = build(run_dir, since)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    out = args.out or paths.SUMMARY
    for path, content in ((out, text), (out.with_suffix(".html"), to_html(text))):
        atomic_write_text(path, content)
    print(f"wrote {out} and {out.with_suffix('.html').name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
