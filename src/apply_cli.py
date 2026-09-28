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
                        applications/apply_runs/<timestamp>.md.
  learn [--job J] [--undo ID [--wording W | --keyword K]] [--undo-claim TEXT] [--undo-fact LABEL]
                        review what the bank holds, or take one thing back.
  sheet [--add]         rebuild the status sheet's status/note/command from
                        state.yaml, the ledger and the run reports; --add first
                        appends newly scored roles.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import shlex
import sys
import time
from dataclasses import dataclass, field as dc_field, replace
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

from src import app_cli, paths, state_io, track_cli
from src.apply import ashby, bank, ledger, lever
from src.apply.answers import (DEFAULT_PATH, EXAMPLE_PATH, FACT_WORDING_FACTS,
                               UNKNOWN_COUNTRY, Answers,
                               AnswersError, load_answers)
from src.apply.fill import (_require_playwright, blocking_details, has_driver,
                             run_one)
from src.apply.greenhouse import ApplyUrlError, PostingExpired, load_board
from src.apply.plan import Override, Plan, PlanError, plan_for_board
from src.apply.reconcile import ReconcileError
from src.apply.schema import SchemaError
from src.apply.domscan import DomScanError
from src.discovery.normalize import normalize_company
from src.ats_http import CareersError

# Board identity comes from the shared table in
# `src/ats_registry.py`, wrapped by `src.apply.detect` so
# `shortlist.py` can ask "can /apply submit to this?" without importing this
# CLI. Re-exported here because callers and tests already reach for
# `apply_cli.detect_ats`. `detect_ats` recognizes Workday too, so every
# submission path gates on SUBMITTABLE_ATS, not on "not None".
from src.apply.detect import (  # noqa: E402
    SUBMITTABLE_ATS, detect_ats, is_auto_submittable,
)

CLEAN = paths.CLEAN
PIPELINE = paths.PIPELINE
APPLICATIONS = paths.APPLICATIONS
APPLY_RUNS = APPLICATIONS / "apply_runs"


def ledger_path() -> Path:
    """`submitted.jsonl`, resolved at call time so a test that points
    `APPLY_RUNS` elsewhere moves the ledger with it."""
    return APPLY_RUNS / ledger.LEDGER_NAME

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
    """The posting URL to apply through.

    clean.parquet and state.yaml disagree often enough to matter — dedupe now
    prefers an applyable url, so clean is usually the better one, but a role can
    also drop out of the 14-day window entirely, and one live role has an
    Avature url in state and a LinkedIn one in clean. So: take whichever of the
    two is a posting this module can parse, clean first, and report both when
    neither is.
    """
    candidates: list[tuple[str, str]] = []
    if CLEAN.exists():
        clean = pd.read_parquet(CLEAN, columns=["job_id", "url"])
        row = clean.loc[clean["job_id"] == job_id]
        if not row.empty:
            candidates.append(("clean.parquet", str(row.iloc[0]["url"] or "")))
    if state:
        candidates.append(("state.yaml", str(state.get("url") or "")))

    if not candidates:
        raise ApplyCliError(
            f"{job_id} is in neither clean.parquet nor pipeline/{job_id}/state.yaml"
        )
    for _, url in candidates:
        # submittable, not merely recognized: Workday parses cleanly and is
        # still manual-apply.
        if is_auto_submittable(url):
            return url

    seen = "; ".join(f"{where}: {url or '(empty)'}" for where, url in candidates)
    raise ManualApplyOnly(
        f"{job_id} has no Greenhouse, Lever or Ashby posting URL to apply through "
        f"({seen}). Not one of the boards /apply submits to — apply by hand."
    )


def resolve_out_dir(job_id: str) -> Path:
    """The folder the role's resume was last built in, as `app dir` resolves it."""
    try:
        return app_cli._built_dir(job_id)
    except app_cli.AppError as exc:
        raise ApplyCliError(str(exc)) from exc


def _playwright_available() -> bool:
    """A separate, monkeypatchable seam so tests never depend on whether the
    optional `apply` dependency group is actually installed.

    Delegates to `fill._require_playwright()` rather than importing
    `playwright` here directly — `test_no_module_but_fill_names_the_driver`
    enforces that only fill.py may name that import, so every other module
    stays importable without the `apply` extras group."""
    try:
        _require_playwright()
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
    state_path = state_io.state_path_for(PIPELINE, job_id)
    if not state_path.exists():
        print(f"ERROR: {state_path} missing.", file=sys.stderr)
        return 1
    if not DEFAULT_PATH.exists():
        print(f"ERROR: {DEFAULT_PATH} missing. Copy {EXAMPLE_PATH} and fill it in.",
              file=sys.stderr)
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
        info = app_cli.role_info(job_id)
    except (ApplyCliError, app_cli.AppError) as exc:
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
    for key in ("JOB_ID", "STATE", "OUT_DIR", "VERTICAL", "COMPANY_ANSWERS",
                "OVERRIDES_FILE"):
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
            raise ApplyCliError(f"{where} has unknown keys {sorted(unknown)}; "
                                f"allowed: {', '.join(OVERRIDE_KEYS)}")
        fact = entry.get("fact")
        if fact is not None:
            if (not isinstance(fact, dict) or set(fact) != {"fact", "yes_when"}
                    or fact["fact"] not in FACT_WORDING_FACTS
                    or not isinstance(fact["yes_when"], bool)):
                raise ApplyCliError(
                    f"{where} fact is {{\"fact\": one of "
                    f"{', '.join(FACT_WORDING_FACTS)}, \"yes_when\": true|false}}")
            overrides[field_id] = Override(value="", source=str(entry.get("source") or ""),
                                           fact=dict(fact))
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
        overrides[field_id] = Override(value=value, source=str(entry.get("source") or ""),
                                       question=question, claim=claim, keyword=keyword)
    return overrides


def answers_confirmed(path: Path | None) -> bool:
    """Whether the user confirmed this answers file (/apply's step 6 writes
    `"confirmed": true`). Only a confirmed file teaches the bank or lets a
    keyword guess through a submit."""
    if path is None:
        return False
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")).get("confirmed") is True
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


def _salary_expectation(vertical: str) -> dict:
    verticals_path = paths.PROFILE / "verticals.yaml"
    if not vertical or not verticals_path.is_file():
        return {}
    config = yaml.safe_load(verticals_path.read_text(encoding="utf-8")) or {}
    block = (config.get("verticals") or {}).get(vertical) or {}
    return block.get("salary_expectation") or {}


#: A posted minimum below this is a rate per hour or month, not per year.
MIN_ANNUAL_SALARY = 20000


def salary_for(job_id: str, vertical: str, country: str) -> float | None:
    """The figure to ask for: this posting's own USD minimum times the lane's
    `markup_pct` for a US role, else the lane's `fallback` for this country.

    `fallback` is a number (a US figure) or a mapping of country to number.
    None when nothing is configured for this country: the question parks.
    """
    expectation = _salary_expectation(vertical)
    if not expectation:
        return None
    us = country == "United States"
    markup = expectation.get("markup_pct")
    if us and markup is not None and CLEAN.exists():
        clean = pd.read_parquet(CLEAN)
        if {"salary_min", "salary_currency"} <= set(clean.columns):
            row = clean.loc[clean["job_id"] == job_id]
            if not row.empty:
                salary_min = row.iloc[0]["salary_min"]
                currency = str(row.iloc[0]["salary_currency"] or "").strip().upper()
                # Under this is an hourly or monthly rate, not a year's pay:
                # the lane's fallback answers instead.
                if currency == "USD" and not pd.isna(salary_min) and \
                        salary_min >= MIN_ANNUAL_SALARY:
                    return float(salary_min) * (1 + float(markup) / 100)
    fallback = expectation.get("fallback")
    if isinstance(fallback, dict):
        value = fallback.get(country)
        return float(value) if value is not None else None
    return float(fallback) if fallback is not None and us else None


def job_country(state: dict | None, answers: Answers) -> str:
    """The job's country: from discovery's location for it (`clean.parquet`,
    already cleaned), else `state.yaml`'s copy, else its title's residency
    qualifier ("(Germany)"), else the one country its JD names. A location that
    reads as no country is `UNKNOWN_COUNTRY`; no location at all, or "Remote",
    is "" (the candidate's own country applies)."""
    state = state or {}
    job_id = str(state.get("job_id") or "")
    try:
        from src.discovery.location import parse_location, residency_countries
    except Exception:  # noqa: BLE001 - the parser is optional plumbing
        return ""
    for raw in (_clean_value(job_id, "location"), str(state.get("location") or "")):
        try:
            parsed = parse_location(raw)
        except Exception:  # noqa: BLE001 - libpostal missing: no country, not a crash
            continue
        if parsed.country:
            return parsed.country
        if answers.home_country in parsed.candidate_countries:
            return answers.home_country
    raw = _clean_value(job_id, "location") or str(state.get("location") or "")
    found = _pieces_country(raw, parse_location) or _code_country(raw)
    if found:
        return found
    named = residency_countries(str(state.get("title") or ""))
    if len(named) == 1:
        return next(iter(named))
    found = _jd_country(job_id)
    if found:
        return found
    if _US_STATE_START.match(raw.strip()):
        return "United States"
    # A place was given and nothing could read it: guessing home would answer
    # a Polish job's work-authorization questions as a US one.
    if raw.strip() and not _NO_PLACE.search(raw):
        return UNKNOWN_COUNTRY
    return ""


def _us_state_start() -> re.Pattern:
    import pycountry

    codes = "|".join(sub.code.split("-")[1]
                     for sub in pycountry.subdivisions.get(country_code="US"))
    return re.compile(r"^(?:USA?-)?(?:" + codes + r")(?:\b|$)")


_US_STATE_START = _us_state_start()
# A location that says how, not where: the candidate's own country applies.
_NO_PLACE = re.compile(r"\b(?:remote|remoto|remota|hybrid|anywhere|work from home|wfh)\b",
                       re.IGNORECASE)


def _pieces_country(raw: str, parse_location) -> str:
    """The one country the pieces of a location name, when the whole string
    parses as none ("IN Pune", "USA-CA San Jose", "Wrocław, DO, PL")."""
    from src.apply.answers import places_named

    if re.match(r"^\s*(?:US|USA|U\.S\.A?\.?|United States)\b", raw):
        return "United States"
    named = {p for p in places_named(raw)
             if p not in ("European Union", "Latin America", "Asia Pacific")}
    if len(named) == 1:
        # A country the location names outright beats a city that could be
        # in two ("Santa Clara" is in California and in Cuba).
        return named.pop()
    found = set(named)
    words = [w for w in re.split(r"[\s,;/|()-]+", raw) if w]
    for n in (3, 2, 1):
        for i in range(len(words) - n + 1):
            piece = " ".join(words[i:i + n])
            if len(piece) < 4:
                continue
            try:
                country = parse_location(piece).country
            except Exception:  # noqa: BLE001 - libpostal missing
                return ""
            if country:
                found.add(country)
    return found.pop() if len(found) == 1 else ""


def _code_country(raw: str) -> str:
    """A country code closing the location ("München, BY, DE", "SGP Work-at-Home",
    "TUN - ARIANA"), or a US state's full name ("Northern Virginia"). A two-letter
    code that is also a US state ("IN", "IL") counts only after a region code
    that is not one ("MH, IN" is Maharashtra, India; "Chicago, IL" is Illinois)."""
    import pycountry

    us_codes = {sub.code.split("-")[1] for sub in pycountry.subdivisions.get(country_code="US")}
    us_names = [sub.name.casefold() for sub in pycountry.subdivisions.get(country_code="US")]
    text = raw.strip()
    if any(re.search(rf"\b{re.escape(n)}\b", text.casefold()) for n in us_names):
        return "United States"
    parts = [p.strip() for p in re.split(r"[,;]", text) if p.strip()]
    tokens = re.findall(r"\b[A-Z]{2,3}\b", text)
    for code in ([parts[-1]] if parts else []) + tokens[:1]:
        if len(code) == 3:
            country = pycountry.countries.get(alpha_3=code)
            if country:
                return country.name
        if len(code) == 2 and code not in ("US",):
            country = pycountry.countries.get(alpha_2=code)
            if country is None:
                continue
            if code not in us_codes:
                return country.name
            if len(parts) >= 2 and re.fullmatch(r"[A-Z]{2}", parts[-2]) \
                    and parts[-2] not in us_codes:
                return country.name
    return ""


def _clean_value(job_id: str, column: str) -> str:
    if not job_id or not CLEAN.exists():
        return ""
    import pyarrow.parquet as pq

    if column not in pq.read_schema(CLEAN).names:
        return ""
    clean = pd.read_parquet(CLEAN, columns=["job_id", column])
    row = clean.loc[clean["job_id"] == job_id]
    return "" if row.empty else str(row.iloc[0][column] or "")


# "must be based in Germany", "open to candidates located in India": the JD
# saying where the hire works, not every country it happens to mention.
_RESIDENCY_PHRASE = re.compile(
    r"(?:based|located|residing|reside|live|living|work(?:ing)?\s+from|remote|"
    r"authori[sz]ed\s+to\s+work|eligible\s+to\s+work|hiring)\s+(?:only\s+)?"
    r"(?:in|within|from|across)\s+(?:the\s+)?([^.;\n]{2,40})",
    re.IGNORECASE)


def _jd_country(job_id: str) -> str:
    """The one country the JD says the hire works in, for a posting whose
    location says only "Remote". Two countries, or none, is no answer."""
    from src.apply.answers import places_named

    countries = set()
    for m in _RESIDENCY_PHRASE.finditer(_clean_value(job_id, "jd_text")):
        countries |= {p for p in places_named(m.group(1))
                      if p not in ("European Union", "Latin America", "Asia Pacific")}
    return countries.pop() if len(countries) == 1 else ""


def company_answers_for(out_dir: Path) -> dict[str, str]:
    path = out_dir / "company_answers.yaml"
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {k: str(v).strip() for k, v in data.items()
            if isinstance(k, str) and isinstance(v, str) and v.strip()}


def build(job_id: str, url: str | None = None, out_dir: Path | None = None,
          answers_path: Path | None = None) -> tuple[Plan, "Answers"]:
    """Everything `plan` does, minus the printing.

    Returns the answer bank alongside the plan: the fill re-resolves a parked
    select once the browser can read its real options.
    """
    state = state_io.load_state(state_io.state_path_for(PIPELINE, job_id))
    posting_url = url or resolve_url(job_id, state)
    target = Path(out_dir) if out_dir else resolve_out_dir(job_id)
    answers = load_answers()
    tailored_dirs = (state or {}).get("tailored_dirs") or []
    vertical = tailored_dirs[-1].split("/", 1)[0] if tailored_dirs else ""
    answers = replace(answers, job_source=str((state or {}).get("source") or ""),
                      job_country=job_country(state, answers),
                      company_answers=company_answers_for(target))
    answers = replace(answers, salary_figure=salary_for(job_id, vertical, answers.country))
    overrides = load_overrides(Path(answers_path), job_id) if answers_path else None

    ats = detect_ats(posting_url)
    if ats not in SUBMITTABLE_ATS:
        raise ApplyCliError(f"{posting_url}: not a Greenhouse, Lever or Ashby posting URL")
    if ats == "lever":
        board = lever.load_board(posting_url)
        return plan_for_board(board, answers, target, job_id=job_id, overrides=overrides,
                              ats="lever", requires_captcha=board.requires_captcha), answers
    if ats == "ashby":
        # Read through Ashby's GraphQL form API, not its HTML.
        board = ashby.load_board(posting_url)
        return plan_for_board(board, answers, target, job_id=job_id, overrides=overrides,
                              ats="ashby"), answers
    board = load_board(posting_url)
    return plan_for_board(board, answers, target, job_id=job_id, overrides=overrides), answers


# Every exception `build()` can raise that names a per-role problem rather
# than a bug in this CLI. `PostingExpired` is handled separately — it is an
# ordinary outcome, not a failure.
BUILD_ERRORS = (ApplyCliError, ApplyUrlError, AnswersError, PlanError, CareersError,
                 SchemaError, DomScanError, ReconcileError)


# ------------------------------------------------------------------ rendering


def _value(value) -> str:
    if isinstance(value, bool):
        return "checked" if value else "unchecked"
    if isinstance(value, tuple):
        return ", ".join(value)
    return str(value)


def as_dict(plan: Plan) -> dict:
    return {
        "job_id": plan.job_id,
        "board": plan.board,
        "token": plan.token,
        "form_url": plan.form_url,
        "company": plan.company,
        "title": plan.title,
        "out_dir": str(plan.out_dir),
        "submit_selector": plan.submit_selector,
        "submit_disabled": plan.submit_disabled,
        "parked": plan.parked,
        "submittable": plan.submittable,
        "work_authorization": plan.work_authorization,
        "fields": [
            {"id": f.id, "name": f.name, "label": f.label, "kind": f.kind,
             "section": f.section, "required": f.required, "multi": f.multi,
             "tier": f.tier,
             "value": list(f.value) if isinstance(f.value, tuple) else f.value,
             "assert_selected": f.needs_selection_assert, "description": f.description,
             "options": list(f.options), "source": f.source, "reason": f.reason,
             "replaced": f.replaced}
            for f in plan.fields
        ],
        "files": [
            {"id": f.id, "name": f.name, "label": f.label, "required": f.required,
             "path": str(f.path)}
            for f in plan.files
        ],
        "unmapped": [
            {"id": u.id, "label": u.label, "required": u.required, "kind": u.kind,
             "section": u.section, "tier": u.tier, "reason": u.reason,
             "options": list(u.options), "description": u.description,
             "multi": u.multi, "source": u.source, "similar": list(u.similar)}
            for u in plan.unmapped
        ],
        "draftable": [
            {"id": d.id, "label": d.label, "kind": d.kind, "section": d.section,
             "options": list(d.options), "description": d.description,
             "required": d.required, "tier": d.tier, "reason": d.reason,
             "multi": d.multi, "source": d.source, "similar": list(d.similar)}
            for d in plan.draftable
        ],
        "skipped": [
            {"id": s.id, "label": s.label, "tier": s.tier, "reason": s.reason}
            for s in plan.skipped
        ],
        "api_only": list(plan.api_only),
        "ats": plan.ats,
        "requires_captcha": plan.requires_captcha,
        "ignored_overrides": list(plan.ignored_overrides),
    }


def _ignored_override_lines(plan: Plan) -> list[str]:
    if not plan.ignored_overrides:
        return []
    return ["", f"OVERRIDES IGNORED ({len(plan.ignored_overrides)}) — supplied, "
                "validated, and not applied:",
            *(f"  - {note}" for note in plan.ignored_overrides)]


def render(plan: Plan) -> str:
    lines = [
        f"{plan.company or '(unknown company)'} — {plan.title or '(unknown title)'}",
        f"job_id {plan.job_id}   board {plan.board}   token {plan.token}",
        f"form    {plan.form_url}",
        f"tailor  {plan.out_dir}",
        "",
        f"FILL ({len(plan.fields)})",
    ]
    for f in plan.fields:
        flag = "*" if f.required else " "
        assertion = "  [assert selection]" if f.needs_selection_assert else ""
        lines.append(f"  {flag} {f.id:<28} [{f.tier}] {_value(f.value)}{assertion}")
        lines.append(f"      {f.label}")

    lines.append("")
    lines.append(f"ATTACH ({len(plan.files)})")
    for f in plan.files:
        flag = "*" if f.required else " "
        lines.append(f"  {flag} {f.id:<28} {f.path.name}")

    lines.append("")
    lines.append(f"DRAFTABLE, optional and unmatched ({len(plan.draftable)})")
    for d in plan.draftable:
        lines.append(f"    {d.id:<28} {d.label}")
        if d.description:
            lines.append(f"      note: {d.description}")
        if d.options:
            lines.append(f"      offers: {', '.join(d.options)}")

    lines.append("")
    lines.append(f"SKIPPED, optional ({len(plan.skipped)})")
    for s in plan.skipped:
        lines.append(f"    {s.id:<28} [{s.tier}] {s.reason}")

    lines.append("")
    lines.append(f"UNMAPPED ({len(plan.unmapped)})")
    for u in plan.unmapped:
        flag = "*" if u.required else " "
        lines.append(f"  {flag} {u.id:<28} [{u.tier}] {u.reason}")
        lines.append(f"      {u.label}")
        if u.description:
            lines.append(f"      note: {u.description}")
        if u.options:
            lines.append(f"      offers: {', '.join(u.options)}")

    lines.append("")
    if plan.parked:
        required = len(plan.required_parked)
        lines.append(
            f"PARKED: {len(plan.unmapped)} unanswered question(s), {required} required. "
            "Nothing would be submitted."
        )
    else:
        lines.append("READY: every rendered field resolved. --submit would click.")
    if plan.api_only:
        lines.append(f"(declared by the API, rendered nowhere: {', '.join(plan.api_only)})")
    lines.extend(_ignored_override_lines(plan))
    return "\n".join(lines)


def _offered(options: tuple[str, ...], limit: int = 6) -> str:
    if not options:
        return ""
    shown = ", ".join(options[:limit])
    return shown if len(options) <= limit else f"{shown}, +{len(options) - limit} more"


def render_questions(plan: Plan) -> str:
    """The form grouped by who answers it: known facts, keyword guesses to
    confirm, questions the bank has never seen, and what blocks the submit."""
    lines = [
        f"{plan.company or '(unknown company)'} — {plan.title or '(unknown title)'}",
        f"job_id {plan.job_id}   {plan.ats}/{plan.board}   {len(plan.fields)} filled, "
        f"{len(plan.files)} attached, {len(plan.unmapped)} unanswered",
        "",
    ]
    guesses = [f for f in plan.fields if f.source.startswith("bank:") and "~" in f.source]
    known = [f for f in plan.fields if f not in guesses]

    lines.append(f"KNOWN — answered from the bank or your facts ({len(known)})")
    for f in known:
        flag = "*" if f.required else " "
        via = f"  via {f.source}" if f.source else ""
        lines.append(f"  {flag} [{f.tier}] {f.label}")
        lines.append(f"      -> {_value(f.value)}{via}")
        if f.options:
            lines.append(f"      of {len(f.options)} offered: {_offered(f.options)}")

    lines.append("")
    lines.append(f"GUESSED BY KEYWORD — confirm or correct ({len(guesses)})")
    for f in guesses:
        flag = "*" if f.required else " "
        lines.append(f"  {flag} [{f.id}] {f.label}")
        lines.append(f"      -> {_value(f.value)}  via {f.source}")
        if f.options:
            lines.append(f"      of {len(f.options)} offered: {_offered(f.options, limit=10)}")

    new = [*plan.draftable, *(u for u in plan.unmapped if u.tier == "C")]
    lines.append("")
    lines.append(f"NEW — not in the bank yet ({len(new)})")
    for q in new:
        flag = "*" if q.required else " "
        lines.append(f"  {flag} [{q.id}] {q.label}")
        if q.options:
            lines.append(f"      choose from: {_offered(q.options, limit=10)}")
        if q.description:
            lines.append(f"      note: {q.description}")
        if q.similar:
            lines.append(f"      closest in the bank: {', '.join(q.similar)}")

    blockers = [u for u in plan.unmapped if u.required or u.blocks]
    lines.append("")
    lines.append(f"BLOCKING — required and unanswered ({len(blockers)})")
    for u in blockers:
        lines.append(f"    [{u.tier}] [{u.id}] {u.label}")
        lines.append(f"      {u.reason}")
        if u.options:
            lines.append(f"      offers: {_offered(u.options, limit=10)}")

    if plan.skipped:
        lines.append("")
        lines.append(f"LEFT BLANK, optional ({len(plan.skipped)})")
        for sk in plan.skipped:
            lines.append(f"    [{sk.tier}] {sk.label}: {sk.reason}")

    lines.append("")
    lines.append("PARKED: nothing would be submitted." if plan.parked
                 else "READY: every required field resolved. --submit would click.")
    lines.extend(_ignored_override_lines(plan))
    return "\n".join(lines)


# ------------------------------------------------------------------ commands


def _cmd_plan(args: argparse.Namespace) -> int:
    try:
        plan, _ = build(args.job_id, url=args.url, out_dir=args.out_dir,
                        answers_path=args.answers)
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
    from src.apply import sheet

    try:
        counts = sheet.refresh(add=args.add)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(" · ".join(f"{status} {n}" for status, n in sorted(counts.items())))
    return 0


def _cmd_learn(args: argparse.Namespace) -> int:
    """Review or undo what the bank holds. Runs learn on their own; this is
    for looking and taking back."""
    path = Path(args.path) if args.path else None
    try:
        if args.undo or args.undo_claim or args.undo_fact:
            print(bank.undo(args.undo or "", wording=args.wording or "",
                            keyword=args.keyword or "", claim=args.undo_claim or "",
                            fact=args.undo_fact or "",
                            path=path))
            return 0
        for line in bank.review(path, since_job=args.job or ""):
            print(line)
    except (bank.BankError, AnswersError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def render_fill(result, plan: Plan) -> str:
    done = sum(1 for o in result.outcomes if o.action != "failed")
    lines = [f"\nFILLED {done} / {len(result.outcomes)} — {plan.form_url}"]
    for outcome in result.outcomes:
        mark = "ok  " if outcome.action != "failed" else "FAIL"
        was = f"   (was {outcome.before!r})" if outcome.was_prefilled else ""
        lines.append(f"  {mark} {outcome.id:<26} {outcome.after or outcome.note}{was}")
    if result.observed_options:
        lines.append("\nOptions read off the opened widgets:")
        for field_id, options in result.observed_options.items():
            lines.append(f"  {field_id}: {list(options)}")
    if result.recovered:
        lines.append(
            f"\nRecovered at fill time (parked, then resolved once the widget's "
            f"real options could be read): {list(result.recovered)}"
        )
    if result.prefilled:
        lines.append(f"\nPrefilled before we wrote (upload parse?): {list(result.prefilled)}")
    drifted = [r for r in result.manifest if not r["matches"] and r["group"] == "filled"]
    if drifted:
        lines.append(f"\n{len(drifted)} FIELD(S) THE PAGE DISAGREES WITH:")
        for row in drifted:
            lines.append(f"  - {row['id']}: planned {row['planned']!r}, "
                         f"page holds {row['actual']!r}")
    occupied = [r for r in result.manifest
                if r["group"] in ("parked", "left blank", "skipped") and r["actual"]]
    if occupied:
        lines.append(f"\n{len(occupied)} UNANSWERED QUESTION(S) THE PAGE ALREADY "
                     f"HAS A VALUE FOR:")
        for row in occupied:
            lines.append(f"  - {row['id']} [{row['group']}]: {row['actual']!r} "
                         f"— {row['label'][:60]}")
    if result.failures:
        lines.append(f"\n{len(result.failures)} FAILURE(S):")
        lines.extend(f"  - {f}" for f in result.failures)
    return "\n".join(lines)


# ------------------------------------------------------------------ queue


def eligible_queue(pipeline_dir: Path | None = None) -> list[str]:
    """`state == tailored` with a resume on file, and whose latest submit-ledger
    line (if any) is `refused` — a role already sent to a board never
    re-enters the queue, even if its state.yaml was re-created.

    A cover letter is not required here: whether one is needed depends on what
    the board's form asks, which `/apply`'s step 3 (Materials) decides per
    role. A role whose board requires a cover letter it doesn't have
    surfaces via `build_plan()`'s `unmapped`/`parked` outcome, the same
    mechanism that blocks any other missing required field. Sorted by job_id:
    the only ordering signal every eligible role is guaranteed to carry, so
    the queue is reproducible run to run.

    Defaults to the module-level `PIPELINE`, looked up at call time — a
    default *parameter value* would freeze the original `paths.PIPELINE`
    object and miss a test's `monkeypatch.setattr(apply_cli, "PIPELINE", ...)`.
    """
    idx = state_io.load_state_index(pipeline_dir if pipeline_dir is not None else PIPELINE)
    sent = ledger.sent_job_ids(ledger_path())
    return sorted(
        job_id for job_id, st in idx.items()
        if st.get("state") == "tailored"
        and st.get("tailored_dirs")
        and job_id not in sent
    )


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
    state = state_io.load_state(state_io.state_path_for(PIPELINE, job_id))
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
        print(f"WARNING: --rate {seconds:g}s is below the {MIN_RATE_SECONDS:g}s "
              f"floor; using {MIN_RATE_SECONDS:g}s.", file=sys.stderr)
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


def _track_applied(job_id: str, plan) -> tuple[int, str]:
    """Write the `applied` transition through /track (R10). Returns
    `(returncode, note)` — never raises, because by the time this runs the
    application is already on the board and losing that fact is worse than
    any state-write error."""
    try:
        rc = track_cli.main([job_id, "applied", "--note",
                             f"auto-submitted via /apply ({plan.board})"])
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
            lessons.append(bank.Lesson(label=f.label, value=f.value, claim=override.claim,
                                       job_id=job_id))
            continue
        if override.fact is not None:
            lessons.append(bank.Lesson(label=f.label, value=f.value, fact=override.fact,
                                       job_id=job_id))
            continue
        if not override.question:
            continue
        misfired = f.replaced[5:].split("~", 1)[0] if f.replaced.startswith("bank:") else ""
        lessons.append(bank.Lesson(
            label=f.label, question=override.question, value=f.value, offered=f.options,
            misfired=misfired, source=override.source, job_id=job_id,
            keyword=override.keyword))
    for f in plan.fields:
        if f.id not in overrides and f.source.startswith("bank:") and "~" in f.source:
            lessons.append(bank.Lesson(
                label=f.label, question=f.source[5:].split("~", 1)[0], value=f.value,
                offered=f.options, job_id=job_id))
    return lessons


def _run_role(job_id: str, *, submit: bool, headless: bool,
              answers_path: Path | None = None,
              manifest_path: Path | None = None) -> RunOutcome:
    """One role, start to finish. Never raises — every failure mode comes back
    as a category on the outcome, so one bad role cannot stop the queue.

    `answers_path` is the session's confirmed answers for a single-role run.
    What they teach is saved to the answer bank before the browser opens, so it
    holds whether or not this form goes on to submit.
    """
    try:
        plan, answers = build(job_id, answers_path=answers_path)
    except PostingExpired as exc:
        return RunOutcome(job_id, category="expired", detail=str(exc))
    except ManualApplyOnly as exc:
        return RunOutcome(job_id, category="manual", detail=str(exc))
    except BUILD_ERRORS as exc:
        return RunOutcome(job_id, category="failed", detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - see the docstring's "never raises"
        return RunOutcome(job_id, category="failed",
                           detail=f"unexpected {type(exc).__name__}: {exc}")

    if plan.ignored_overrides:
        return RunOutcome(job_id, plan.company, plan.title, "failed",
                          detail="answers that cannot apply: "
                                 + "; ".join(plan.ignored_overrides))

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
            job_id, plan.company, plan.title, "parked",
            detail=f"{len(guesses)} keyword guess(es) not confirmed — run /apply {job_id}",
            unmapped=tuple({"id": f.id, "label": f.label, "tier": "B",
                            "reason": f"guessed {f.value!r} {f.source}", "source": f.source}
                           for f in guesses))
    outcome = _attempt(job_id, plan, answers, submit=submit, headless=headless,
                       manifest_path=manifest_path)
    from_bank = sum(1 for f in plan.fields if f.source.startswith("bank:"))
    return replace(outcome, from_bank=from_bank, learned=learned)


def _attempt(job_id: str, plan: Plan, answers, *, submit: bool, headless: bool,
             manifest_path: Path | None) -> RunOutcome:
    """The browser half of `_run_role`: fill, and click only when asked."""
    if submit and headless and plan.requires_captcha:
        # Lever renders hCaptcha on every form and `submit()` blocks for a
        # human to solve it. Headless there means the wait can only ever time
        # out — 10 minutes per role, after the click has already landed.
        return RunOutcome(
            job_id, plan.company, plan.title, "failed",
            detail=f"{plan.ats} renders a captcha and --headless leaves nobody to "
                   f"solve it; re-run this role without --headless",
        )

    if not has_driver(plan.ats):
        # Reported rather than attempted, so the browser never opens for a
        # board that cannot be filled.
        return RunOutcome(job_id, plan.company, plan.title, "manual",
                           detail=f"{plan.ats}: form scanned and planned, but no "
                                  f"browser driver exists yet — apply by hand")

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
                job_id, plan.company, plan.title, "skipped",
                detail=f"already sent {sent.get('at', '')} ({sent.get('status', '')}) "
                       f"per {ledger.LEDGER_NAME}; delete that line to retry",
            )

    clicked: list[bool] = []

    def mark(status: str, detail: str = "") -> None:
        ledger.append(ledger_path(), job_id, status, form_url=plan.form_url,
                      company=plan.company, title=plan.title, detail=detail)

    def before_click() -> None:
        clicked.append(True)
        mark("clicking")

    sink: list = []
    try:
        result = run_one(plan, answers, headless=headless, submit_after=submit,
                          sink=sink, after=None if submit else review,
                          before_click=before_click)
    except BaseException as exc:  # noqa: BLE001 - Ctrl-C must not lose a submit
        landed = sink[0] if sink else None
        # The manifest is read before the click, so a crash after it still has
        # one worth keeping — it is the only record of what the form held.
        if manifest_path is not None and landed is not None and landed.manifest:
            try:
                write_manifest(landed, plan, Path(manifest_path))
            except OSError:
                pass
        if landed is not None and landed.submitted:
            mark("ambiguous", f"{type(exc).__name__} after the click")
            rc, exc_note = _track_applied(job_id, plan)
            if rc:
                return RunOutcome(
                    job_id, plan.company, plan.title, "submitted_untracked",
                    detail=f"SUBMITTED, then {type(exc).__name__}: {exc}, and "
                           f"track_cli exited {rc}{exc_note} — state.yaml still says "
                           f"tailored; fix by hand",
                )
            return RunOutcome(
                job_id, plan.company, plan.title, "submitted_unconfirmed",
                detail=f"SUBMITTED, then {type(exc).__name__}: {exc} — the click "
                       f"landed; verify by hand",
            )
        if isinstance(exc, Exception):
            # A playwright timeout or driver error must not escape:
            # `run_queue` would abort mid-walk and every role already
            # submitted would lose its only record.
            return RunOutcome(job_id, plan.company, plan.title, "failed",
                               detail=f"{type(exc).__name__}: {exc}")
        raise

    if manifest_path is not None:
        try:
            write_manifest(result, plan, Path(manifest_path))
        except OSError as exc:
            print(f"WARNING: could not write the manifest: {exc}", file=sys.stderr)

    if clicked and not result.submitted and result.outcome not in ("rejected", "unsent"):
        # The board declined every click, or a captcha was never solved: it
        # has nothing, so the role may be tried again.
        mark("refused", result.submit_error)
    if result.outcome in ("rejected", "unsent"):
        mark(result.outcome, result.submit_error)
        return RunOutcome(job_id, plan.company, plan.title, result.outcome,
                           detail=result.submit_error or "clicked; no submit request "
                                  "fired — verify on the board by hand",
                           evidence=result.evidence)

    if result.submitted:
        mark(result.outcome or "ambiguous")
        rc, exc_note = _track_applied(job_id, plan)
        if rc:
            # The application is on the board but the state write refused or
            # blew up. The ledger line already keeps it out of the next run.
            return RunOutcome(
                job_id, plan.company, plan.title, "submitted_untracked",
                detail=f"SUBMITTED but track_cli exited {rc}{exc_note} — state.yaml "
                       f"still says tailored; fix by hand",
            )
        if result.confirmed:
            return RunOutcome(job_id, plan.company, plan.title, "submitted",
                               evidence=result.evidence)
        return RunOutcome(
            job_id, plan.company, plan.title, "submitted_unconfirmed",
            detail="clicked and transitioned to applied, but the board showed no "
                   "confirmation this code recognises — verify by hand",
            evidence=result.evidence,
        )

    blocking = blocking_details(plan, result)
    if blocking:
        return RunOutcome(job_id, plan.company, plan.title, "parked",
                           detail=f"{len(blocking)} required question(s) unresolved",
                           unmapped=blocking)
    if result.failures:
        return RunOutcome(job_id, plan.company, plan.title, "failed",
                           detail="; ".join(result.failures))
    if result.submit_error:
        # Not a park (nothing is unmapped) and not a fill failure — the guard
        # refused for a form-shape reason instead (no submit button, or it
        # stayed disabled). Distinct enough from either to say so.
        return RunOutcome(job_id, plan.company, plan.title, "failed",
                           detail=result.submit_error)

    return RunOutcome(job_id, plan.company, plan.title, "ready",
                       detail="every field resolved; rerun with --submit to click")


def queue_companies(job_ids: list[str], pipeline_dir: Path | None = None) -> dict[str, str]:
    """`job_id -> company`, read from state.yaml. Empty for anything the index
    does not carry; the per-company cap treats that as "unknown", never as a
    match."""
    idx = state_io.load_state_index(pipeline_dir if pipeline_dir is not None else PIPELINE)
    return {job_id: str(idx.get(job_id, {}).get("company") or "") for job_id in job_ids}


def run_queue(job_ids: list[str], *, submit: bool, headless: bool = False,
              rate: float = 240.0, jitter: float = 60.0,
              sleeper=time.sleep, jitter_fn=random.uniform,
              answers_path: Path | None = None,
              manifest_path: Path | None = None,
              on_outcome=None,
              collect_into: list | None = None,
              companies: dict[str, str] | None = None) -> list[RunOutcome]:
    """Walk the queue, sleeping `rate + U(0, jitter)` seconds before each
    attempted role after the first.

    At most `MAX_SUBMISSIONS_PER_COMPANY` role per company is submitted per
    run: once a company's application goes out, every later role at that
    company is reported `skipped` and never opens a browser. Only applies when
    `submit` is set — a fill-and-stop walk sends nothing.

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
        companies = queue_companies(job_ids) if submit else {}
    submitted_per_company: dict[str, int] = {}
    attempted = 0
    for job_id in job_ids:
        known = companies.get(job_id, "")
        key = normalize_company(known)
        if submit and key and submitted_per_company.get(key, 0) >= MAX_SUBMISSIONS_PER_COMPANY:
            outcomes.append(RunOutcome(
                job_id, known, "", "skipped",
                detail=f"{known}: already submitted to this company in this run",
            ))
            if on_outcome is not None:
                on_outcome(outcomes)
            continue

        if attempted:
            sleeper(rate + jitter_fn(0, jitter))
        attempted += 1
        outcome = _run_role(job_id, submit=submit, headless=headless,
                             answers_path=answers_path,
                             manifest_path=manifest_path)
        outcomes.append(outcome)
        if outcome.category in _CLICKED:
            # Count both names: the plan's is what actually went out, the
            # index's is what the next role will be checked against.
            for counted in {normalize_company(outcome.company), key} - {""}:
                submitted_per_company[counted] = \
                    submitted_per_company.get(counted, 0) + 1
        if on_outcome is not None:
            on_outcome(outcomes)
    return outcomes


# ------------------------------------------------------------------ report


_CATEGORY_ORDER = ("submitted_untracked", "submitted", "submitted_unconfirmed",
                    "rejected", "unsent", "parked", "ready", "manual", "skipped", "failed", "expired")
_CATEGORY_TITLE = {
    "submitted": "Submitted",
    "submitted_unconfirmed": "Submitted (no confirmation seen — verify by hand)",
    "submitted_untracked": "SUBMITTED BUT NOT TRACKED — fix state.yaml by hand",
    "rejected": "Rejected by the board (not tracked — read its reason)",
    "unsent": "Clicked, but no submit request fired (not tracked — verify by hand)",
    "parked": "Parked",
    "ready": "Ready (not submitted)",
    "manual": "Manual-apply (no submit path for this board)",
    "skipped": "Skipped (already sent, or one submission per company per run)",
    "failed": "Failed",
    "expired": "Expired postings",
}

# Categories where a click reached the board; the per-company cap counts them.
_CLICKED = ("submitted", "submitted_unconfirmed", "submitted_untracked",
            "rejected", "unsent")

# Categories that make `apply run` exit non-zero. `manual` is deliberately not
# one: a board with no submit path is a fact about the board, not a failure.
_EXIT_NONZERO = ("failed", "submitted_untracked")


def _park_kind(row: dict) -> str:
    """Why a question parked, in the words that say what to do next."""
    if row.get("source") == "export_control" or "export control" in row.get("reason", ""):
        return "new question"
    if "~" in row.get("source", ""):
        return "guess to confirm"
    if row.get("tier") == "C":
        return "new question"
    return "needs a fact"


_TOTALS = re.compile(r"<!-- totals parked=(\d+) bank=(\d+) learned=(\d+) roles=(\d+) -->")


def _totals(outcomes: list[RunOutcome]) -> tuple[int, int, int, int]:
    parked = sum(1 for o in outcomes if o.category == "parked")
    return (parked, sum(o.from_bank for o in outcomes),
            sum(len(o.learned) for o in outcomes), len(outcomes))


def _trend(runs_dir: Path, limit: int = 10) -> list[str]:
    """One line per recent run that parked or learned anything, oldest first."""
    rows = []
    for report in sorted(runs_dir.glob("*.md"))[-limit:]:
        m = _TOTALS.search(report.read_text(encoding="utf-8", errors="replace"))
        if m:
            rows.append(f"  {report.stem}: parked {m[1]} of {m[4]} · from bank {m[2]} "
                        f"· learned {m[3]}")
    return rows


def render_report(outcomes: list[RunOutcome], started_at: datetime,
                  runs_dir: Path | None = None) -> str:
    lines = [
        f"# apply run — {started_at.isoformat(timespec='seconds')}",
        "",
        f"{len(outcomes)} role(s) attempted.",
        "",
    ]
    for category in _CATEGORY_ORDER:
        rows = [o for o in outcomes if o.category == category]
        if not rows:
            continue
        lines.append(f"## {_CATEGORY_TITLE[category]} ({len(rows)})")
        lines.append("")
        for o in rows:
            who = f"{o.company} — {o.title}".strip(" —") or o.job_id
            lines.append(f"- `{o.job_id}` {who}")
            if o.detail:
                lines.append(f"  - {o.detail}")
            for row in o.unmapped:
                lines.append(f"  - `{row['id']}` [{_park_kind(row)}] {row['label']}")
                lines.append(f"    - {row['reason']}")
                if row.get("options"):
                    lines.append(f"    - offers: {', '.join(row['options'])}")
                if row.get("description"):
                    lines.append(f"    - note: {row['description']}")
            if o.from_bank or o.learned:
                lines.append(f"  - answered from the bank: {o.from_bank}; "
                             f"learned: {len(o.learned)}")
            for change in o.learned:
                lines.append(f"    - {change}")
            if o.evidence:
                lines.append(f"  - what the board showed after the click: "
                             f"`{o.evidence}`")
        lines.append("")
    parked, from_bank, learned, roles = _totals(outcomes)
    lines.append(f"**parked {parked} · answered from bank {from_bank} · learned {learned} new**")
    trend = _trend(runs_dir) if runs_dir is not None and runs_dir.is_dir() else []
    if trend:
        lines.append("")
        lines.append("Recent runs:")
        lines.extend(trend)
    lines.append(f"<!-- totals parked={parked} bank={from_bank} learned={learned} roles={roles} -->")
    return "\n".join(lines)


def write_report(outcomes: list[RunOutcome], started_at: datetime,
                  out_dir: Path | None = None, path: Path | None = None) -> Path:
    if path is not None:
        out_dir = path.parent
    else:
        out_dir = out_dir if out_dir is not None else APPLY_RUNS
    out_dir.mkdir(parents=True, exist_ok=True)
    path = path or reserve_report_path(started_at, out_dir)
    path.write_text(render_report(outcomes, started_at, runs_dir=out_dir), encoding="utf-8")
    return path


def write_manifest(result, plan: Plan, path: Path) -> Path:
    """The post-fill read-back, as JSON, at a path the caller named.

    Only ever written when `--manifest` asked for it, and only for a run that
    actually opened a browser — an unasked-for artifact next to the run report
    would make "no run was started, so no report exists" untrue.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "job_id": plan.job_id,
        "company": plan.company,
        "title": plan.title,
        "form_url": plan.form_url,
        "landed_url": result.landed_url,
        "page_title": result.title,
        "submitted": result.submitted,
        "failures": list(result.failures),
        "recovered": list(result.recovered),
        "fields": result.manifest,
    }, indent=2), encoding="utf-8")
    return path


def reserve_report_path(started_at: datetime, out_dir: Path | None = None) -> Path:
    """A report path no existing run owns.

    This file is the only record of what went out, so two runs starting in the
    same second must not share a name. Reserved once per run and re-written
    per role, so the crash-survival writes keep hitting the same file.
    """
    out_dir = out_dir if out_dir is not None else APPLY_RUNS
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = started_at.strftime("%Y-%m-%dT%H-%M-%S")
    path = out_dir / f"{stem}.md"
    n = 2
    while path.exists():
        path = out_dir / f"{stem}.{n}.md"
        n += 1
    return path


def submission_preview(job_ids: list[str],
                        pipeline_dir: Path | None = None) -> list[tuple[str, str, str, str]]:
    """One `(job_id, company, title, board)` row per role `--submit` would
    actually apply to, in queue order.

    `MAX_SUBMISSIONS_PER_COMPANY` is applied here the same way `run_queue`
    applies it, so what the confirmation prompt shows is what the walk does.
    Reads state.yaml and clean.parquet only — no network, no browser.
    """
    idx = state_io.load_state_index(pipeline_dir if pipeline_dir is not None else PIPELINE)
    rows: list[tuple[str, str, str, str]] = []
    counted: dict[str, int] = {}
    for job_id in job_ids:
        state = idx.get(job_id, {}) or {}
        company = str(state.get("company") or "")
        key = normalize_company(company)
        if key and counted.get(key, 0) >= MAX_SUBMISSIONS_PER_COMPANY:
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


def confirm_submission(job_ids: list[str]) -> int:
    """Print the roles `--submit` will apply to, then require a typed yes.

    Returns 0 to proceed and 1 to abort. Called before a browser opens and
    before any state is written, so an abort sends nothing. A non-tty stdin
    fails closed: nobody can answer, so it refuses rather than proceeding.
    """
    rows = submission_preview(job_ids)
    print(f"About to SUBMIT {len(rows)} real application(s):")
    for job_id, company, title, board in rows:
        who = f"{company} — {title}".strip(" —") or "(unknown role)"
        print(f"  {job_id}  {who}  [{board}]")

    if not sys.stdin.isatty():
        print("ERROR: --submit needs confirmation and stdin is not a terminal. "
              "Pass --yes to confirm when running non-interactively.",
              file=sys.stderr)
        return 1
    try:
        reply = input("\nType 'yes' to submit these applications: ")
    except EOFError:
        reply = ""
    if reply.strip().lower() not in _CONFIRM_YES:
        print("ABORTED: nothing submitted.", file=sys.stderr)
        return 1
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    # Checked before anything opens a browser. --job-id is exempt: one named
    # role is already a bound of one.
    if args.submit and args.limit is None and not args.job_id:
        print("ERROR: --submit requires an explicit --limit (or --job-id for a "
              "single role). Without it this walks the entire eligible queue "
              "and applies to every role in it.", file=sys.stderr)
        return 1

    if args.answers and not args.job_id:
        print("ERROR: --answers only applies to a single role — pass --job-id",
              file=sys.stderr)
        return 1

    # One path, one role: a queue walk would have every role overwrite the
    # previous role's read-back at the same filename.
    if args.manifest and not args.job_id:
        print("ERROR: --manifest writes one file, so it only applies to a "
              "single role — pass --job-id", file=sys.stderr)
        return 1

    if args.job_id:
        queue = eligible_queue()
        if args.job_id not in queue:
            sent = ledger.prior(ledger_path(), args.job_id)
            why = (f"already sent {sent.get('at', '')} ({sent.get('status', '')}) per "
                   f"{ledger.LEDGER_NAME}; delete that line to retry" if sent
                   else "needs state == tailored with tailored_dirs[] non-empty")
            print(f"ERROR: {args.job_id} is not eligible — {why}", file=sys.stderr)
            return 1
        queue = [args.job_id]
    else:
        # Boards with no submit path are dropped before --limit counts, so a
        # limit of 5 means five roles that can actually be filled.
        queue = [j for j in eligible_queue() if _auto_submittable(j)]
        if args.limit is not None:
            queue = queue[:args.limit]

    if not queue:
        print("Nothing eligible: no role at state=tailored with "
              "tailored_dirs[] non-empty.")
        return 0

    if args.submit and not args.yes:
        rc = confirm_submission(queue)
        if rc:
            return rc

    try:
        rate = clamp_rate(parse_duration(args.rate))
        jitter = parse_duration(args.jitter)
    except ApplyCliError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    started = datetime.now()
    # The caller owns the list and the report is rewritten after every role, so
    # a crash — or a Ctrl-C — mid-walk still leaves a complete record of every
    # application that already went out.
    outcomes: list[RunOutcome] = []
    path = reserve_report_path(started)
    write_report(outcomes, started, path=path)
    try:
        run_queue(queue, submit=args.submit, headless=args.headless,
                   rate=rate, jitter=jitter, answers_path=args.answers,
                   manifest_path=args.manifest,
                   collect_into=outcomes,
                   on_outcome=lambda done: write_report(done, started, path=path))
    finally:
        write_report(outcomes, started, path=path)

    print(render_report(outcomes, started, runs_dir=path.parent))
    print(f"Report written to {path}")
    return 1 if any(o.category in _EXIT_NONZERO for o in outcomes) else 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        prog="apply", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prep = sub.add_parser("prepare", help="/apply's Step 1: validate prerequisites, "
                                           "derive out_dir/vertical/company_answers")
    prep.add_argument("job_id")
    prep.set_defaults(func=_cmd_prepare)

    p = sub.add_parser("plan", help="print the fill plan for one role")
    p.add_argument("job_id")
    p.add_argument("--json", action="store_true")
    p.add_argument("--questions", action="store_true",
                   help="group the form by who answers it (config vs judgment) "
                        "instead of by what fill.py does with it")
    p.add_argument("--url", default=None,
                   help="posting URL override, when the stored one is stale")
    p.add_argument("--out-dir", default=None, type=Path,
                   help="/tailor output dir override")
    p.add_argument("--answers", default=None, type=Path,
                   help="the /apply session's answers for this role")
    p.set_defaults(func=_cmd_plan)

    r = sub.add_parser("run", help="walk the eligible queue")
    r.add_argument("--limit", type=int, default=None,
                   help="most roles to walk; required with --submit unless --job-id")
    r.add_argument("--rate", default="4m",
                   help=f"delay between roles, e.g. '4m'; floored at "
                        f"{MIN_RATE_SECONDS:g}s")
    r.add_argument("--jitter", default="60s", help="added on top of --rate, uniformly")
    r.add_argument("--submit", action="store_true",
                   help="click submit on every role that resolves; default is fill-and-stop")
    r.add_argument("--yes", action="store_true",
                   help="skip the --submit confirmation prompt; required to "
                        "submit when stdin is not a terminal")
    r.add_argument("--job-id", default=None, help="run one specific role instead of the queue")
    r.add_argument("--answers", default=None, type=Path,
                   help="the /apply session's confirmed answers; requires --job-id. "
                        "What they teach is saved to the answer bank")
    r.add_argument("--manifest", default=None, type=Path,
                   help="write the post-fill read-back of every field to this "
                        "path as JSON; requires --job-id")

    r.add_argument("--headless", action="store_true")
    r.set_defaults(func=_cmd_run)

    sh = sub.add_parser("sheet", help="rebuild the status sheet from state, ledger and run reports")
    sh.add_argument("--add", action="store_true",
                    help="first append roles newly scored at or above score_threshold")
    sh.set_defaults(func=_cmd_sheet)

    lrn = sub.add_parser("learn", help="review or undo what the answer bank holds")
    lrn.add_argument("--job", default=None, help="only questions this job taught")
    lrn.add_argument("--undo", default=None, metavar="QUESTION_ID",
                     help="remove a question, or one wording of it with --wording")
    lrn.add_argument("--wording", default=None, help="with --undo: one wording only")
    lrn.add_argument("--keyword", default=None, help="with --undo: one keyword only")
    lrn.add_argument("--undo-claim", default=None, metavar="OPTION_TEXT",
                     help="remove one work-authorization claim")
    lrn.add_argument("--undo-fact", default=None, metavar="LABEL",
                     help="remove one judged work-authorization / export-control wording")
    lrn.add_argument("--path", default=None, type=Path, help="bank path (tests)")
    lrn.set_defaults(func=_cmd_learn)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
