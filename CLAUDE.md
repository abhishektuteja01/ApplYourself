# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal, human-gated job-search pipeline, sponsorship-aware throughout (the
scoring filter and the outreach disclosure rules both know about it). It scrapes
jobs, scores them against a profile, and generates tailored application material.
Outreach is always gated on the user. `/apply` submits only when every required
field resolves, from the bank's (`profile/application_answers.yaml`) facts and
answers through the apply-judge. Fields the facts don't settle park for the user,
whose confirmed answers are saved as facts.

## Instructions

0. Answer in simple English and few words. Lead with the result; summarize long work instead of pasting big outputs.
1. Config, personal decisions, should not be hardcoded into commands or code. Stop if that is the direction.
2. No dated references if not needed, no extra verbose paragraphs explaining why the decision was made. If absolutely needed (in case of A/B tests and similar), a concise one liner is enough.
3. Explain code changes and tests before updating or implementing. Start post confirmation.
4. Every drafted public text passes through `/no_ai_slop` (NO-SLOP in `.claude/shared/no_fab.md`).

## The two rule codes

**R7 — the determinism boundary.** No module under `src/` ever calls an LLM. `src/`
is deterministic plumbing; all judgment happens inside a slash-command session,
which writes a small YAML of choices and calls the `src/` helpers via Bash. A
corollary: `src/` is vertical-agnostic and company-agnostic.

**R10 — one writer for state.** `pipeline/events.jsonl` is the append-only
history and each `state.yaml` is a cache of its fold. `src/applyourself/track_cli.py` is the
only module that appends state events (`created`, `transition`); `/track`,
`app_cli`, `apply_cli` and `apply/triage` all go through `track_cli.main`.
Everything else appends side-list events only.

R7 and R10 are the only rule codes in this repo. Every other rule is stated inline
where it applies, by name (`NO-FAB`, `NO-DRIFT`) or in plain words.

## Where the detail lives

- `.claude/rules/*.md`: per-area detail, each scoped by `paths:` so it loads only
  when you open the code it governs (`src-boundary`, `state-machine`,
  `verticals-config`, `scoring`, `apply-submission`, `linting`,
  `profile-templates`, `pii-gate`, `discovery`).
- `.claude/hooks/*.sh` enforce the R7 boundary, the R10 state.yaml and
  events.jsonl writer, the PII gate, and that only the user writes an `/apply`
  `.confirmed` file.
- `.githooks/pre-commit` runs `ruff check`, `ruff format --check` and `mypy` when a
  `.py` file is staged, and `scripts/r7_scan.sh` plus the test suite when `src/`,
  `tests/`, `scripts/`, `data/defaults/` or `profile/*.example.*` is staged;
  `pre-merge-commit` runs it on merges too. Enable it once per clone: `git config core.hooksPath .githooks`. CI runs the same ruff,
  R7 scan and shellcheck.
- `.claude/commands/*.md` hold the real orchestration; read one before running its
  slash command. `.claude/shared/no_fab.md` defines `NO-FAB`, `NO-DRIFT`,
  `REPHRASE-LICENSE`, `SKILLS-SOURCE`, the `uv run app build` problem codes and the
  fix loop. The judges are `.claude/agents/score-judge.md`, spawned by `/score` and
  `/ingest`, and `.claude/agents/apply-judge.md`, spawned by `/apply` and the
  night's apply_prep; neither is invoked directly.

## Pipeline stages (data flow)

| # | Stage | Command | Deterministic plumbing |
|---|-------|---------|------------------------|
| 1 | Discovery | `/discover` | `src/applyourself/discovery/` — `run.py` checks due boards and searches (`sources/`) into the jobs store (`store.py`, ids in `dedupe.py`), rebuilds `clean.parquet` (`live_view.py`); `digest.py` reads the report back |
| 2 | Scoring | `/score` | `score_filter.py`, `score_store.py`, `shortlist.py`, `score_cli.py` |
| 3 | Application material | `/tailor`, `/cover-letter`, `/company-answers`, `/outreach` | `app_cli.py`, `material/` (folders, new, checks, build), `resume_model.py`, `docx_render.py`, `docx_cover_letter.py`, `lint.py` |
| 4 | Tracking | `/track`, `/standup` | `state_io.py`, `track_cli.py` |
| 5 | Submission | `/apply` | `src/applyourself/apply/` (`judge_packet.py`, `judged.py`, `plan.py`), `apply_cli.py`; the apply-judge answers non-structural fields |

`src/applyourself/shared/` holds what several stages use: paths, verticals, job identity, the
title gate, locations, the job-board registry and the HTTP client.

- `jobs/clean.parquet` is the **only** discovery output anything downstream reads:
  the live view of `jobs/jobs.parquet` (one row per posting, first and last seen),
  through the title gate and location allowlist, one row per `job_id`. Boards are
  listed in `data/universe/boards.csv`.
- Application material lands in `applications/<vertical>/<dir>/`, the only state
  the material commands share: `uv run app dir <id>` resolves it from
  `tailored_dirs[-1]`, and "has a letter / answers" means the file is there.
- Each resume is built by `src/applyourself/resume_model.py` from one source per fact
  (`resume.yaml`, `bullets.md`, `skills_master.md`, the lane's `resume:` block);
  detail in `verticals-config`.
- `saved -> tailored` is `uv run track <id> tailored --if saved`, fired by
  `/cover-letter`, `/apply` and `apply triage --fill`; it is `/ingest`'s only
  transition. `/interview` writes only
  under `interview/`: never `profile/`, never `state.yaml`.

### `job_id` is a content hash — treat it as load-bearing

`job_id = sha1(company_normalized + "|" + title_normalized)[:8]`. URL and
`jd_text` are deliberately **excluded** so the id is stable across re-scrapes.
Changing the hash inputs would silently orphan `pipeline/<job_id>/state.yaml` and
`applications/<dir>` keys. Do not add url/jd_text to the hash.
Ids are stored, never recomputed; a hash already held by another pair gets a salt
(title + `#2`, `#3`, ...). `jobs/job_ids.parquet` keeps every (id, pair) forever,
so `forget` never makes a returning pair re-mint (`uv run discover-ids` builds it).
Matching uses the stored `dedupe_key` (`identity.DEDUPE_SUFFIXES`), never the id;
widen that list, not `normalize_company`.

## Commands

Run every command from the repo root. Every CLI is a `pyproject` `[project.scripts]`
entry run with `uv run`; each has `--help`.

```bash
uv run pytest tests -q                # must be fully green after any src/ or fixture change
./scripts/nightly_pipeline.sh         # the whole night: discover, score, synonyms, tailor, apply_prep,
                                      # cleanup, summary. DRY_RUN=1 prints only; STAGE_LIMIT=N caps score/tailor/apply_prep
uv run night --resume                 # what that script runs: --resume reruns the last night's stages not ok,
                                      # --only STAGE runs one; one lock; logs/pipeline_<stamp>/run.json records each stage
./scripts/pii_scan.sh                 # PII gate: denylisted strings in tracked files
```

- `uv run app build` never writes `state:`.
- `apply run` fills, and clicks only with `--submit`. `--submit` needs `--limit` (unless
  `--job-id`) and a typed confirmation unless `--yes`; it never clicks a role in
  `apply_runs/submitted.jsonl` unless its latest line is `refused`. `--rate` floors at 30s,
  one submit per company per run, and none to a company applied to within
  `company_cooldown_days` (`--job-id` bypasses it).

## Gotchas

- Python is pinned `>=3.12,<3.13`; use `uv run` for everything (deps + venv).
- `apply` needs `uv sync --group apply`. A plain `uv sync` removes the group;
  `uv sync --all-groups` keeps it. The core `uv sync` needs no C toolchain.
- `AUDIT.md` (gitignored) is the live backlog. Read it if it exists locally; a
  fresh clone won't have it.
