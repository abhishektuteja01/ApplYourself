# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal, human-gated job-search pipeline, sponsorship-aware throughout (the
scoring pre-screen and the outreach disclosure rules both know about it). It scrapes
jobs, scores them against a profile, and generates tailored application material.
Every outward action (applying, sending outreach) is gated on the user; the code
never submits anything.

## Instructions

1. No dated references if not needed, no extra verbose paragraphs explaining why the decision was made. If absolutely needed (in case of A/B tests and similar), a concise one liner is enough.

## The one rule that shapes everything: the determinism boundary (R7)

**No module under `src/` ever calls an LLM.** `src/` is deterministic plumbing:
parquet I/O, config loading, cleaning, linting, docx rendering, state transitions.
All *judgment* (scoring a job, tailoring a resume, writing a cover letter/outreach)
happens inside a slash-command session in `.claude/commands/*.md`, which calls the
`src/` helpers via Bash for the deterministic parts. When editing, keep judging out
of `src/` and keep parquet/state mutation out of the command prose.

A corollary: `src/` is **vertical-agnostic and company-agnostic**. Never
hardcode a vertical name, search term, or company. Those come only from
`profile/*.yaml` and `data/universe/*.csv`.

**R7 and R10 are the only rule codes in this repo.** Every other rule is stated
inline where it applies, by name (`NO-FAB`, `NO-DRIFT`) or in plain words. An
`R<n>` anywhere else is local to `plans/discovery_plan.md`, whose numbering is
its own and does not match this file's.

## Pipeline stages (data flow)

1. **Discovery** (`src/discovery/`, CLI `discover`) — deterministic, LLM-free
   overnight scrape. Sources in order: manual `inbox/*.md` clips → JobSpy
   (LinkedIn + Indeed; ZipRecruiter and Google are wired but `enabled: false` —
   dead upstream) → Greenhouse/Lever/Ashby JSON boards over
   `data/universe/*.csv` + `profile/companies.yaml`. Board/inbox rows are
   title-classified into a vertical at fetch time; unclassified rows dropped.
   Always ends by running cleaning (try/finally), even after a crash/deadline.
2. **Cleaning** (`src/discovery/cleaning.py`) — normalize, drop short/stale rows,
   drop rows outside the location allowlist, dedupe (exact then rapidfuzz
   WRatio ≥ 90), assign `job_id`, tag seen-ledger. Writes `jobs/clean.parquet` (the **only** discovery
   output downstream reads) + `clean.preview.jsonl`.
3. **Scoring** (`/score`, `/rescore`; plumbing in `src/scoring_io.py`,
   `src/score_cli.py`) — LLM judges rows and writes `jobs/scored.parquet` +
   `shortlist/<date>.md`. See below.
4. **Application material** (`/tailor`, `/cover-letter`, `/outreach`) — generate
   docx/pdf into `applications/<vertical>/<dir>/`; docx rendering via
   `src/docx_render.py`; text passes `src/lint.py`.
5. **Tracking** (`/track`, `/standup`; plumbing in `src/state_io.py`,
   `src/track_cli.py`) — one `pipeline/<job_id>/state.yaml` per role moving
   through an 11-state machine.

### `job_id` is a content hash — treat it as load-bearing

`job_id = sha1(company_normalized + "|" + title_normalized)[:8]`. URL and
`jd_text` are deliberately **excluded** so the id is stable across re-scrapes.
Changing the hash inputs would silently orphan `pipeline/<job_id>/state.yaml` and
`applications/<dir>` keys. Do not add url/jd_text to the hash.

## Verticals: the config spine

A "vertical" is a job lane. `src/verticals.py`
is the single source of truth loader.

- Config lives in `profile/verticals.yaml` (gitignored user data) with a matching
  `profile/verticals/<name>/{rubric.md, tailoring.md}` dir per vertical, plus the
  resume each block's required `resume_file` points at (judges score against it,
  per `score-judge.md`). `verticals-check` fails loud if any of the three is missing.
- The loader is **strict**: every vertical block must have all current required
  keys or it raises `ValueError`. Because `tests/conftest.py` injects the config
  via an autouse fixture, a malformed block errors the *entire* test suite.
- **Two fixture mirrors must stay byte-identical to each other** for tests to
  pass: `tests/fixtures/verticals.yaml` and `tests/discovery/fixtures/verticals.yaml`.
  Any schema change must be mirrored into both in the same change.
  `TestFixtureMirrors` enforces it.
- Consumers must call `verticals.get_config()` **inside function bodies**, never at
  module level, so test injection via `set_config()` always wins.
- Templates for onboarding a new vertical: `profile/*.example.yaml` and
  `profile/verticals/example_{primary,secondary}/`. Use `/new-vertical`.

> The two `tests/**/fixtures/verticals.yaml` files are **synthetic** — three
> fictional verticals (`example_primary/secondary/tertiary`), no real search
> terms or skill weights. The real config is covered separately by
> `tests/test_real_config_drift.py`, which skips when `profile/verticals.yaml`
> is absent. Keep real strategy out of the fixtures; add real-config assertions
> to the drift test instead, structurally (never pin a real term — it is committed).

## Scoring architecture (`/score`)

`/score` runs in **two modes** dispatched on `$ARGUMENTS`:
- **Orchestrator mode** (no `--range`): runs deterministic `src.score_cli` plumbing
  (dump → split → spawn judges → check-coverage → merge → shortlist), and fans out
  parallel Sonnet **judge agents** over per-vertical row ranges. It never judges a
  row itself and never reads JD/batch content — its context stays counts-only.
- **Judge mode** (`--range A-B --vertical V`): a spawned agent judges lines A–B of
  `jobs/scored.staging/unscored_<vertical>.jsonl`, writes batch files, never merges.

A judge only picks rows from its assigned range — gaps/collisions are impossible by
construction. Deterministic pre-screens (out-of-lane titles, per-vertical
disqualifiers, `hard_ineligible` sponsorship phrases) auto-skip rows *before* any
judge sees them. `/rescore` discards `scored.parquet` and re-judges the whole
14-day window from scratch.

## State machine (`/track`)

11 states: `saved, skip, tailored, applied, recruiter_contact, screen, interview,
offer, rejected, withdrawn, ghosted`. Terminal (`offer, rejected, withdrawn,
ghosted`) reject all out-transitions. `/track` is the **sole writer of state
transitions** (R10). `/tailor`, `/cover-letter`, `/outreach` may only append to
side lists (`tailored_dirs[]`, `cover_letters[]`, `outreach[]`); `/standup` is
read-only and is the sole regenerator of `pipeline.md`.

## Linting (`src/lint.py`)

Two tiers: Tier 1 mechanical fixes (dashes, smart quotes, ellipsis, NBSP,
zero-width) auto-applied to everything; Tier 2 banned-phrase violations
(`profile/de_ai_rules.yaml`) are flagged only — the command session loops the LLM
to rewrite until the linter returns empty. Verbatim canonical `bullets.md` text is
exempt from Tier 2 only when `bullets_diction_pass_completed: true`; outreach is
never exempt.

## Commands

```bash
# Tests — must be fully green. Run this after any src/ or fixture change.
uv run pytest tests -q
uv run pytest tests/test_verticals.py -q          # single test file
uv run pytest tests/test_verticals.py::<name>     # single test

# Deterministic CLIs (entry points in pyproject [project.scripts]).
uv run discover [--resume <run_id>]   # overnight scrape -> jobs/clean.parquet
uv run verticals-check                # validate config + rubric/tailoring dirs
uv run ingest-url <url>               # pull one JD into inbox/
uv run score <subcommand>             # score_cli plumbing (dump/split/merge/...)
uv run track <job_id> <state> [--note ...]   # state transition
uv run tailor-prep <job_id>           # /tailor front-matter: prereqs, row load, out dir
```

The user-facing workflow is the slash commands (`/score`, `/tailor`,
`/cover-letter`, `/outreach`, `/track`, `/standup`, `/new-vertical`,
`/suggest-synonyms`, `/rescore`, `/no_ai_slop`), defined in
`.claude/commands/*.md`. `score-judge.md` also lives there but is spawned by
`/score`, never invoked directly. There is no
`extract` module in `src/`, and no LLM *judging* in `src/` — but the deterministic
plumbing each command leans on does live there (e.g. `src/tailor_cli.py` for
`/tailor`'s prereqs/row-load/output-dir and jd_snapshot; the tailoring itself
stays in the command session).

## Gotchas

- Python is pinned `>=3.12,<3.13`; use `uv run` for everything (deps + venv).
- Read the relevant command `.md` before running its slash command — the real
  orchestration logic lives there, not in `src/`.
- `HANDOFF.md` (live breakage/fixes), `cleaning.md` (cleanup backlog) and
  `publish.md` (publish backlog) are gitignored working notes. Read them if
  they exist locally; on a fresh clone they won't.
- `plans/*.md` are reference, not instructions: `discovery_plan.md` is the
  completed v2 discovery build, `discovery_next.md` its downstream handoff, and
  `ats_application_forms_research.md` un-built research. Nothing there is a live
  checklist to resume.
