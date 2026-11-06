"""Deterministic CLI for /apply (R7: no LLM calls; R10: never writes state).

prepare <job_id>      check the role, the answer bank and Playwright; print
                      OUT_DIR / VERTICAL / COMPANY_ANSWERS / OVERRIDES_FILE and
                      reset that role's answers_override.json.
plan <job_id> [--json] [--questions] [--answers F] [--url U] [--out-dir D]
                      read the form, answer it from the bank, print the plan.
                      No browser (Ashby opens one headless page for field
                      text). Exits 1 when an --answers entry cannot apply.
run [--job-id X] [--answers F] [--manifest F] [--submit] [--yes] [--limit N]
    [--rate 4m] [--jitter 60s] [--headless]
                      fill, and click only with --submit. With --answers (the
                      session's confirmed answers) it first saves what they
                      teach to the bank. --submit needs --limit or --job-id,
                      a typed confirmation unless --yes, and never clicks a
                      role already in the submit ledger. Writes
                      applications/apply_runs/<timestamp>.json and the
                      .md report rendered from it.
confirm <answers-file>
                      show the answers and the bank's keyword guesses they
                      leave standing (offline, from the saved plan.json),
                      take a typed yes on a terminal, and
                      store their hash beside the file; run and the bank honour
                      the file only while it still matches.
wordings (--review [--out F] | --save F)
                      list the work-authorization wordings saved plan.json
                      files park on that fact_wordings lacks, each with the
                      suggested fact, its answer and a decision to set; --save
                      saves the decision: yes rows (typed yes on a terminal).
learn [--job J] [--undo ID [--wording W | --keyword K]] [--undo-claim TEXT] [--undo-fact LABEL]
                      review what the bank holds, or take one thing back.
triage [--fill] [--job-id J ...] [--limit N]
                      sort Resume ready roles into ready / open with no model; --fill
                      fills the ready ones (never submits) and writes a run report.
teach --question-id Q --label L --value V [--offered O ...] [--keyword K] [--misfired Q2] [--job J]
                      save one answer to the bank, for a question answered outside a run.
sheet [--add | --list STATUS]
                      rebuild the status sheet's status/note/command from
                      state.yaml, the ledger and the run records; --add first
                      appends newly scored roles; --list prints the job_ids
                      at one status and writes nothing.
resolve-urls (--dry-run | --apply) [--limit N]
                      for Manual apply / No apply URL roles whose company has a
                      Greenhouse, Lever or Ashby board in boards.csv, look for
                      the title there. --dry-run lists them with no network
                      call; --apply fetches and stores jobs/apply_urls.json.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import random
import re
import shlex
import sys
import time
from dataclasses import dataclass, replace
from dataclasses import field as dc_field
from pathlib import Path

import pandas as pd

from applyourself import state_io, track_cli
from applyourself.apply import (
    apply_url,
    ashby,
    bank,
    browser,
    confirmation,
    cooldown,
    greenhouse,
    ledger,
    lever,
    run_record,
)
from applyourself.apply.answers import (
    Answers,
    canonical_place,
    find_question,
)
from applyourself.apply.bank_schema import (
    EXAMPLE_NAME,
    FACT_WORDING_FACTS,
    AnswersError,
    load_answers,
)

# Board identity comes from the shared table in
# `src/applyourself/shared/job_boards.py`, wrapped by `applyourself.apply.detect` so
# `shortlist.py` can ask "can /apply submit to this?" without importing this
# CLI. `detect_ats` recognizes Workday too, so every
# submission path gates on SUBMITTABLE_ATS, not on "not None".
from applyourself.apply.detect import SUBMITTABLE_ATS, detect_ats
from applyourself.apply.domscan import DomScanError
from applyourself.apply.fill import (
    SubmitGuardError,
    blocking_details,
    has_driver,
    run_one,
)
from applyourself.apply.greenhouse import ApplyUrlError, PostingExpired, load_board
from applyourself.apply.plan import Override, Plan, PlanError, plan_for_board
from applyourself.apply.reconcile import ReconcileError
from applyourself.apply.report import (
    _value,
    as_dict,
    render,
    render_fill,
    render_questions,
    render_report,
    reserve_report_path,
    write_manifest,
    write_report,
)
from applyourself.apply.role_facts import company_answers_for, job_country
from applyourself.apply.schema import SchemaError
from applyourself.material import folders as app_folders
from applyourself.score_store import view
from applyourself.shared import clock, paths, schemas
from applyourself.shared.http_client import CareersError
from applyourself.shared.identity import normalize_company
from applyourself.shared.logs import setup_logging
from applyourself.shared.parquet_io import read_parquet

R = run_record.ReasonCode


def ledger_path() -> Path:
    """`submitted.jsonl`, resolved at call time from `paths.APPLY_RUNS`."""
    return paths.APPLY_RUNS / ledger.LEDGER_NAME


# What an `--answers` entry may carry. `label` is a record for the reader; the
# rest is read.
OVERRIDE_KEYS = ("value", "source", "question", "claim", "keyword", "fact", "label")
CLAIM_KEYS = ("authorized", "requires_sponsorship", "holds", "country")


class ApplyCliError(Exception):
    """A per-role failure with a message meant for the user."""


class ManualApplyOnly(ApplyCliError):
    """The role is real and reachable, but no submit path exists for its board
    — Workday, LinkedIn, or a company careers page.

    Distinct from a failure on purpose. These roles have to be applied to by
    hand, and the run report says so in its own category rather
    than burying them in `failed`, where they read as breakage and drive the
    exit code."""


def resolve_url(job_id: str, state: dict | None) -> str:
    """The posting URL to apply through: `apply_url.resolve` over the stored
    apply_url, clean.parquet's url, state.yaml's url and a board link in the
    JD, so the sheet and the run agree. ManualApplyOnly names why when none
    is a board /apply submits to.
    """
    candidates: list[tuple[str, str]] = []
    jd = ""
    if paths.CLEAN.exists():
        clean = read_parquet(
            paths.CLEAN, schemas.CLEAN_SCHEMA_VERSION, columns=["job_id", "url", "jd_text"]
        )
        row = clean.loc[clean["job_id"] == job_id]
        if not row.empty:
            candidates.append(("clean.parquet", str(row.iloc[0]["url"] or "")))
            jd = str(row.iloc[0]["jd_text"] or "")
    if state:
        candidates.append(("state.yaml", str(state.get("url") or "")))

    if not candidates:
        raise ApplyCliError(
            f"{job_id} is in neither clean.parquet nor pipeline/{job_id}/state.yaml"
        )
    found = apply_url.resolve([u for _, u in candidates], jd, apply_url.load().get(job_id))
    if found.kind == apply_url.SUBMITTABLE:
        return found.url

    seen = "; ".join(f"{where}: {url or '(empty)'}" for where, url in candidates)
    raise ManualApplyOnly(
        f"{job_id} has no Greenhouse, Lever or Ashby posting URL to apply through "
        f"({seen}; {found.reason}). Not one of the boards /apply submits to — apply by hand."
    )


def resolve_out_dir(job_id: str) -> Path:
    """The folder the role's resume was last built in, as `app dir` resolves it."""
    try:
        return app_folders.built_dir(job_id)
    except app_folders.AppError as exc:
        raise ApplyCliError(str(exc)) from exc


def _playwright_available() -> bool:
    """A separate, monkeypatchable seam so tests never depend on whether the
    optional `apply` dependency group is actually installed.

    Delegates to `browser.require_playwright()` rather than importing
    `playwright` here directly — `test_no_module_but_fill_names_the_driver`
    enforces that only browser.py and fill.py may name that import, so every other module
    stays importable without the `apply` extras group."""
    try:
        browser.require_playwright()
    except SystemExit:
        return False
    return True


def _cmd_prepare(args: argparse.Namespace) -> int:
    """`/apply` Step 1: validate prerequisites, derive this role's
    out_dir/vertical/company_answers from state.yaml, and reset its
    answers_override.json. Never judges (R7). Consolidated into one call so
    the command session does not issue several.

    Prints `key=value` lines, shell-quoted, for the command session to read.
    """
    job_id = args.job_id
    state_path = state_io.state_path_for(paths.PIPELINE, job_id)
    if not state_path.exists():
        print(f"ERROR: {state_path} missing.", file=sys.stderr)
        return 1
    if not paths.ANSWER_BANK.exists():
        print(
            f"ERROR: {paths.ANSWER_BANK} missing. Copy {paths.example(EXAMPLE_NAME)} and fill it in.",
            file=sys.stderr,
        )
        return 1
    if not _playwright_available():
        print(
            "ERROR: playwright not installed. Run: uv sync --group apply && "
            "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 uv run playwright install chrome",
            file=sys.stderr,
        )
        return 1

    state = state_io.load_state(state_path) or {}
    role_state = state.get("state", "")
    if role_state not in ("saved", "tailored"):
        print(
            f"ERROR: {state_path} has state {role_state!r}, not 'saved' or "
            "'tailored'. Run /tailor first.",
            file=sys.stderr,
        )
        return 1
    # `saved` is a valid entry state: whether this role needs a cover letter
    # before it can become `tailored` is /apply's step 3 (Materials), per role,
    # from the board's own form.

    try:
        out_dir = resolve_out_dir(job_id)
        info = app_folders.role_info(job_id)
    except (ApplyCliError, app_folders.AppError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    vertical = info["vertical"]

    # The role folder is the truth: answers exist iff the file is there. A
    # missing file is not a prerequisite: /apply's step 3 (Materials) decides
    # from the board's form whether any company question needs one.
    candidate = out_dir / "company_answers.yaml"
    company_answers = str(candidate) if info["has_answers"] else ""

    # Persisted next to the role's other artifacts instead of a /tmp scratch
    # file -- an audit trail of what was submitted and why. Overwritten fresh
    # at the start of every run, so every key present by the end belongs to
    # this run only.
    overrides_file = out_dir / "answers_override.json"
    overrides_file.write_text(json.dumps({"job_id": job_id}) + "\n", encoding="utf-8")

    result = {
        "JOB_ID": job_id,
        "STATE": role_state,
        "OUT_DIR": str(out_dir),
        "VERTICAL": vertical,
        "COMPANY_ANSWERS": company_answers,
        "OVERRIDES_FILE": str(overrides_file),
    }
    for key in ("JOB_ID", "STATE", "OUT_DIR", "VERTICAL", "COMPANY_ANSWERS", "OVERRIDES_FILE"):
        print(f"{key}={shlex.quote(result[key])}")
    return 0


def load_overrides(path: Path, job_id: str | None = None) -> dict[str, Override]:
    """The /apply session's answers for one role:
    `{"job_id": "...", "<field_id>": {"value": ..., "source": ..., "question": ...}}`.

    `value` is a string, or a list for a multi-select. `question` is the bank
    question the label asks, so a confirmed run saves it; `claim` is, for a
    work-authorization option, the facts its text states. The `job_id` key binds
    the file to one role: a "why us" answer sent to the wrong company is the
    failure it prevents.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApplyCliError(f"{path}: not readable JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise ApplyCliError(f"{path}: top level must be a JSON object")

    raw.pop("confirmed", None)
    declared = raw.pop("job_id", None)
    if declared is not None and job_id is not None and declared != job_id:
        raise ApplyCliError(
            f"{path} was drafted for job_id {declared!r} but this run is "
            f"{job_id!r}. Company-specific answers do not transfer between "
            f"roles — re-run /apply for {job_id}."
        )

    overrides: dict[str, Override] = {}
    for field_id, entry in raw.items():
        where = f"{path}: {field_id!r}"
        if not isinstance(entry, dict):
            raise ApplyCliError(f"{where} must be an object")
        unknown = set(entry) - set(OVERRIDE_KEYS)
        if unknown:
            raise ApplyCliError(
                f"{where} has unknown keys {sorted(unknown)}; allowed: {', '.join(OVERRIDE_KEYS)}"
            )
        fact = entry.get("fact")
        if fact is not None:
            if (
                not isinstance(fact, dict)
                or set(fact) != {"fact", "yes_when"}
                or fact["fact"] not in FACT_WORDING_FACTS
                or not isinstance(fact["yes_when"], bool)
            ):
                raise ApplyCliError(
                    f'{where} fact is {{"fact": one of '
                    f'{", ".join(FACT_WORDING_FACTS)}, "yes_when": true|false}}'
                )
            overrides[field_id] = Override(
                value="", source=str(entry.get("source") or ""), fact=dict(fact)
            )
            continue
        value = entry.get("value")
        if isinstance(value, list):
            if not value or not all(isinstance(v, str) and v.strip() for v in value):
                raise ApplyCliError(f"{where} value list must hold non-empty strings")
            value = tuple(v.strip() for v in value)
        elif isinstance(value, str) and value.strip():
            value = value.strip()
        else:
            raise ApplyCliError(f"{where} value must be a non-empty string or list")
        question = str(entry.get("question") or "").strip()
        if question and not re.fullmatch(r"[a-z0-9_]+", question):
            raise ApplyCliError(f"{where} question id is lowercase letters, digits and _")
        claim = entry.get("claim")
        if claim is not None:
            if not isinstance(claim, dict) or not claim or set(claim) - set(CLAIM_KEYS):
                raise ApplyCliError(f"{where} claim keys are {', '.join(CLAIM_KEYS)}")
            for k, v in claim.items():
                if k != "country" and not isinstance(v, bool):
                    raise ApplyCliError(f"{where} claim.{k} must be true or false")
        keyword = str(entry.get("keyword") or "").strip()
        overrides[field_id] = Override(
            value=value,
            source=str(entry.get("source") or ""),
            question=question,
            claim=claim,
            keyword=keyword,
        )
    return overrides


def answers_confirmed(path: Path | None) -> bool:
    """Whether the user confirmed this answers file with `apply confirm` and it
    has not changed since. Only a confirmed file teaches the bank or lets a
    keyword guess through a submit."""
    return confirmation.is_confirmed(path)


#: A posted minimum below this is a rate per hour or month, not per year.
MIN_ANNUAL_SALARY = 20000


def salary_for(
    job_id: str, vertical: str, country: str, answers: Answers | None = None
) -> float | None:
    """The figure to ask for, from the bank's `salary` on this country's
    work-authorization row: in your own country, this posting's minimum (in the
    row's currency) times its `markup_pct`, else its `amount`.

    None when the row sets none for this lane: the question parks.
    """
    answers = answers if answers is not None else load_answers()
    salary = answers.facts_for(country).salary
    if salary is None or not salary.for_lane(vertical):
        return None
    home = (canonical_place(country) or country) == answers.home_country
    if home and salary.markup_pct is not None and paths.CLEAN.exists():
        clean = read_parquet(paths.CLEAN, schemas.CLEAN_SCHEMA_VERSION)
        if {"salary_min", "salary_currency"} <= set(clean.columns):
            row = clean.loc[clean["job_id"] == job_id]
            if not row.empty:
                salary_min = row.iloc[0]["salary_min"]
                currency = str(row.iloc[0]["salary_currency"] or "").strip().upper()
                # Under this is an hourly or monthly rate, not a year's pay:
                # the row's amount answers instead.
                if (
                    currency == salary.currency
                    and not pd.isna(salary_min)
                    and salary_min >= MIN_ANNUAL_SALARY
                ):
                    return float(salary_min) * (1 + float(salary.markup_pct) / 100)
    return float(salary.amount)


def build(
    job_id: str,
    url: str | None = None,
    out_dir: Path | None = None,
    answers_path: Path | None = None,
) -> tuple[Plan, Answers]:
    """Everything `plan` does, minus the printing.

    Returns the answer bank alongside the plan: the fill re-resolves a parked
    select once the browser can read its real options.
    """
    state = state_io.load_state(state_io.state_path_for(paths.PIPELINE, job_id))
    posting_url = url or resolve_url(job_id, state)
    target = Path(out_dir) if out_dir else resolve_out_dir(job_id)
    answers = load_answers()
    tailored_dirs = (state or {}).get("tailored_dirs") or []
    vertical = tailored_dirs[-1].split("/", 1)[0] if tailored_dirs else ""
    answers = replace(
        answers,
        job_source=str((state or {}).get("source") or ""),
        job_country=job_country(state, answers),
        company_answers=company_answers_for(target),
    )
    answers = replace(answers, salary_figure=salary_for(job_id, vertical, answers.country, answers))
    overrides = load_overrides(Path(answers_path), job_id) if answers_path else None

    ats = detect_ats(posting_url)
    if ats not in SUBMITTABLE_ATS:
        raise ApplyCliError(f"{posting_url}: not a Greenhouse, Lever or Ashby posting URL")
    if ats == "lever":
        board = lever.load_board(posting_url)
        return plan_for_board(
            board,
            answers,
            target,
            job_id=job_id,
            overrides=overrides,
            ats="lever",
            requires_captcha=board.requires_captcha,
        ), answers
    if ats == "ashby":
        # Read through Ashby's GraphQL form API, not its HTML.
        board = ashby.load_board(posting_url)
        return plan_for_board(
            board, answers, target, job_id=job_id, overrides=overrides, ats="ashby"
        ), answers
    board = load_board(posting_url)
    return plan_for_board(board, answers, target, job_id=job_id, overrides=overrides), answers


# Every exception `build()` can raise that names a per-role problem rather
# than a bug in this CLI. `PostingExpired` is handled separately — it is an
# ordinary outcome, not a failure.
BUILD_ERRORS = (
    ApplyCliError,
    ApplyUrlError,
    AnswersError,
    PlanError,
    CareersError,
    SchemaError,
    DomScanError,
    ReconcileError,
)


# ------------------------------------------------------------------ commands


def _cmd_plan(args: argparse.Namespace) -> int:
    try:
        plan, _ = build(args.job_id, url=args.url, out_dir=args.out_dir, answers_path=args.answers)
    except PostingExpired as exc:
        print(f"EXPIRED: {exc}", file=sys.stderr)
        return 2
    except BUILD_ERRORS as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(as_dict(plan), indent=2))
    elif getattr(args, "questions", False):
        print(render_questions(plan))
    else:
        print(render(plan))
    # An answer that cannot apply looks like one that did, unless this fails.
    return 1 if plan.ignored_overrides else 0


def _cmd_sheet(args: argparse.Namespace) -> int:
    from applyourself.apply import sheet

    try:
        if args.list:
            print("\n".join(sheet.job_ids(args.list)))
            return 0
        if args.compare:
            return _sheet_compare(sheet.compare())
        counts = sheet.refresh(add=args.add)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(" · ".join(f"{status} {n}" for status, n in sorted(counts.items())))
    return 0


def _cmd_resolve_urls(args: argparse.Namespace) -> int:
    """The opt-in network step of the apply-URL resolver; nothing else fetches."""
    from applyourself.apply import sheet
    from applyourself.discovery.sources.boards.base import load_boards

    stored = apply_url.load()
    by_company = apply_url.boards_by_company(load_boards())
    todo = [
        r
        for r in sheet.queue()
        if r["status"] in apply_url.STATUS.values()
        and apply_url.needs_lookup(stored.get(r["name"]))
        and normalize_company(r["company"]) in by_company
    ][: args.limit]
    for r in todo:
        boards = by_company[normalize_company(r["company"])]
        print(f"{r['name']}  {r['company']} — {r['title']}  -> {', '.join(b.key for b in boards)}")
    if args.dry_run or not todo:
        print(
            f"{len(todo)} role(s) to look up{' (dry run, no network call)' if args.dry_run else ''}"
        )
        return 0
    rows = [{"job_id": r["name"], "company": r["company"], "title": r["title"]} for r in todo]
    found, errors = apply_url.find_on_boards(rows, by_company)
    apply_url.save(found)
    for e in errors:
        print(f"ERROR: {e}", file=sys.stderr)
    hits = sum(1 for v in found.values() if v["apply_url"])
    print(f"found {hits} of {len(todo)}; stored {len(found)} in {paths.APPLY_URLS}")
    return 1 if errors else 0


def _sheet_compare(got: dict[str, list[str]]) -> int:
    """Print how the sheet as it stands differs from the computed queue; 1 when
    the roles differ. Order alone is not a difference: the queue sorts by score,
    then job_id."""
    print(f"sheet rows {len(got['sheet'])} · queue rows {len(got['queue'])}")
    for key in ("missing", "extra"):
        print(f"{key} {len(got[key])}: {' '.join(got[key][:20])}".rstrip(": "))
    print("order: " + ("differs (queue sorts by score, then job_id)" if got["moved"] else "same"))
    return 1 if got["missing"] or got["extra"] else 0


def _cmd_learn(args: argparse.Namespace) -> int:
    """Review or undo what the bank holds. Runs learn on their own; this is
    for looking and taking back."""
    path = Path(args.path) if args.path else None
    try:
        if args.undo or args.undo_claim or args.undo_fact:
            print(
                bank.undo(
                    args.undo or "",
                    wording=args.wording or "",
                    keyword=args.keyword or "",
                    claim=args.undo_claim or "",
                    fact=args.undo_fact or "",
                    path=path,
                )
            )
            return 0
        for line in bank.review(path, since_job=args.job or ""):
            print(line)
    except (bank.BankError, AnswersError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_triage(args: argparse.Namespace) -> int:
    from applyourself.apply import triage

    try:
        return triage.main(
            args.job_id or None, fill=args.fill, headless=args.headless, limit=args.limit
        )
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _cmd_teach(args: argparse.Namespace) -> int:
    """Save one answer to the bank: a question the user answered outside a run."""
    values = tuple(args.value)
    lesson = bank.Lesson(
        label=args.label,
        question=args.question_id,
        value=values[0] if len(values) == 1 else values,
        offered=tuple(args.offered),
        misfired=args.misfired or "",
        source=args.source,
        job_id=args.job or "",
        keyword=args.keyword or "",
    )
    try:
        changes = bank.learn([lesson], Path(args.path) if args.path else None)
    except (bank.BankError, AnswersError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    for line in changes or ["nothing to change: the bank already answers this"]:
        print(line)
    return 1 if any(c.startswith("not saved:") for c in changes) else 0


# ------------------------------------------------------------------ queue


def eligible_queue(pipeline_dir: Path | None = None) -> list[str]:
    """`state == tailored` with its built folder on disk, and whose latest submit-ledger
    line (if any) is `refused` — a role already sent to a board never
    re-enters the queue, even if its state.yaml was re-created. Matched on
    job_id or on the form URL, as `ledger.verdict` does.

    A cover letter is not required here: whether one is needed depends on what
    the board's form asks, which `/apply`'s step 3 (Materials) decides per
    role. A role whose board requires a cover letter it doesn't have
    surfaces via `build_plan()`'s `unmapped`/`parked` outcome, the same
    mechanism that blocks any other missing required field. Highest fit_score
    first (`score_store.view`; a role with no score last), then job_id, so a
    batch `--limit` takes the best fits and the queue is reproducible run to run.

    Defaults to `paths.PIPELINE`, looked up at call time.
    """
    idx = state_io.load_state_index(pipeline_dir if pipeline_dir is not None else paths.PIPELINE)
    sent = ledger.sent_job_ids(ledger_path())
    sent_forms = ledger.sent_form_keys(ledger_path())
    queue = [
        job_id
        for job_id, st in idx.items()
        if st.get("state") == "tailored"
        and _built_on_disk(st)
        and job_id not in sent
        and not (sent_forms and _form_key(job_id, st) in sent_forms)
    ]
    fit: dict[str, float] = {}
    if queue and paths.SCORED.exists():
        scored = view(paths.SCORED).dropna(subset=["fit_score"])
        fit = dict(zip(scored["job_id"], scored["fit_score"], strict=True))
    return sorted(queue, key=lambda j: (j not in fit, -fit.get(j, 0.0), j))


def _built_on_disk(state: dict) -> bool:
    folder = app_folders.built_folder(state)
    return folder is not None and folder.is_dir()


def _form_key(job_id: str, state: dict) -> str:
    """`ledger.url_key` of the form this role would be filled through, or ""
    when it has none: a retitled role keeps its form but gets a new id."""
    try:
        url = resolve_url(job_id, state)
    except ApplyCliError:
        return ""
    parsers = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}
    module = parsers.get(detect_ats(url) or "")
    try:
        return ledger.url_key(module.parse_posting(url).form_url) if module else ""
    except ApplyUrlError:
        return ""


_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(s|m|h)?$")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, None: 1}

# Hard floor on the delay between roles, in seconds. A constant, never a config
# key: this paces requests at third-party employer boards, so it is not the
# operator's to turn down.
MIN_RATE_SECONDS = 30.0

# Submissions allowed per company per run.
MAX_SUBMISSIONS_PER_COMPANY = 1


def _auto_submittable(job_id: str) -> bool:
    """Whether `resolve_url` would find a posting to fill for this role."""
    state = state_io.load_state(state_io.state_path_for(paths.PIPELINE, job_id))
    try:
        resolve_url(job_id, state)
    except ApplyCliError:
        return False
    return True


def parse_duration(spec: str) -> float:
    """'4m' / '60s' / '90' -> seconds. Bare digits are seconds."""
    match = _DURATION.match(spec.strip())
    if not match:
        raise ApplyCliError(f"not a duration: {spec!r} (want e.g. '4m', '60s', '90')")
    value, unit = match.groups()
    return float(value) * _DURATION_UNITS[unit]


def clamp_rate(seconds: float) -> float:
    """Raise a below-floor `--rate` to `MIN_RATE_SECONDS`, warning on stderr."""
    if seconds < MIN_RATE_SECONDS:
        print(
            f"WARNING: --rate {seconds:g}s is below the {MIN_RATE_SECONDS:g}s "
            f"floor; using {MIN_RATE_SECONDS:g}s.",
            file=sys.stderr,
        )
        return MIN_RATE_SECONDS
    return seconds


@dataclass(frozen=True)
class RunOutcome:
    job_id: str
    company: str = ""
    title: str = ""
    category: str = "failed"
    """submitted | submitted_unconfirmed | submitted_untracked | rejected |
    unsent | manual | parked | ready | skipped | failed | expired"""

    detail: str = ""
    unmapped: tuple[dict, ...] = dc_field(default_factory=tuple)
    """One row per still-unanswered required question — `fill.blocking_details`,
    not bare ids. The report is the only place a parked role explains itself,
    and the next /apply pass starts by reading it."""

    evidence: Path | None = None
    """The post-submit capture. Named in the report because a submission whose
    confirmation nothing recognised is exactly the one someone has to go look
    at by hand."""

    from_bank: int = 0
    """Fields the answer bank answered on this form."""
    learned: tuple[str, ...] = ()
    """What this run saved to the bank, one line per change."""
    reason: str = ""
    """A `run_record.ReasonCode`: why the role ended in its category."""
    at: str = dc_field(
        default_factory=lambda: clock.now().isoformat(timespec="seconds"), compare=False
    )
    """When the role's outcome was reached."""


def _track_applied(job_id: str, plan) -> tuple[int, str]:
    """Write the `applied` transition through /track (R10). Returns
    `(returncode, note)` — never raises, because by the time this runs the
    application is already on the board and losing that fact is worse than
    any state-write error."""
    try:
        rc = track_cli.main(
            [job_id, "applied", "--note", f"auto-submitted via /apply ({plan.board})"]
        )
    except Exception as exc:  # noqa: BLE001
        return 1, f" ({type(exc).__name__}: {exc})"
    return int(rc or 0), ""


def lessons_from(plan: Plan, overrides: dict[str, Override], job_id: str) -> list:
    """What a confirmed plan teaches the bank: every override that names a
    question or states a claim, and every keyword guess that went unchallenged
    (the user saw it and let it stand)."""
    fields = {f.id: f for f in plan.fields}
    lessons = []
    for field_id, override in overrides.items():
        f = fields.get(field_id)
        if f is None:
            continue
        if override.claim is not None:
            lessons.append(
                bank.Lesson(label=f.label, value=f.value, claim=override.claim, job_id=job_id)
            )
            continue
        if override.fact is not None:
            lessons.append(
                bank.Lesson(label=f.label, value=f.value, fact=override.fact, job_id=job_id)
            )
            continue
        if not override.question:
            continue
        misfired = f.replaced[5:].split("~", 1)[0] if f.replaced.startswith("bank:") else ""
        lessons.append(
            bank.Lesson(
                label=f.label,
                question=override.question,
                value=f.value,
                offered=f.options,
                misfired=misfired,
                source=override.source,
                job_id=job_id,
                keyword=override.keyword,
            )
        )
    for f in plan.fields:
        if f.id not in overrides and f.source.startswith("bank:") and "~" in f.source:
            lessons.append(
                bank.Lesson(
                    label=f.label,
                    question=f.source[5:].split("~", 1)[0],
                    value=f.value,
                    offered=f.options,
                    job_id=job_id,
                )
            )
    return lessons


def _run_role(
    job_id: str,
    *,
    submit: bool,
    headless: bool,
    answers_path: Path | None = None,
    manifest_path: Path | None = None,
    hold_company: bool = False,
) -> RunOutcome:
    """One role, start to finish. Never raises — every failure mode comes back
    as a category on the outcome, so one bad role cannot stop the queue.

    `hold_company`: re-check the company cooldown against the ledger under its
    lock, right before the click (a batch run; `--job-id` bypasses it).

    `answers_path` is the session's confirmed answers for a single-role run.
    What they teach is saved to the answer bank before the browser opens, so it
    holds whether or not this form goes on to submit.
    """
    try:
        plan, answers = build(job_id, answers_path=answers_path)
    except PostingExpired as exc:
        return RunOutcome(job_id, category="expired", detail=str(exc), reason=R.POSTING_EXPIRED)
    except ManualApplyOnly as exc:
        return RunOutcome(job_id, category="manual", detail=str(exc), reason=R.MANUAL_BOARD)
    except BUILD_ERRORS as exc:
        return RunOutcome(job_id, category="failed", detail=str(exc), reason=R.BUILD_ERROR)
    except Exception as exc:  # noqa: BLE001 - see the docstring's "never raises"
        return RunOutcome(
            job_id,
            category="failed",
            detail=f"unexpected {type(exc).__name__}: {exc}",
            reason=R.BUILD_ERROR,
        )

    if plan.ignored_overrides:
        return RunOutcome(
            job_id,
            plan.company,
            plan.title,
            "failed",
            detail="answers that cannot apply: " + "; ".join(plan.ignored_overrides),
            reason=R.BAD_OVERRIDES,
        )

    learned: tuple[str, ...] = ()
    confirmed = answers_confirmed(answers_path)
    if confirmed:
        try:
            lessons = lessons_from(plan, load_overrides(Path(answers_path), job_id), job_id)
            learned = tuple(bank.learn(lessons))
        except (bank.BankError, AnswersError, ApplyCliError, OSError) as exc:
            print(f"WARNING: nothing saved to the answer bank: {exc}", file=sys.stderr)

    guesses = [f for f in plan.fields if f.source.startswith("bank:") and "~" in f.source]
    if submit and not confirmed and guesses:
        # A keyword guess is only an answer once someone has confirmed it, and
        # only /apply's step 6 does that.
        return RunOutcome(
            job_id,
            plan.company,
            plan.title,
            "parked",
            detail=f"{len(guesses)} keyword guess(es) not confirmed — run /apply {job_id}",
            reason=R.UNCONFIRMED_GUESSES,
            unmapped=tuple(
                {
                    "id": f.id,
                    "label": f.label,
                    "tier": "B",
                    "reason": f"guessed {f.value!r} {f.source}",
                    "source": f.source,
                }
                for f in guesses
            ),
        )
    outcome = _attempt(
        job_id,
        plan,
        answers,
        submit=submit,
        headless=headless,
        manifest_path=manifest_path,
        hold_company=hold_company,
    )
    from_bank = sum(1 for f in plan.fields if f.source.startswith("bank:"))
    return replace(outcome, from_bank=from_bank, learned=learned)


def _attempt(
    job_id: str,
    plan: Plan,
    answers,
    *,
    submit: bool,
    headless: bool,
    manifest_path: Path | None,
    hold_company: bool = False,
) -> RunOutcome:
    """The browser half of `_run_role`: fill, and click only when asked."""
    if submit and headless and plan.requires_captcha:
        # Lever renders hCaptcha on every form and `submit()` blocks for a
        # human to solve it. Headless there means the wait can only ever time
        # out — 10 minutes per role, after the click has already landed.
        return RunOutcome(
            job_id,
            plan.company,
            plan.title,
            "failed",
            detail=f"{plan.ats} renders a captcha and --headless leaves nobody to "
            f"solve it; re-run this role without --headless",
            reason=R.CAPTCHA_HEADLESS,
        )

    if not has_driver(plan.ats):
        # Reported rather than attempted, so the browser never opens for a
        # board that cannot be filled.
        return RunOutcome(
            job_id,
            plan.company,
            plan.title,
            "manual",
            detail=f"{plan.ats}: form scanned and planned, but no "
            f"browser driver exists yet — apply by hand",
            reason=R.NO_DRIVER,
        )

    # `sink` holds the FillResult from before the browser opens, so a crash
    # anywhere after the submit click — including in browser teardown — still
    # tells us an application went out. Reading only the return value lost
    # that, and a lost `submitted` means the role stays `tailored` and the
    # next run applies to the same board again.
    def review(result) -> None:
        """No-submit run only: report while the window is still open, then
        hold it for a human. A non-tty run (background/CI) has nobody to
        hold for, so it closes immediately like today."""
        print(render_fill(result, plan))
        if not headless and sys.stdin.isatty():
            input("\nreview the form in the browser, then press Enter to close: ")

    if submit:
        sent = ledger.prior(ledger_path(), job_id, plan.form_url)
        if sent is not None:
            return RunOutcome(
                job_id,
                plan.company,
                plan.title,
                "skipped",
                detail=f"already sent {sent.get('at', '')} ({sent.get('status', '')}) "
                f"per {ledger.LEDGER_NAME}; delete that line to retry",
                reason=R.ALREADY_SENT,
            )

    clicked: list[bool] = []

    def mark(status: str, detail: str = "") -> None:
        ledger.append(
            ledger_path(),
            job_id,
            status,
            form_url=plan.form_url,
            company=plan.company,
            title=plan.title,
            detail=detail,
        )

    held: list[str] = []
    cooled: list[bool] = []

    def cooling(recs: list[dict]) -> str:
        others = (r for r in recs if r.get("job_id") != job_id)
        day = cooldown.until(plan.company, cooldown.ledger_applications(others))
        cooled.append(bool(day))
        return f"{plan.company}: {cooldown.note(day)}" if day else ""

    def before_click() -> None:
        if not clicked:
            # The "already sent" re-check and the append share one lock.
            why = ledger.claim(
                ledger_path(),
                job_id,
                "clicking",
                form_url=plan.form_url,
                company=plan.company,
                title=plan.title,
                held=cooling if hold_company else None,
            )
            if why:
                held.append(why)
                raise SubmitGuardError(why)
        else:
            mark("clicking")
        clicked.append(True)

    sink: list = []
    try:
        result = run_one(
            plan,
            answers,
            headless=headless,
            submit_after=submit,
            sink=sink,
            after=None if submit else review,
            before_click=before_click,
        )
    except BaseException as exc:  # Ctrl-C must not lose a submit
        landed = sink[0] if sink else None
        # The manifest is read before the click, so a crash after it still has
        # one worth keeping — it is the only record of what the form held.
        if manifest_path is not None and landed is not None and landed.manifest:
            with contextlib.suppress(OSError):
                write_manifest(landed, plan, Path(manifest_path))
        if landed is not None and landed.submitted:
            mark("ambiguous", f"{type(exc).__name__} after the click")
            rc, exc_note = _track_applied(job_id, plan)
            if rc:
                return RunOutcome(
                    job_id,
                    plan.company,
                    plan.title,
                    "submitted_untracked",
                    detail=f"SUBMITTED, then {type(exc).__name__}: {exc}, and "
                    f"track_cli exited {rc}{exc_note} — state.yaml still says "
                    f"tailored; fix by hand",
                    reason=R.TRACK_FAILED,
                )
            return RunOutcome(
                job_id,
                plan.company,
                plan.title,
                "submitted_unconfirmed",
                detail=f"SUBMITTED, then {type(exc).__name__}: {exc} — the click "
                f"landed; verify by hand",
                reason=R.CRASH_AFTER_CLICK,
            )
        if isinstance(exc, Exception):
            # A playwright timeout or driver error must not escape:
            # `run_queue` would abort mid-walk and every role already
            # submitted would lose its only record.
            return RunOutcome(
                job_id,
                plan.company,
                plan.title,
                "failed",
                detail=f"{type(exc).__name__}: {exc}",
                reason=R.BROWSER_ERROR,
            )
        raise

    if manifest_path is not None:
        try:
            write_manifest(result, plan, Path(manifest_path))
        except OSError as exc:
            print(f"WARNING: could not write the manifest: {exc}", file=sys.stderr)

    if held and not clicked:
        return RunOutcome(
            job_id,
            plan.company,
            plan.title,
            "skipped",
            detail=held[0],
            reason=R.COOLDOWN if cooled else R.ALREADY_SENT,
        )

    if clicked and not result.submitted and result.outcome not in ("rejected", "unsent"):
        # The board declined every click, or a captcha was never solved: it
        # has nothing, so the role may be tried again.
        mark("refused", result.submit_error)
    if result.outcome in ("rejected", "unsent"):
        mark(result.outcome, result.submit_error)
        return RunOutcome(
            job_id,
            plan.company,
            plan.title,
            result.outcome,
            detail=result.submit_error
            or "clicked; no submit request fired — verify on the board by hand",
            evidence=result.evidence,
            reason=R.BOARD_REJECTED if result.outcome == "rejected" else R.NO_SUBMIT_REQUEST,
        )

    if result.submitted:
        mark(result.outcome or "ambiguous")
        rc, exc_note = _track_applied(job_id, plan)
        if rc:
            # The application is on the board but the state write refused or
            # blew up. The ledger line already keeps it out of the next run.
            return RunOutcome(
                job_id,
                plan.company,
                plan.title,
                "submitted_untracked",
                detail=f"SUBMITTED but track_cli exited {rc}{exc_note} — state.yaml "
                f"still says tailored; fix by hand",
                reason=R.TRACK_FAILED,
            )
        if result.confirmed:
            return RunOutcome(
                job_id,
                plan.company,
                plan.title,
                "submitted",
                evidence=result.evidence,
                reason=R.CONFIRMED,
            )
        return RunOutcome(
            job_id,
            plan.company,
            plan.title,
            "submitted_unconfirmed",
            detail="clicked and transitioned to applied, but the board showed no "
            "confirmation this code recognises — verify by hand",
            evidence=result.evidence,
            reason=R.NO_CONFIRMATION,
        )

    blocking = blocking_details(plan, result)
    if blocking:
        return RunOutcome(
            job_id,
            plan.company,
            plan.title,
            "parked",
            detail=f"{len(blocking)} required question(s) unresolved",
            unmapped=blocking,
            reason=R.UNRESOLVED,
        )
    if result.failures:
        return RunOutcome(
            job_id,
            plan.company,
            plan.title,
            "failed",
            detail="; ".join(result.failures),
            reason=R.FILL_FAILED,
        )
    if result.submit_error:
        # Not a park (nothing is unmapped) and not a fill failure — the guard
        # refused for a form-shape reason instead (no submit button, or it
        # stayed disabled). Distinct enough from either to say so.
        return RunOutcome(
            job_id,
            plan.company,
            plan.title,
            "failed",
            detail=result.submit_error,
            reason=R.SUBMIT_GUARD,
        )

    return RunOutcome(
        job_id,
        plan.company,
        plan.title,
        "ready",
        detail="every field resolved; rerun with --submit to click",
        reason=R.READY,
    )


def queue_companies(job_ids: list[str], pipeline_dir: Path | None = None) -> dict[str, str]:
    """`job_id -> company`, read from state.yaml. Empty for anything the index
    does not carry; the per-company cap treats that as "unknown", never as a
    match."""
    idx = state_io.load_state_index(pipeline_dir if pipeline_dir is not None else paths.PIPELINE)
    return {job_id: str(idx.get(job_id, {}).get("company") or "") for job_id in job_ids}


def run_queue(
    job_ids: list[str],
    *,
    submit: bool,
    headless: bool = False,
    rate: float = 240.0,
    jitter: float = 60.0,
    sleeper=time.sleep,
    jitter_fn=random.uniform,
    answers_path: Path | None = None,
    manifest_path: Path | None = None,
    on_outcome=None,
    collect_into: list | None = None,
    companies: dict[str, str] | None = None,
    hold_company: bool = False,
) -> list[RunOutcome]:
    """Walk the queue, sleeping `rate + U(0, jitter)` seconds before each
    attempted role after the first.

    At most `MAX_SUBMISSIONS_PER_COMPANY` role per company is submitted per
    run: once a company's application goes out, every later role at that
    company is reported `skipped` and never opens a browser. Only applies when
    `submit` is set — a fill-and-stop walk sends nothing.

    `hold_company` (a batch run): a role whose company had an application
    within `company_cooldown_days` is `skipped` with "cooldown until <date>";
    a fill-and-stop walk still fills it and says so on its `ready` line.

    `companies` maps job_id to the company name the cap keys off; it defaults
    to reading state.yaml. `sleeper`/`jitter_fn` are injectable so tests never
    actually sleep or depend on randomness. `answers_path` (see `_run_role`) is
    only meaningful when `job_ids` names a single role.

    `on_outcome` is called with the accumulated list after every role, and
    `collect_into` lets the caller own that list. Both exist for one reason:
    the run report is the only record of what went out, so it has to survive a
    crash mid-walk. A caller that owns the list still has every completed
    outcome even if this function never returns.
    """
    outcomes = [] if collect_into is None else collect_into
    if companies is None:
        companies = queue_companies(job_ids) if submit or hold_company else {}
    latest = cooldown.last_applications(ledger_path=ledger_path()) if hold_company else {}
    submitted_per_company: dict[str, int] = {}
    attempted = 0
    for job_id in job_ids:
        known = companies.get(job_id, "")
        key = normalize_company(known)
        cool = cooldown.until(known, latest) if hold_company else None
        if submit and cool is not None:
            outcomes.append(
                RunOutcome(
                    job_id,
                    known,
                    "",
                    "skipped",
                    detail=f"{known}: {cooldown.note(cool)}",
                    reason=R.COOLDOWN,
                )
            )
            if on_outcome is not None:
                on_outcome(outcomes)
            continue
        if submit and key and submitted_per_company.get(key, 0) >= MAX_SUBMISSIONS_PER_COMPANY:
            outcomes.append(
                RunOutcome(
                    job_id,
                    known,
                    "",
                    "skipped",
                    detail=f"{known}: already submitted to this company in this run",
                    reason=R.COMPANY_CAP,
                )
            )
            if on_outcome is not None:
                on_outcome(outcomes)
            continue

        if attempted:
            sleeper(rate + jitter_fn(0, jitter))
        attempted += 1
        outcome = _run_role(
            job_id,
            submit=submit,
            headless=headless,
            answers_path=answers_path,
            manifest_path=manifest_path,
            hold_company=hold_company,
        )
        if cool is not None and outcome.category == "ready":
            outcome = replace(outcome, detail=cooldown.note(cool))
        outcomes.append(outcome)
        if outcome.category in _CLICKED:
            # Count both names: the plan's is what actually went out, the
            # index's is what the next role will be checked against.
            for counted in {normalize_company(outcome.company), key} - {""}:
                submitted_per_company[counted] = submitted_per_company.get(counted, 0) + 1
        if on_outcome is not None:
            on_outcome(outcomes)
    return outcomes


# ------------------------------------------------------------------ report


# Categories where a click reached the board; the per-company cap counts them.
_CLICKED = ("submitted", "submitted_unconfirmed", "submitted_untracked", "rejected", "unsent")

# Categories that make `apply run` exit non-zero. `manual` is deliberately not
# one: a board with no submit path is a fact about the board, not a failure.
_EXIT_NONZERO = ("failed", "submitted_untracked")


def submission_preview(
    job_ids: list[str], pipeline_dir: Path | None = None, hold_company: bool = False
) -> list[tuple[str, str, str, str]]:
    """One `(job_id, company, title, board)` row per role `--submit` would
    actually apply to, in queue order.

    `MAX_SUBMISSIONS_PER_COMPANY` and, with `hold_company`, the company
    cooldown are applied here the same way `run_queue` applies them, so what
    the confirmation prompt shows is what the walk does.
    Reads state.yaml and clean.parquet only — no network, no browser.
    """
    idx = state_io.load_state_index(pipeline_dir if pipeline_dir is not None else paths.PIPELINE)
    latest = (
        cooldown.last_applications(pipeline_dir, ledger_path=ledger_path()) if hold_company else {}
    )
    rows: list[tuple[str, str, str, str]] = []
    counted: dict[str, int] = {}
    for job_id in job_ids:
        state = idx.get(job_id, {}) or {}
        company = str(state.get("company") or "")
        key = normalize_company(company)
        if key and counted.get(key, 0) >= MAX_SUBMISSIONS_PER_COMPANY:
            continue
        if cooldown.until(company, latest) is not None:
            continue
        if key:
            counted[key] = counted.get(key, 0) + 1
        try:
            board = detect_ats(resolve_url(job_id, state)) or "unknown board"
        except ApplyCliError:
            board = "manual-apply"
        rows.append((job_id, company, str(state.get("title") or ""), board))
    return rows


# What counts as a typed yes. Anything else — a bare Enter, an empty string,
# "n", a typo — aborts.
_CONFIRM_YES = ("y", "yes")


def confirm_submission(job_ids: list[str], hold_company: bool = False) -> int:
    """Print the roles `--submit` will apply to, then require a typed yes.

    Returns 0 to proceed and 1 to abort. Called before a browser opens and
    before any state is written, so an abort sends nothing. A non-tty stdin
    fails closed: nobody can answer, so it refuses rather than proceeding.
    """
    rows = submission_preview(job_ids, hold_company=hold_company)
    print(f"About to SUBMIT {len(rows)} real application(s):")
    for job_id, company, title, board in rows:
        who = f"{company} — {title}".strip(" —") or "(unknown role)"
        print(f"  {job_id}  {who}  [{board}]")

    if not sys.stdin.isatty():
        print(
            "ERROR: --submit needs confirmation and stdin is not a terminal. "
            "Pass --yes to confirm when running non-interactively.",
            file=sys.stderr,
        )
        return 1
    try:
        reply = input("\nType 'yes' to submit these applications: ")
    except EOFError:
        reply = ""
    if reply.strip().lower() not in _CONFIRM_YES:
        print("ABORTED: nothing submitted.", file=sys.stderr)
        return 1
    return 0


def _standing_guesses(job_id: str, answers_file: Path, data: dict) -> str:
    """The bank's keyword guesses this file leaves standing: confirming lets
    them through a submit and saves each as a wording. Offline: read from the
    role's saved `plan.json` beside the file, re-checked against the bank."""
    plan_file = answers_file.parent / "plan.json"
    try:
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
        schemas.check(plan.get("schema_version"), schemas.PLAN_SCHEMA_VERSION, plan_file)
        if not job_id or plan.get("job_id") != job_id:
            raise ValueError(f"{plan_file} is not for job_id {job_id or '(none)'}")
        bank_now = load_answers()
    except (OSError, ValueError, AnswersError) as exc:
        return f"\nWARNING: the bank's keyword guesses can't be listed offline ({exc})."
    guesses = []
    for f in plan.get("fields") or []:
        if f.get("id") in data or "~" not in str(f.get("source") or ""):
            continue
        found = find_question(str(f.get("label") or ""), bank_now)
        if found and "~" in found[1]:
            guesses.append((f, found[1]))
    lines = [f"\nGuessed by keyword from the bank, also confirmed ({len(guesses)}):"]
    for f, source in guesses:
        value = f.get("value")
        lines.append(f'{f.get("id")} — "{f.get("label")}"')
        lines.append(f"  {_value(tuple(value) if isinstance(value, list) else value)}  [{source}]")
    return "\n".join(lines)


def _cmd_confirm(args: argparse.Namespace) -> int:
    if bank.unattended():
        print(
            f"ERROR: {bank.UNATTENDED_ENV}=1: apply confirm needs you at a terminal.",
            file=sys.stderr,
        )
        return 1
    try:
        data = confirmation.load(args.answers_file)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {args.answers_file}: not readable JSON ({exc})", file=sys.stderr)
        return 1
    print(confirmation.render(data))
    print(_standing_guesses(str(data.get("job_id") or ""), Path(args.answers_file), data))
    print("\nConfirming saves these to the answer bank and lets them through a submit.")
    if not sys.stdin.isatty():
        print(
            "ERROR: apply confirm needs a typed yes and stdin is not a terminal.", file=sys.stderr
        )
        return 1
    try:
        reply = input("Type 'yes' to confirm these answers: ")
    except EOFError:
        reply = ""
    if reply.strip().lower() not in _CONFIRM_YES:
        print("ABORTED: not confirmed.", file=sys.stderr)
        return 1
    print(f"Confirmed: {confirmation.record(args.answers_file, data)}")
    return 0


def _cmd_wordings(args: argparse.Namespace) -> int:
    from applyourself.apply import wordings

    bank_path = Path(args.path) if args.path else None
    if args.review:
        try:
            out, rows = wordings.review(load_answers(bank_path), args.out)
        except (AnswersError, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(f"{len(rows)} wording(s) to decide: {out}")
        return 0
    if bank.unattended():
        print(
            f"ERROR: {bank.UNATTENDED_ENV}=1: apply wordings --save needs you at a terminal.",
            file=sys.stderr,
        )
        return 1
    try:
        lessons = wordings.to_save(args.save)
    except wordings.WordingsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if not lessons:
        print("nothing to save: no row has decision: yes")
        return 0
    print(f"Saving {len(lessons)} wording(s) to the answer bank's fact_wordings:")
    for lesson in lessons:
        print(f"  {lesson.label!r} -> Yes when {(lesson.fact or {}).get('fact')} is true")
    if not sys.stdin.isatty():
        print(
            "ERROR: apply wordings --save needs a typed yes and stdin is not a terminal.",
            file=sys.stderr,
        )
        return 1
    try:
        reply = input("Type 'yes' to save these wordings: ")
    except EOFError:
        reply = ""
    if reply.strip().lower() not in _CONFIRM_YES:
        print("ABORTED: nothing saved.", file=sys.stderr)
        return 1
    try:
        changes = bank.learn(lessons, bank_path)
    except (bank.BankError, AnswersError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    for line in changes or ["nothing to change: the bank already holds these"]:
        print(line)
    return 1 if any(c.startswith("not saved:") for c in changes) else 0


def _cmd_run(args: argparse.Namespace) -> int:
    if bank.unattended() and (args.submit or answers_confirmed(args.answers)):
        what = "--submit" if args.submit else "a confirmed --answers file (it writes the bank)"
        print(
            f"ERROR: {bank.UNATTENDED_ENV}=1: apply run refuses {what} in an "
            "unattended run. Run it yourself, in a session.",
            file=sys.stderr,
        )
        return 1

    # Checked before anything opens a browser. --job-id is exempt: one named
    # role is already a bound of one.
    if args.submit and args.limit is None and not args.job_id:
        print(
            "ERROR: --submit requires an explicit --limit (or --job-id for a "
            "single role). Without it this walks the entire eligible queue "
            "and applies to every role in it.",
            file=sys.stderr,
        )
        return 1

    if args.answers and not args.job_id:
        print("ERROR: --answers only applies to a single role — pass --job-id", file=sys.stderr)
        return 1

    # One path, one role: a queue walk would have every role overwrite the
    # previous role's read-back at the same filename.
    if args.manifest and not args.job_id:
        print(
            "ERROR: --manifest writes one file, so it only applies to a "
            "single role — pass --job-id",
            file=sys.stderr,
        )
        return 1

    if args.job_id:
        queue = eligible_queue()
        if args.job_id not in queue:
            sent = ledger.prior(ledger_path(), args.job_id)
            why = (
                f"already sent {sent.get('at', '')} ({sent.get('status', '')}) per "
                f"{ledger.LEDGER_NAME}; delete that line to retry"
                if sent
                else "needs state == tailored with its tailored_dirs[-1] folder on disk"
            )
            print(f"ERROR: {args.job_id} is not eligible — {why}", file=sys.stderr)
            return 1
        queue = [args.job_id]
    else:
        # Boards with no submit path are dropped before --limit counts, so a
        # limit of 5 means five roles that can actually be filled.
        queue = [j for j in eligible_queue() if _auto_submittable(j)]
        if args.limit is not None:
            queue = queue[: args.limit]

    if not queue:
        print("Nothing eligible: no role at state=tailored with its built folder on disk.")
        return 0

    if args.submit and not args.yes:
        rc = confirm_submission(queue, hold_company=not args.job_id)
        if rc:
            return rc

    try:
        rate = clamp_rate(parse_duration(args.rate))
        jitter = parse_duration(args.jitter)
    except ApplyCliError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    started = clock.now()
    # The caller owns the list and the report is rewritten after every role, so
    # a crash — or a Ctrl-C — mid-walk still leaves a complete record of every
    # application that already went out.
    outcomes: list[RunOutcome] = []
    path = reserve_report_path(started)
    write_report(outcomes, started, path=path)
    try:
        run_queue(
            queue,
            submit=args.submit,
            headless=args.headless,
            rate=rate,
            jitter=jitter,
            answers_path=args.answers,
            manifest_path=args.manifest,
            collect_into=outcomes,
            on_outcome=lambda done: write_report(done, started, path=path),
            hold_company=not args.job_id,
        )
    finally:
        write_report(outcomes, started, path=path)

    print(render_report(outcomes, started, runs_dir=path.parent))
    print(f"Report written to {path}")
    return 1 if any(o.category in _EXIT_NONZERO for o in outcomes) else 0


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        prog="apply",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prep = sub.add_parser(
        "prepare",
        help="/apply's Step 1: validate prerequisites, derive out_dir/vertical/company_answers",
    )
    prep.add_argument("job_id")
    prep.set_defaults(func=_cmd_prepare)

    p = sub.add_parser("plan", help="print the fill plan for one role")
    p.add_argument("job_id")
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--questions",
        action="store_true",
        help="group the form by who answers it (config vs judgment) "
        "instead of by what fill.py does with it",
    )
    p.add_argument("--url", default=None, help="posting URL override, when the stored one is stale")
    p.add_argument("--out-dir", default=None, type=Path, help="/tailor output dir override")
    p.add_argument(
        "--answers", default=None, type=Path, help="the /apply session's answers for this role"
    )
    p.set_defaults(func=_cmd_plan)

    r = sub.add_parser("run", help="walk the eligible queue")
    r.add_argument(
        "--limit",
        type=int,
        default=None,
        help="most roles to walk; required with --submit unless --job-id",
    )
    r.add_argument(
        "--rate",
        default="4m",
        help=f"delay between roles, e.g. '4m'; floored at {MIN_RATE_SECONDS:g}s",
    )
    r.add_argument("--jitter", default="60s", help="added on top of --rate, uniformly")
    r.add_argument(
        "--submit",
        action="store_true",
        help="click submit on every role that resolves; default is fill-and-stop",
    )
    r.add_argument(
        "--yes",
        action="store_true",
        help="skip the --submit confirmation prompt; required to "
        "submit when stdin is not a terminal",
    )
    r.add_argument("--job-id", default=None, help="run one specific role instead of the queue")
    r.add_argument(
        "--answers",
        default=None,
        type=Path,
        help="the /apply session's confirmed answers; requires --job-id. "
        "What they teach is saved to the answer bank",
    )
    r.add_argument(
        "--manifest",
        default=None,
        type=Path,
        help="write the post-fill read-back of every field to this path as JSON; requires --job-id",
    )

    r.add_argument("--headless", action="store_true")
    r.set_defaults(func=_cmd_run)

    c = sub.add_parser("confirm", help="confirm an /apply answers file (typed yes on a terminal)")
    c.add_argument("answers_file", type=Path)
    c.set_defaults(func=_cmd_confirm)

    wd = sub.add_parser(
        "wordings",
        help="review the work-authorization wordings saved plans park on; save the approved ones",
    )
    wmode = wd.add_mutually_exclusive_group(required=True)
    wmode.add_argument(
        "--review",
        action="store_true",
        help='write each wording not yet in fact_wordings, with decision: "" to set',
    )
    wmode.add_argument(
        "--save",
        type=Path,
        metavar="FILE",
        help="save the decision: yes rows as exact fact_wordings (typed yes on a terminal)",
    )
    wd.add_argument(
        "--out",
        type=Path,
        default=None,
        help="with --review: where to write (default applications/apply_runs/wordings_review.yaml)",
    )
    wd.add_argument("--path", default=None, type=Path, help="bank path (tests)")
    wd.set_defaults(func=_cmd_wordings)

    sh = sub.add_parser(
        "sheet", help="rewrite the status sheet from the work queue (state, scores, ledger, runs)"
    )
    sh.add_argument(
        "--add",
        action="store_true",
        help="kept for old callers; every rewrite holds every queued role",
    )
    sh.add_argument(
        "--list",
        default=None,
        metavar="STATUS",
        help="print the queued job_ids at this status, one per line; writes nothing",
    )
    sh.add_argument(
        "--compare",
        action="store_true",
        help="print how the sheet as it stands differs from the queue; writes nothing",
    )
    sh.set_defaults(func=_cmd_sheet)

    ru = sub.add_parser(
        "resolve-urls", help="look for Manual apply / No apply URL roles on the company's own board"
    )
    mode = ru.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="list the lookups; no network call")
    mode.add_argument(
        "--apply", action="store_true", help="fetch the boards and store what matches"
    )
    ru.add_argument("--limit", type=int, default=None)
    ru.set_defaults(func=_cmd_resolve_urls)

    lrn = sub.add_parser("learn", help="review or undo what the answer bank holds")
    lrn.add_argument("--job", default=None, help="only questions this job taught")
    lrn.add_argument(
        "--undo",
        default=None,
        metavar="QUESTION_ID",
        help="remove a question, or one wording of it with --wording",
    )
    lrn.add_argument("--wording", default=None, help="with --undo: one wording only")
    lrn.add_argument("--keyword", default=None, help="with --undo: one keyword only")
    lrn.add_argument(
        "--undo-claim",
        default=None,
        metavar="OPTION_TEXT",
        help="remove one work-authorization claim",
    )
    lrn.add_argument(
        "--undo-fact",
        default=None,
        metavar="LABEL",
        help="remove one judged work-authorization / export-control wording",
    )
    lrn.add_argument("--path", default=None, type=Path, help="bank path (tests)")
    lrn.set_defaults(func=_cmd_learn)

    tri = sub.add_parser(
        "triage",
        help="sort the Resume ready roles into ready / open, "
        "and with --fill fill the ready ones (no model, no submit)",
    )
    tri.add_argument(
        "--fill",
        action="store_true",
        help="fill each ready role's form (never submits) and write a run report",
    )
    tri.add_argument("--headless", action="store_true")
    tri.add_argument("--job-id", action="append", default=[], help="only these roles; repeat")
    tri.add_argument("--limit", type=int, default=None)
    tri.set_defaults(func=_cmd_triage)

    tch = sub.add_parser("teach", help="save one answer to the answer bank")
    tch.add_argument("--question-id", required=True, help="the bank question, existing or new")
    tch.add_argument("--label", required=True, help="the board's question text, in full")
    tch.add_argument(
        "--value", required=True, action="append", help="the answer; repeat for a multi-select"
    )
    tch.add_argument(
        "--offered",
        action="append",
        default=[],
        help="an option the board offers; repeat per option",
    )
    tch.add_argument("--keyword", default=None, help="words every wording of it shares")
    tch.add_argument(
        "--misfired", default=None, help="the question whose keyword wrongly answered this label"
    )
    tch.add_argument("--job", default=None)
    tch.add_argument("--source", default="user")
    tch.add_argument("--path", default=None, type=Path, help="bank path (tests)")
    tch.set_defaults(func=_cmd_teach)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
