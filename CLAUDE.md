# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal, human-gated job-search pipeline, sponsorship-aware throughout (the
scoring filter and the outreach disclosure rules both know about it). It scrapes
jobs, scores them against a profile, and generates tailored application material.
Outreach is always gated on the user. Application submission is automatable via
`/apply`, which submits only when every required field resolves — from the answer
bank (`profile/application_answers.yaml`), or from answers the command session
drafts under NO-FAB and the user confirms. Every confirmed answer is saved to the
bank, so each form parks less than the last. Anything still unresolved parks the
role for review.

## Instructions

0. Always answer in simple english, too coded or big outputs will drain, summarize your thinking and then provide each output.
1. Config, personal decisions, should not be hardcoded into commands or code. Stop if that is the direction. 
2. No dated references if not needed, no extra verbose paragraphs explaining why the decision was made. If absolutely needed (in case of A/B tests and similar), a concise one liner is enough.
3. Explain code changes and tests before updating or implementing. Start post confirmation.

## The two rule codes

**R7 — the determinism boundary.** No module under `src/` ever calls an LLM. `src/`
is deterministic plumbing; all judgment happens inside a slash-command session,
which calls the `src/` helpers via Bash. A corollary: `src/` is vertical-agnostic
and company-agnostic.

**R10 — one writer for state.** `src/track_cli.py` is the only module that changes
`state:`; `/track`, `app_cli`, `apply_cli` and `apply/triage` all go through
`track_cli.main`. Everything else appends to side lists only.

R7 and R10 are the only rule codes in this repo. Every other rule is stated inline
where it applies, by name (`NO-FAB`, `NO-DRIFT`) or in plain words.

## Where the detail lives

`.claude/rules/*.md` hold the per-area detail, each scoped by `paths:` so it loads
only when you open the code it governs — `src-boundary`, `state-machine`,
`verticals-config`, `scoring`, `apply-submission`, `linting`, `profile-templates`,
`pii-gate`, `discovery`. Run `/context` to see what actually loaded.

`.claude/hooks/*.sh` enforce four of these mechanically rather than by memory: the
R7 boundary, the R10 state.yaml writer, the PII gate, and that only the user writes
an `/apply` `.confirmed` file.
`.githooks/pre-commit` runs `scripts/r7_scan.sh` and the test suite on any commit
that stages `src/`, `tests/`, `scripts/` or `profile/*.example.*`, and `ruff check`
on any commit that stages a `.py` file (`git config core.hooksPath .githooks`,
once per clone). CI runs the same ruff, R7 scan and shellcheck.

## Pipeline stages (data flow)

| # | Stage | Command | Deterministic plumbing |
|---|-------|---------|------------------------|
| 1 | Discovery | `/discover` | `src/discovery/` — `run.py` checks due boards and searches (`sources/`) into the jobs store (`store.py`, ids in `dedupe.py`), rebuilds `clean.parquet` (`live_view.py`); `digest.py` reads the report back |
| 2 | Scoring | `/score` | `score_filter.py`, `score_store.py`, `shortlist.py`, `score_cli.py` |
| 3 | Application material | `/tailor`, `/cover-letter`, `/company-answers`, `/outreach` | `app_cli.py`, `resume_model.py`, `docx_render.py`, `docx_cover_letter.py`, `lint.py` |
| 4 | Tracking | `/track`, `/standup` | `state_io.py`, `track_cli.py` |
| 5 | Submission | `/apply` | `src/apply/`, `apply_cli.py` |

`src/shared/` holds what several stages use: paths, verticals, job identity, the
title gate, locations, the job-board registry and the HTTP client.

`jobs/clean.parquet` is the **only** discovery output anything downstream reads.
`jobs/jobs.parquet` holds one row per posting with its `job_id` and the dates it
was first and last seen; a night only adds to it. `clean.parquet` is the live
view built from it: seen recently, through the title gate and location
allowlist, one row per `job_id`, within retention. Boards are listed in
`data/universe/boards.csv`; `jobs/boards.parquet` records
when each was last checked.
Application material lands in `applications/<vertical>/<dir>/`, and that folder
is the only state the material commands share: `uv run app dir <id>` resolves it
from `state.yaml`'s `tailored_dirs[-1]`, and "has a letter / answers" means the
file is there. Tracking keeps one `pipeline/<job_id>/state.yaml` per role.

### One source per resume fact

`profile/resume.yaml` (name, contact, education, each job/project header with
its per-lane `{min, max, order}`), `profile/bullets.md` (claims, `default_for`),
`profile/skills_master.md` (skills, `show`) and each lane's `resume:` block in
`verticals.yaml` (section order, bullet budget, skill lines, baseline summary).
Things say where they appear, so deleting one is one edit; `verticals-check`
names any pointer that breaks. `src/resume_model.py` builds every resume from
them — `/score`'s baseline and `/tailor`'s plan alike.

### `job_id` is a content hash — treat it as load-bearing

`job_id = sha1(company_normalized + "|" + title_normalized)[:8]`. URL and
`jd_text` are deliberately **excluded** so the id is stable across re-scrapes.
Changing the hash inputs would silently orphan `pipeline/<job_id>/state.yaml` and
`applications/<dir>` keys. Do not add url/jd_text to the hash.
Ids are stored, never recomputed; a hash already held by another pair gets a salt
(title + `#2`, `#3`, ...).

## Commands

Every CLI is a `pyproject` `[project.scripts]` entry run with `uv run`; each has `--help`.

```bash
uv run pytest tests -q                # must be fully green after any src/ or fixture change
./scripts/nightly_pipeline.sh         # the whole night: discover, score, synonyms, tailor, apply_prep,
                                      # summary. DRY_RUN=1 prints only; STAGE_LIMIT=N caps tailor/apply_prep
./scripts/pii_scan.sh                 # PII gate: denylisted strings in tracked files
```

- `discover` needs `uv sync --group discovery` (libpostal); `apply` needs `uv sync --group apply`.
- `uv run app build` never writes `state:`.
- `apply run` fills, and clicks only with `--submit`. `--submit` needs `--limit` (unless
  `--job-id`) and a typed confirmation unless `--yes`; it never clicks a role in
  `apply_runs/submitted.jsonl` unless its latest line is `refused`. `--rate` floors at 30s,
  one submit per company per run.

The user-facing workflow is the slash commands in `.claude/commands/*.md`. The judge is a
subagent, not a command: `.claude/agents/score-judge.md`, spawned by `/score` and `/ingest`,
never invoked directly. `/ingest`'s only transition is the cover letter's `--if saved`
promotion. `/interview` writes only under `interview/` (gitignored): never `profile/`,
never `state.yaml`.
`.claude/shared/no_fab.md` is the one include the writing commands read: it defines
`NO-FAB`, `NO-DRIFT`, `REPHRASE-LICENSE`, `SKILLS-SOURCE`, the problem codes
`uv run app build` reports, and the fix loop. `saved -> tailored` is
`uv run track <id> tailored --if saved`, fired by `/cover-letter`, `/apply` and
`apply triage --fill`.
No LLM *judging* in `src/`: the session writes a small YAML of choices, and
`src/app_cli.py` does everything mechanical around it.

## Gotchas

- Python is pinned `>=3.12,<3.13`; use `uv run` for everything (deps + venv).
- The core `uv sync` needs no C toolchain. `postal` (libpostal) is the opt-in
  `discovery` group, imported lazily in `src/shared/location/parser.py`: importing
  discovery works without it, address parsing raises a message telling you to
  install it. `tests/discovery` and `tests/shared/test_location.py` need it; the
  rest of the suite does not.
- Read the relevant command `.md` before running its slash command — the real
  orchestration logic lives there, not in `src/`.
- `AUDIT.md` (gitignored) is the live backlog. Read it if it exists locally; a
  fresh clone won't have it.
