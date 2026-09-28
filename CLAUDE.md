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

**R10 — one writer for state.** `/track` is the sole writer of state transitions.
Every other command appends to side lists only.

R7 and R10 are the only rule codes in this repo. Every other rule is stated inline
where it applies, by name (`NO-FAB`, `NO-DRIFT`) or in plain words.

## Where the detail lives

`.claude/rules/*.md` hold the per-area detail, each scoped by `paths:` so it loads
only when you open the code it governs — `src-boundary`, `state-machine`,
`verticals-config`, `scoring`, `apply-submission`, `linting`, `profile-templates`,
`pii-gate`, `discovery`. Run `/context` to see what actually loaded.

`.claude/hooks/*.sh` enforce three of these mechanically rather than by memory: the
R7 boundary, the R10 state.yaml writer, and the PII gate.
`.githooks/pre-commit` runs the test suite on any commit that stages `src/` or
`tests/fixtures/`, and `ruff check --select F401,F841` on any commit that stages
a `.py` file (`git config core.hooksPath .githooks`, once per clone).

## Pipeline stages (data flow)

| # | Stage | Command | Deterministic plumbing |
|---|-------|---------|------------------------|
| 1 | Discovery | `/discover` | `src/discovery/` — `run.py` checks due boards and searches into the jobs store (`store.py`), rebuilds `clean.parquet`; `digest.py` reads the report back |
| 2 | Scoring | `/score` | `score_filter.py`, `score_store.py`, `shortlist.py`, `score_cli.py` |
| 3 | Application material | `/tailor`, `/cover-letter`, `/company-answers`, `/outreach` | `app_cli.py`, `resume_model.py`, `docx_render.py`, `docx_cover_letter.py`, `lint.py` |
| 4 | Tracking | `/track`, `/standup` | `state_io.py`, `track_cli.py` |
| 5 | Submission | `/apply` | `src/apply/`, `apply_cli.py` |

`jobs/clean.parquet` is the **only** discovery output anything downstream reads.
`jobs/jobs.parquet` holds one row per posting with its `job_id` and the dates it
was first and last seen; a night only adds to it. `clean.parquet` is the live
view built from it: seen recently, through the title gate and location
allowlist, one row per `job_id`, within retention. Boards are listed in
`data/universe/boards.csv` + `boards.local.csv`; `jobs/boards.parquet` records
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

## Commands

```bash
# Tests — must be fully green. Run this after any src/ or fixture change.
uv run pytest tests -q
uv run pytest tests/test_verticals.py -q          # single test file
uv run pytest tests/test_verticals.py::<name>     # single test

# Deterministic CLIs (entry points in pyproject [project.scripts]).
uv run discover [--deadline-hours H] [--source NAME] [--max-terms N] [--jobs-dir D] [--boards-dir D]
                                      # overnight scrape -> jobs store -> jobs/clean.parquet
                                      # the overrides narrow one run without touching
                                      # config; --source is exhaustive and repeatable
                                      # (naming one excludes the inbox); --jobs-dir and
                                      # --boards-dir point a shadow run at a copy.
                                      # A killed run just leaves boards due.
                                      # needs `uv sync --group discovery` (libpostal)
uv run verticals-check                # validate config + rubric/tailoring dirs
uv run discovery-check                # resolve + validate the discovery config:
                                      # prints the effective location allowlist,
                                      # the jobspy search locations and the
                                      # enabled sources, exits 1 on any problem.
                                      # `discover` runs it as preflight
uv run discover-digest [--json] [--run-id ID] [--window N] [--runs-dir D]
                                      # read the recent jobs/runs/*.json reports and
                                      # print the trend facts /discover renders:
                                      # per-lane rows vs that lane's own 7-day
                                      # median, error delta, zero streaks, 403/429s,
                                      # crashed / truncated lanes, dead boards.
                                      # Reader only, writes nothing
uv run onboard-scaffold --vertical V --work-auth citizen|needs_now|time_limited
                                      # [--with-apply] [--with-optional] [--force] [--dry-run]
                                      # /onboarding's setup chores: copy every
                                      # profile/*.example.* to its real name, strip the
                                      # example lanes + set default_vertical, set the home
                                      # country's work-auth row, probe libpostal.
                                      # Skips existing files unless --force
uv run ingest-url <url> [--vertical V] [--company C] [--title T] [--dry-run]
                                      # one JD -> jobs store + clean rebuild
                                      # --vertical: lane (/ingest always passes it)
                                      # --company/--title: override a bad parse
                                      # --dry-run: print text only
uv run score start [--all] [--vertical V] [--job-id ID]
                                      # drop by hard rules, stage judge slices
uv run score finish                   # check answers, merge, prune, write shortlist
uv run track <job_id> <state> [--note ...]   # state transition
uv run app new <job_id>               # /tailor: checks, registers the role, opens its folder + jd_snapshot.md
uv run app dir <job_id>               # JSON: the role's folder, name, has_resume/letter/answers
uv run app build resume|letter|answers <job_id> [--final]
                                      # check the session's YAML (resume_plan / cover_letter /
                                      # company_answers), render docx+pdf, write trace.md.
                                      # Exit 2 lists problems; --final (resume) falls back to
                                      # canonical text. Never writes `state:`
uv run profile-extract <file>         # dump a .docx/.md resume's text (/onboarding ingest)
# /apply plumbing (needs `uv sync --group apply`).
uv run apply prepare <job_id>         # validate prereqs, resolve out dir/vertical/answers
uv run apply plan <job_id> [--json] [--questions] [--url U] [--out-dir D] [--answers F]
                                      # print the fill plan, no browser
                                      # (Ashby opens a headless one for DOM-only field text)
                                      # --questions: KNOWN / GUESSED BY KEYWORD /
                                      # NEW / BLOCKING. Exit 1 if an --answers
                                      # entry cannot apply
uv run apply run [--limit N] [--rate 4m] [--jitter 60s] [--job-id ID] [--answers F] [--manifest F] [--headless] [--submit] [--yes]
                                      # fill, and click only with --submit
                                      # --answers: the session's confirmed answers;
                                      #   what they teach is saved to the bank first
                                      # --manifest: planned vs what the page holds
                                      # --submit needs --limit (unless --job-id),
                                      # a typed confirmation unless --yes; never
                                      # clicks a role in apply_runs/submitted.jsonl
                                      # unless its latest line is `refused`
                                      # --rate floors at 30s, one submit per company per run
uv run apply learn [--job J] [--undo ID [--wording W | --keyword K]] [--undo-claim TEXT] [--undo-fact LABEL]
                                      # review the answer bank, or take one thing back
uv run apply sheet [--add]            # rebuild the overnight status sheet; --add first
                                      # appends roles scored >= score_threshold
uv run python scripts/audit_slugs.py [--ats NAME] [--stale-days N] [--pacing S] [--limit N] [--resume]
                                      # one-off liveness audit of the ATS slug
                                      # universe -> jobs/slug_audit_<date>.csv:
                                      # active / stale / empty / dead / error
                                      # per slug. Report only; writes nothing
                                      # else. `--ats` is repeatable, default
                                      # ashby/greenhouse/lever (workday opt-in)
./scripts/pii_scan.sh                 # PII gate: denylisted strings in tracked files
uv run python scripts/scrub_example_templates.py  # strip Word metadata from the two .example.docx
```

The user-facing workflow is the slash commands (`/onboarding`, `/discover`, `/score`,
`/tailor`, `/cover-letter`, `/company-answers`, `/apply`, `/outreach`,
`/track`, `/standup`,
`/new-vertical`, `/tune-vertical`, `/suggest-synonyms`, `/no_ai_slop`,
`/ingest`, `/interview`), defined in
`.claude/commands/*.md`. The judge is a subagent, not a command:
`.claude/agents/score-judge.md`, spawned by `/score` and `/ingest`,
never invoked directly. `/discover` is the scrape plus a digest: `discovery-check`
preflight, `uv run discover` with the user's overrides, then `discover-digest`
(`src/discovery/digest.py`) reads `jobs/runs/*.json` and the session renders
≤10 lines from the numbers it computed. `/new-vertical <name>` writes the loader's minimum for a
new lane in one confirm-or-edit, and splits into `pass-a` (the lane block and
classifier rule, enough to start scraping) and `pass-b` (rubric, tailoring and resume shape)
when `/onboarding` passes a mode token; `/tune-vertical <name>` is the deep pass over a
lane that already exists, run against real scored rows. `/company-answers <job_id>` drafts that
role's `company_answers.yaml` into its built folder (once; `--refresh` redoes it);
`/apply` and `/cover-letter` both call it. `/ingest <url> <vertical> [resume]
[cover-letter]` is the single-URL fast path: it chains `ingest-url` → a
one-row `score start --job-id` + one judge + `score finish` → `/tailor` →
`/cover-letter`, spawning the existing commands rather than reimplementing
them; the only transition is the cover letter's `--if saved` promotion.
`/interview <job_id | vertical>` is the prep half: it drills the user on the claims
their own resume makes, depth-first, and persists `interview/gap_report.md` — a
worklist of claims that did not hold, which every later session reads first. In role
mode its targets come from that role's code-written `trace.md` — what landed, which
words were rephrased under license, and which JD keywords did not land.
It writes only under `interview/` (gitignored): never `profile/`, never `state.yaml`.
`.claude/shared/no_fab.md` is the one include the writing commands read: it defines
`NO-FAB`, `NO-DRIFT`, `REPHRASE-LICENSE`, `SKILLS-SOURCE`, the problem codes
`uv run app build` reports, and the fix loop. `saved -> tailored` is
`uv run track <id> tailored --if saved`, fired by `/cover-letter` and `/apply`.
No LLM *judging* in `src/`: the session writes a small YAML of choices, and
`src/app_cli.py` does everything mechanical around it.

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
