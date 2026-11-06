# ApplYourself

A job-search pipeline you run yourself, on your own machine, over your own
resume. It scrapes postings, scores them against a profile you write, drafts
tailored resumes and cover letters, and can fill and submit an application form
for you, with you in the loop at every point that matters.

<p align="center">
  <img src="docs/media/main.gif" alt="ApplYourself in action" width="100%">
</p>

**It's for you if** you are applying to a lot of roles across a few different
kinds of job, you want the sifting automated but the writing to still sound like
you, and you are comfortable running things from a terminal.

**It's not for you if** you want a hosted service, a web UI, or something that
applies to hundreds of jobs while you sleep. This is a repo you clone, a profile
you fill in by hand, and a set of commands you run when you decide to.

It needs [Claude Code](https://claude.com/claude-code), because the slash commands
are where the scoring, the writing and every judgment call happen. `src/` is
plain Python that never calls a model. It is sponsorship-aware throughout: the
scoring filter and the outreach drafts both know when work authorization matters.

## What it writes, and what it sends

Everything lands on your disk. Outreach messages and cover letters are
**written to files and nothing more**. Nothing here emails, DMs or posts.

Submission is the one exception. `/apply` drives a real Chrome and can fill and
submit an employer's form, on **Greenhouse, Lever and Ashby only**. It fills and
stops by default; only `--submit` presses the button, after you confirm every
answer. **Filling is not a dry run:** it uploads your resume and cover letter to
the employer's server before any submit decision.

## How it works

Five stages. Each writes files the next one reads, so you can stop anywhere,
look, and pick up later.

A **lane** (a "vertical") is one kind of role you're pursuing. Each lane has its
own search terms, title rules, drop rules, scoring rubric and resume shape. A
posting belongs to exactly one lane and is scored and tailored in it. Lanes are
pure config: `src/` names no lane, search term or company.

Read a command's `.md` in `.claude/commands/` before running it. That is where
the real orchestration lives. Two rules shape the code: no module in `src/`
calls a model, and `uv run track` is the only writer of a role's state. Both are
in `CLAUDE.md`.

### 1. Discovery

Finds postings and keeps one row per posting in `jobs/jobs.parquet`. Sources are
inbox clips (`inbox/*.md`), LinkedIn and Indeed searches, a nightly public feed,
and company boards on Greenhouse, Lever, Ashby, Workday and Eightfold, listed in
`data/universe/boards.csv`. Each board is checked every `every_days`; a board
that keeps answering 404 is marked dead and checked less often. Every posting
gets a stable `job_id`. The output everything downstream reads is
`jobs/clean.parquet`: recent postings whose title fits a lane and holds no
ban word, inside your location allowlist, one row per `job_id`.

- Slash command: `/discover` runs it and prints a short health digest.
- CLIs: `uv run discover`, `uv run ingest-url <url>` (one posting),
  `uv run discovery-check` (the resolved config), `uv run discover-digest`,
  `uv run discover-dupes`, `uv run discover-ids`.
- `uv run python scripts/dev/audit_slugs.py` says which boards are active, empty, stale or
  dead; a board that answers 200 with no postings never looks dead otherwise.
- Config: `profile/discovery.yaml`.
- Details: `.claude/rules/discovery.md`.

### 2. Scoring

Drops what your hard rules reject (no lane, an ineligibility phrase), then
parallel judge agents score the rest on
title, skills, seniority and domain. Python turns their facts into the
sponsorship label, caps, total and action. The keeper list is
`shortlist/<date>.md`: the top roles per lane. **Each lane ranks on its own**; a
score in one lane is never compared with another.

- Slash command: `/score` (`--all` re-judges, `--vertical V` one lane,
  `--stale-only` counts rows whose input changed). Re-judge after editing your
  bullets, a rubric or `scoring.yaml`.
- CLI: `uv run score start|finish|show|lane`.
- Config: `profile/scoring.yaml`, each lane's `rubric.md`, and
  `.claude/shared/scoring_rubric.md`.
- Details: `.claude/rules/scoring.md`.

### 3. Application material

Builds a one-page resume, a cover letter, company "why us" answers and outreach
drafts for one role. The session picks bullets and wording; code assembles,
lints, renders and traces it. A build with a problem prints it and writes
nothing. Everything for a role lands in `applications/<lane>/<dir>/`.

- Slash commands: `/tailor <job_id>`, `/cover-letter <job_id>`,
  `/company-answers <job_id>`, `/outreach <job_id> <channel> --to "Name"`
  (never sends), `/ingest <url> <lane> resume cover-letter` (one URL through
  scoring, resume and letter; stops before applying). Also `/interview` (mock
  interviews on your own resume's claims), `/suggest-synonyms` and `/no_ai_slop`.
- CLIs: `uv run app new|dir|build|dupes|schema|lint`, `uv run synonyms`,
  `uv run profile-extract <file>`.
- Lanes: `/new-vertical <name>` adds one, `/tune-vertical <name>` sharpens one;
  `uv run verticals-check` validates them and the resume sources.
- Details: `.claude/rules/verticals-config.md`, `.claude/rules/linting.md`,
  `.claude/shared/no_fab.md`.

### 4. Tracking

Each role has a state (`saved`, `skip`, `tailored`, `applied`, `screen`,
`interview`, `offer`, `rejected`, `withdrawn`). Every change is one line in
`pipeline/events.jsonl`; `pipeline/<job_id>/state.yaml` is a cache of it. An
`applied` role with no news for `ghosted_after_days` shows as `ghosted`.

- Slash commands: `/track <job_id> <state>`, `/standup` (rebuilds
  `pipeline.md`).
- CLIs: `uv run track` (also `states`, `cleanup`, `rebuild-states`),
  `uv run doctor` (where folders, state and logs disagree; read-only).
- Details: `.claude/rules/state-machine.md`.

### 5. Submission

Reads the board's form, answers it from your answer bank
(`profile/application_answers.yaml`), and parks the role on anything it can't
answer. In `/apply` you answer what the bank doesn't know and confirm; every
confirmed answer is saved, so the next form asks less. A submit is bounded: it
needs `--limit` (or one `--job-id`), waits at least 30s between roles, sends one
per company per run, skips companies applied to within `company_cooldown_days`,
and never clicks a role already in `applications/apply_runs/submitted.jsonl`.
A batch takes the highest fit scores first.
Workday, LinkedIn, Indeed and careers pages are always manual.

- Slash command: `/apply <job_id> [--submit] [--letter]`.
- CLI: `uv run apply prepare|plan|run|confirm|learn|teach|wordings|triage|sheet|resolve-urls`.
- Run reports and evidence: `applications/apply_runs/`.
- Details: `.claude/rules/apply-submission.md`.

## Setup

You need [uv](https://docs.astral.sh/uv/), Python 3.12 and Claude Code. PDF
output needs Microsoft Word on macOS. Without Word, pass
`uv run app build ... --no-pdf`, which writes only the `.docx`.

**1. Install.** The core install needs no C toolchain.

```bash
git clone <this repo> && cd <repo>
uv sync
uv run pytest tests -q --ignore=tests/discovery   # should be fully green
```

**2. Install libpostal.** Discovery's location filter needs it, with no
fallback.

```bash
brew install libpostal            # macOS. On Linux, build from source.
uv sync --group discovery
uv run python -c "import postal.parser"   # must exit 0
```

On Linux, build [openvenues/libpostal](https://github.com/openvenues/libpostal)
from source (about 2GB of model data; needs `autoconf`, `automake`, `libtool`,
`pkgconf` and a compiler). `.github/workflows/ci.yml` has a working recipe.

**3. Install the `/apply` browser** (optional). Every other stage works without
it. It drives its own empty Chrome profile at `.apply_profile/`, never yours.

```bash
uv sync --group discovery --group apply   # a plain `uv sync` drops both groups
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 uv run playwright install chrome   # macOS
```

**4. Fill in your profile.** In Claude Code, run `/onboarding`: five resumable
steps and eight questions, with real scored jobs on screen by step 4. It drafts
every `profile/` file. `/onboarding audit` checks an existing setup and changes
nothing. By hand: `uv run onboard-scaffold --vertical <lane> --work-auth <status>`
copies every template, then `/new-vertical <lane>` writes your first lane.

**5. Check your config.**

```bash
uv run verticals-check     # your lanes, or what's missing
uv run config-check        # every profile/ file, OK or ERROR
uv run config --help       # read-only views of the resolved config
```

**6. Enable the git hooks** once per clone: `git config core.hooksPath .githooks`.
They run ruff, mypy, the R7 scan and the tests on commit, and the PII gate on
push.

## What only you can supply

Every file below has a `.example` template in `profile/` documenting its schema.
The templates are shapes, not content, and the pipeline will not invent your
experience. Your filled-in files are gitignored.

| file | what it is |
|---|---|
| `resume.yaml` | Name, contact line, education, and one header per job or project with its bullet budget per lane. |
| `bullets.md` | Your resume bullets, the source of truth for every document. `/tailor` rewords only within the synonyms you allow. |
| `skills_master.md` | Your skills. `show` puts each on a lane's Skills line. |
| `stories.md` | How you decided things, for behavioral questions in `/apply` and `/interview`. Optional. |
| `voice_samples.md` | Messages you actually sent. `/outreach` refuses to run without it. |
| `application_answers.yaml` | The answer bank: contact details, work authorization per country, demographics, links. It grows with each confirmed `/apply`. |
| `verticals.yaml` + `verticals/<lane>/` | Your lanes: `lane.yaml`, `rubric.md`, `tailoring.md`. `verticals.example.yaml` documents every field. |
| `discovery.yaml` | Sources, location allowlist, retention. |
| `scoring.yaml` | Ineligibility phrases, score cutoffs, shortlist size. |
| `settings.yaml` | The night run's caps, outreach word limits, cooldown and ghosted windows, the `/apply` browser. |
| `contacts.yaml` | People to reach out to. Optional. |
| `resume_template.docx`, `cover_letter_template.docx` | Your Word designs. The letter's letterhead is literal text: replace `NAME` and the contact line in your copy. |

The banned-phrase list ships in `data/defaults/de_ai_rules.yaml`; a copy in
`profile/` replaces it whole.

## Daily and nightly routine

**By hand:**

```
/score                    # writes shortlist/<date>.md; read it before tailoring
/tailor <job_id>          # resume for one role
/cover-letter <job_id>    # when the board wants a letter
/apply <job_id>           # fills the form; submits only with --submit
/track <job_id> applied   # or /apply moves it for you
```

**Overnight (macOS).** `./scripts/nightly_pipeline.sh` runs `uv run night`:
discover, score, synonyms, tailor, apply prep (fills, never submits), cleanup,
then `summary.md`, mailed to you if `summary_email` is set in
`profile/settings.yaml`. A failed discover or score stops the night.
`uv run night --resume` reruns the stages that did not finish;
`uv run night --only STAGE` runs one. `DRY_RUN=1` prints only. Each night's
record is `logs/pipeline_<stamp>/run.json`. `max_spend_usd_per_night` caps what
its `claude` calls cost. A stage that hits the `claude` usage limit waits for the
reset and runs again, while the wait ends before `usage_limit_wait_until`. A
night started while another still runs exits and logs a line in
`logs/pipeline_history.log`.

To schedule it, `scripts/launchagent.example.plist` is a LaunchAgent for 20:00.
A job whose time falls while the Mac sleeps is skipped, so it needs a `pmset`
wake a few minutes earlier; the night itself holds off sleep with `caffeinate`.
`/onboarding` prints the install block at the end.

**Each morning,** read `summary.md` and follow its cues.

## Tests

```bash
uv run pytest tests -q --ignore=tests/discovery
```

Drop the `--ignore` once libpostal is installed:
`tests/discovery/test_stapply.py` errors without it. Tests run against a
fictional three-lane config in `tests/fixtures/`, never your real one. CI runs
the full suite with libpostal under `LC_ALL=C`, and the PII gate as its own job.

## Before you push: the PII gate

You will fill your fork with real contact details, employers and names. If you
push it anywhere public, `scripts/pii_scan.sh` fails the push when a
denylisted string reaches a tracked file.

```bash
cp profile/pii_denylist.example.txt profile/pii_denylist.txt   # then fill it in
git config core.hooksPath .githooks                            # once per clone
git add -A && ./scripts/pii_scan.sh                            # or run it by hand
```

It is off the setup path: `/onboarding` offers it later, because a fork you
never push doesn't need it. Set it up before your first push, and cover name,
email, phone, address, government ids, local paths, handles, other people's
names, employers and schools. The denylist itself is gitignored.

- Patterns match **whole words**. Prefix one with `~` to match inside longer
  words, a handle for instance.
- A missing denylist is an **error**, not a pass
  (`PII_SCAN_ALLOW_MISSING=1` makes it a warning on a fresh clone).
- It reads the git **index**: run it after staging.

The pre-push hook also checks the pushed commits' content, messages, author
fields and `user.email`, and the two tracked `.docx` templates. A fresh clone
has no hook until you set `core.hooksPath`, and `--no-verify` skips it; CI is
the copy neither can reach. Details: `.claude/rules/pii-gate.md`.

## License

MIT. See [LICENSE](LICENSE).

The company seed lists in `data/universe/` are vendored from
[kalil0321/ats-scrapers](https://github.com/kalil0321/ats-scrapers) (MIT); see
`data/universe/README.md` for provenance and the refresh procedure.

**On terms of service.** Scraping job boards may conflict with theirs. Whether to
run this, and against what, is your call. The same goes for `/apply`, which
automates form filling and submission into third-party ATS systems, and some of
those services restrict automated submission explicitly. Read them and decide for
yourself.
