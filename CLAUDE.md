# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal, human-gated job-search pipeline, sponsorship-aware throughout (the
scoring pre-screen and the outreach disclosure rules both know about it). It scrapes
jobs, scores them against a profile, and generates tailored application material.
Outreach is always gated on the user. Application submission is automatable via
`/apply`, which submits only when every required field resolves — from config, or
from a per-run Tier C answers file the command session drafts under NO-FAB (generic
answers from `bullets.md`, company-specific ones from that role's
`company_answers.md`). Anything still unresolved parks the role for review.

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

**R10 — one writer for state.** `/track` is the sole writer of state transitions.
Every other command appends to side lists only.

R7 and R10 are the only rule codes in this repo. Every other rule is stated inline
where it applies, by name (`NO-FAB`, `NO-DRIFT`) or in plain words.

## Where the detail lives

`.claude/rules/*.md` hold the per-area detail, each scoped by `paths:` so it loads
only when you open the code it governs — `src-boundary`, `state-machine`,
`verticals-config`, `scoring`, `apply-submission`, `linting`, `profile-templates`,
`pii-gate`. Run `/context` to see what actually loaded.

`.claude/hooks/*.sh` enforce four of these mechanically rather than by memory: the
R7 boundary, the R10 state.yaml writer, the two fixture mirrors, and the PII gate.
The test suite gates every turn that touched `src/` or a fixture.

## Pipeline stages (data flow)

| # | Stage | Command | Deterministic plumbing |
|---|-------|---------|------------------------|
| 1 | Discovery | `discover` | `src/discovery/` — inbox clips → JobSpy → ATS boards + Workday |
| 2 | Cleaning | (always runs after discovery) | `src/discovery/cleaning.py` |
| 3 | Scoring | `/score`, `/rescore` | `prescreen.py`, `scoring_io.py`, `shortlist.py`, `score_cli.py` |
| 4 | Application material | `/tailor`, `/cover-letter`, `/outreach` | `docx_render.py`, `docx_cover_letter.py`, `lint.py` |
| 5 | Tracking | `/track`, `/standup` | `state_io.py`, `track_cli.py` |
| 6 | Submission | `/apply` | `src/apply/`, `apply_cli.py` |

`jobs/clean.parquet` is the **only** discovery output anything downstream reads.
Cleaning normalizes, drops short/stale rows and rows outside the location
allowlist, dedupes (exact then rapidfuzz WRatio ≥ 90), assigns `job_id`, and tags
the seen-ledger, writing `jobs/clean.parquet` + `clean.preview.jsonl`.
Application material lands in `applications/<vertical>/<dir>/`;
tracking keeps one `pipeline/<job_id>/state.yaml` per role.

### `job_id` is a content hash — treat it as load-bearing

`job_id = sha1(company_normalized + "|" + title_normalized)[:8]`. URL and
`jd_text` are deliberately **excluded** so the id is stable across re-scrapes.
Changing the hash inputs would silently orphan `pipeline/<job_id>/state.yaml` and
`applications/<dir>` keys. Do not add url/jd_text to the hash.

## Commands

```bash
# Tests — must be fully green. Run this after any src/ or fixture change.
uv run pytest tests -q
uv run pytest tests/test_verticals.py -q          # single test file
uv run pytest tests/test_verticals.py::<name>     # single test

# Deterministic CLIs (entry points in pyproject [project.scripts]).
uv run discover [--resume <run_id>] [--deadline-hours H] [--source NAME] [--max-terms N]
                                      # overnight scrape -> jobs/clean.parquet
                                      # the three overrides narrow one run without
                                      # touching config; --source is exhaustive and
                                      # repeatable (naming one excludes the inbox)
                                      # needs `uv sync --group discovery` (libpostal)
uv run verticals-check                # validate config + rubric/tailoring dirs
uv run onboard-scaffold --vertical V --work-auth citizen|needs_now|time_limited
                                      # [--with-apply] [--with-optional] [--force] [--dry-run]
                                      # /onboarding's setup chores: copy every
                                      # profile/*.example.* to its real name, strip the
                                      # example lanes + set default_vertical, reconcile
                                      # sponsorship_rules.yaml, probe libpostal.
                                      # Skips existing files unless --force
uv run ingest-url <url> [--vertical V] [--company C] [--title T] [--dry-run]
                                      # one JD -> jobs/raw + clean rebuild
                                      # --vertical: lane (/ingest always passes it)
                                      # --company/--title: override a bad parse
                                      # --dry-run: print text only
uv run score <subcommand>             # score_cli plumbing (dump/split/merge/...)
uv run track <job_id> <state> [--note ...]   # state transition
uv run tailor-prep <job_id>           # /tailor front-matter: prereqs, row load, out dir
uv run profile-extract <file>         # dump a .docx/.md resume's text (/onboarding ingest)
# /apply plumbing (needs `uv sync --group apply`).
uv run apply prepare <job_id>         # validate prereqs, resolve out dir/vertical/answers
uv run apply plan <job_id> [--json] [--url U] [--out-dir D] [--answers F]
                                      # print the fill plan, no browser
                                      # (Ashby opens a headless one for DOM-only field text)
uv run apply fill <job_id> [--url U] [--out-dir D] [--answers F] [--force] [--headless] [--no-pause]
                                      # fill one real form and stop; never submits
uv run apply run [--limit N] [--rate 4m] [--jitter 60s] [--job-id ID] [--answers F] [--headless] [--submit] [--yes]
                                      # walk the eligible queue; --submit needs --limit
                                      #   (unless --job-id names one role),
                                      # --submit prompts for a typed confirmation unless --yes,
                                      # --rate floors at 30s, one submit per company per run
./scripts/pii_scan.sh                 # PII gate: denylisted strings in tracked files
uv run python scripts/scrub_example_templates.py  # strip Word metadata from the two .example.docx
```

The user-facing workflow is the slash commands (`/onboarding`, `/score`,
`/tailor`, `/cover-letter`, `/company-answers`, `/apply`, `/outreach`,
`/track`, `/standup`,
`/new-vertical`, `/tune-vertical`, `/suggest-synonyms`, `/rescore`, `/no_ai_slop`,
`/ingest`), defined in
`.claude/commands/*.md`. The judge is a subagent, not a command:
`.claude/agents/score-judge.md`, spawned by `/score`, `/rescore` and `/ingest`,
never invoked directly. `/new-vertical <name>` writes the loader's minimum for a
new lane in one confirm-or-edit; `/tune-vertical <name>` is the deep pass over a
lane that already exists, run against real scored rows. `/company-answers <job_id>` drafts that
role's `company_answers.md` into its `/tailor` output dir; `/apply` calls it
directly for roles that need no full cover letter. `/ingest <url> <vertical> [resume]
[cover-letter]` is the single-URL fast path: it chains `ingest-url` → a
one-row `score dump --job-id --no-prescreen` + one judge → `/tailor` →
`/cover-letter`, spawning the existing commands rather than reimplementing
them, and stops at `saved` because `/track` alone writes transitions. `.claude/shared/` holds the includes several
commands read: `no_fab.md` (defines `NO-FAB`, `NO-DRIFT`, `REPHRASE-LICENSE`,
`SKILLS-SOURCE`), `lint_loop.md` (the rewrite-loop attempt cap),
`render_pdf.md` (the docx->pdf block), and `self_promote.md` (the guarded
`saved -> tailored` transition `/apply` and `/cover-letter` both fire). There is no
`extract` module in `src/`, and no LLM *judging* in `src/` — but the deterministic
plumbing each command leans on does live there (e.g. `src/tailor_cli.py` for
`/tailor`'s prereqs/row-load/output-dir and jd_snapshot; the tailoring itself
stays in the command session).

## Gotchas

- Python is pinned `>=3.12,<3.13`; use `uv run` for everything (deps + venv).
- The core `uv sync` needs no C toolchain. `postal` (libpostal) is the opt-in
  `discovery` group, imported lazily in `src/discovery/location.py`: importing
  discovery works without it, address parsing raises a message telling you to
  install it. `tests/discovery` needs it; the rest of the suite does not.
- Read the relevant command `.md` before running its slash command — the real
  orchestration logic lives there, not in `src/`.
- `HANDOFF.md` (live breakage/fixes) and `publish.md` (live backlog) are
  gitignored working notes. Read them if they exist locally; on a fresh clone
  they won't.
