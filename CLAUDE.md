# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal, human-gated job-search pipeline, sponsorship-aware throughout (the
scoring pre-screen and the outreach disclosure rules both know about it). It scrapes
jobs, scores them against a profile, and generates tailored application material.
Outreach is always gated on the user. Application submission is automatable via
`/apply`, which submits only when every required field resolves from config;
anything unresolved parks the role for review.

## Instructions

1. No dated references if not needed, no extra verbose paragraphs explaining why the decision was made. If absolutely needed (in case of A/B tests and similar), a concise one liner is enough.
2. Explain code changes and tests before updating or implementing. Start post confirmation.

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
inline where it applies, by name (`NO-FAB`, `NO-DRIFT`) or in plain words.

## Pipeline stages (data flow)

1. **Discovery** (`src/discovery/`, CLI `discover`) — deterministic, LLM-free
   overnight scrape. Sources in order: manual `inbox/*.md` clips → JobSpy
   (LinkedIn + Indeed) → Greenhouse/Lever/Ashby JSON boards over
   `data/universe/*.csv` + `profile/companies.yaml`. Board/inbox rows are
   title-classified into a vertical at fetch time; unclassified rows dropped.
   Always ends by running cleaning (try/finally), even after a crash/deadline.
2. **Cleaning** (`src/discovery/cleaning.py`) — normalize, drop short/stale rows,
   drop rows outside the location allowlist, dedupe (exact then rapidfuzz
   WRatio ≥ 90), assign `job_id`, tag seen-ledger. Writes `jobs/clean.parquet` (the **only** discovery
   output downstream reads) + `clean.preview.jsonl`.
3. **Scoring** (`/score`, `/rescore`; plumbing in `src/prescreen.py` for the
   deterministic pre-screens, `src/scoring_io.py` for parquet read/dump/merge/
   prune, `src/shortlist.py` for compute+render, and `src/score_cli.py`) — LLM judges rows and writes `jobs/scored.parquet` +
   `shortlist/<date>.md`. See below.
4. **Application material** (`/tailor`, `/cover-letter`, `/outreach`) — generate
   docx/pdf into `applications/<vertical>/<dir>/`; docx rendering via
   `src/docx_render.py` (resume) and `src/docx_cover_letter.py`
   (placeholder fill); text passes `src/lint.py`.
5. **Tracking** (`/track`, `/standup`; plumbing in `src/state_io.py`,
   `src/track_cli.py`) — one `pipeline/<job_id>/state.yaml` per role moving
   through an 11-state machine.
6. **Submission** (`/apply`; plumbing in `src/apply/` — `schema.py`,
   `domscan.py`, `reconcile.py`, `answers.py`, `plan.py`, `greenhouse.py`,
   `fill.py` — and `src/apply_cli.py`) — consumes roles sitting at `tailored`
   with both a resume and a cover letter on file, fills the board's
   application form from `profile/application_answers.yaml`, and either
   submits (transitioning to `applied` through `/track`) or parks the role
   on whatever it could not resolve. Greenhouse only for now (see
   `submit_plan.md` for the Ashby/Lever/Workday phases).

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
  `profile/verticals/example_*/` (three: primary, secondary, tertiary — the
  fixtures' `default_vertical` is tertiary). Use `/new-vertical`.

> The two `tests/**/fixtures/verticals.yaml` files are **synthetic** — three
> fictional verticals (`example_primary/secondary/tertiary`), no real search
> terms or skill weights. The real config is covered separately by
> `tests/test_real_config_drift.py`, which skips when `profile/verticals.yaml`
> is absent. Keep real strategy out of the fixtures; add real-config assertions
> to the drift test instead, structurally (never pin a real term — it is committed).

## Scoring architecture (`/score`)

`/score` takes no arguments. It runs the deterministic `src.score_cli`
subcommands in order — `prepare` → judges → `check-coverage` → `merge` →
`render` — and fans out parallel Sonnet judge agents over per-vertical row
ranges. It never judges a row itself, and its context stays counts-only — the one
exception is the recovery path, where it reads and repairs the specific batch rows
a corrupt-JSON or merge-validation error names.

Judging is a **separate command file**, `score-judge.md`, spawned per range
(`--range A-B --vertical V`). A judge reads lines A–B of
`jobs/scored.staging/unscored_<vertical>.jsonl`, writes batch files, and never
merges.

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
uv run ingest-url <url> [--dry-run]   # one JD -> jobs/raw + clean rebuild (--dry-run: print text only)
uv run score <subcommand>             # score_cli plumbing (dump/split/merge/...)
uv run track <job_id> <state> [--note ...]   # state transition
uv run tailor-prep <job_id>           # /tailor front-matter: prereqs, row load, out dir
uv run profile-extract <file>         # dump a .docx/.md resume's text (/onboarding ingest)
uv run apply plan <job_id>            # /apply: print the fill plan, no browser
uv run apply run [--submit]           # /apply: walk the eligible queue (needs `uv sync --group apply`)
./scripts/pii_scan.sh                 # PII gate: denylisted strings in tracked files
uv run python scripts/scrub_example_templates.py  # strip Word metadata from the two .example.docx
```

The user-facing workflow is the slash commands (`/onboarding`, `/score`,
`/tailor`, `/cover-letter`, `/apply`, `/outreach`, `/track`, `/standup`,
`/new-vertical`, `/suggest-synonyms`, `/rescore`, `/no_ai_slop`, `/ingest`), defined in
`.claude/commands/*.md`. `score-judge.md` also lives there but is spawned by
`/score`, never invoked directly. `/ingest <url> <vertical> [resume]
[cover-letter]` is the single-URL fast path: it chains `ingest-url` → a
one-row `score dump --job-id --no-prescreen` + one judge → `/tailor` →
`/cover-letter`, spawning the existing commands rather than reimplementing
them, and stops at `saved` because `/track` alone writes transitions. `.claude/shared/` holds the includes several
commands read: `no_fab.md` (defines `NO-FAB`, `NO-DRIFT`, `REPHRASE-LICENSE`,
`SKILLS-SOURCE`), `lint_loop.md` (the rewrite-loop attempt cap), and
`render_pdf.md` (the docx->pdf block). There is no
`extract` module in `src/`, and no LLM *judging* in `src/` — but the deterministic
plumbing each command leans on does live there (e.g. `src/tailor_cli.py` for
`/tailor`'s prereqs/row-load/output-dir and jd_snapshot; the tailoring itself
stays in the command session).

## New-user templates (`profile/*.example.*`)

Every `profile/` input a command or module reads must be either committed
outright or shipped as a `.example` template beside it.
`tests/test_profile_templates.py` derives that list by scanning
`.claude/**/*.md`, `src/**/*.py` and `scripts/*.sh` for `profile/` references, so
wiring in a new profile file fails the suite until it has a template. Excluded:
committed defaults (`de_ai_rules.yaml`, `sponsorship_rules.yaml`), the
`example_*` lane dirs, and dotfiles (runtime state a command writes, e.g.
`profile/.onboarding.md`).

Template content lives in the fictional widget/gizmo/sprocket/cog world of the
`example_*` lanes, and the ids must resolve: the `SKILL-*` ids those lanes name
in their Skills layouts must exist in `skills_master.example.md`, and every
`evidence:` reference must point at a bullet in `bullets.example.md`. Tests
enforce both directions.

`profile/*.example.docx` are the only tracked binaries, allowlisted **by name**
in `pii_scan.sh` (never by glob) with `tests/test_example_templates.py` standing
in for the text scan the gate skips. They are hand-authored in Word; every
save stamps the editor's name into the document metadata, so run
`scripts/scrub_example_templates.py` afterwards. `--check` exits 1 if either
file still needs it.

`/onboarding` is the interview that fills all of it in.

## The PII gate

The repo is public (`github.com/abhishektuteja01/ApplYourself`).
`scripts/pii_scan.sh` fails on any denylisted string in a tracked file, reading
patterns from gitignored `profile/pii_denylist.txt`. `.githooks/pre-push` runs it
(`git config core.hooksPath .githooks`, once per clone). It reads the git
**index**, so it only sees tracked files: run it *after* staging, and never put a
real pattern in a committed file. `LICENSE` and `data/universe/*.csv` are
allowlisted in-script.

The hook also scans what `pii_scan.sh` structurally cannot: the pushed commits'
**messages, author and committer fields**, plus `user.email` itself. That
metadata lives in no file, so the index scan is blind to it — and GitHub renders
an author email on every commit page.

## Gotchas

- Python is pinned `>=3.12,<3.13`; use `uv run` for everything (deps + venv).
- Read the relevant command `.md` before running its slash command — the real
  orchestration logic lives there, not in `src/`.
- `HANDOFF.md` (live breakage/fixes) and `publish.md` (live backlog) are
  gitignored working notes. Read them if they exist locally; on a fresh clone
  they won't.
