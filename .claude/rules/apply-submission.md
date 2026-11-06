---
paths:
  - "src/applyourself/apply/**"
  - "src/applyourself/apply_cli.py"
---

# Submission (`/apply`)

Takes a role with a resume on file (`saved` or `tailored`), fills the board's form
from the answer bank, and either submits (moving the role to `applied` through
`/track`) or parks it on what it could not answer. Greenhouse, Lever and Ashby
submit. Workday, LinkedIn, Indeed and company careers pages are manual; the run
report lists them under `manual`, not `failed`.

## One answer bank, one writer

`profile/application_answers.yaml` (schema 3) is the whole bank:

- **facts the user states**: `identity`, `education`, `employment` (every key
  left out is read from `profile/resume.yaml` by `src/applyourself/apply/resume_facts.py`:
  name, mailto:/tel: contact, the first plain contact line, `education[0]`, the
  work context that ended last; employment only when the bank has the block),
  `demographics.answers` (topic -> spellings: `gender`, `race`,
  `hispanic_ethnicity`, `veteran_status`, `disability_status`), and `work_authorization` (`us_person`, `nationalities`, and a
  per-country table of `authorized`, `requires_sponsorship`, `unrestricted`,
  `status_label`, `followup`, `start_in_days`, `visa_salary_floor`
  (`{amount, currency, per: year|month, note}`, the visa's lowest gross salary),
  `salary` (`{amount, currency, markup_pct, lanes}`, what `{computed: salary}`
  answers there; `markup_pct`, home row only, scales a posting's minimum in
  `currency`), with a required `default` row);
- **`claims`**: a board's full-sentence work-authorization option -> the facts it
  states. An option is picked only when every stated fact is true for the
  question's country;
- **`fact_wordings`**: a yes/no work-authorization or export-control label ->
  `{fact, yes_when}`. Exact wording only; the answer is computed from the fact;
- **`cover_letter`**: `when_required` (default) or `when_offered`, which parks
  any cover-letter upload the folder has no letter for;
- **`questions`**: one entry per real question. `answer` is text, a list of
  spellings, `{fact: ...}`, `{computed: salary | start_date | how_heard}`, or
  `{role_file: ...}` (the role's `company_answers.yaml`). `wordings` are exact
  labels, `keywords` substring guesses, `not` labels a keyword must never answer.

`src/applyourself/apply/bank.py` is its only writer. `apply run --answers <file>` calls it with
what the user confirmed in `/apply`: a label mapped to a question joins its
`wordings`, an unchallenged keyword guess becomes an exact wording, a corrected
guess joins that question's `not`, a board's option spelling joins the candidates
(only when the board offered none of the bank's), a new answer becomes a question,
a judged option becomes a claim, a `fact` judgment becomes a `fact_wordings`
entry. Each lesson is tried alone and kept only if the
bank still loads and still finds that lesson's question for its label; a refused
lesson is named and the rest stand. A new question's answer must come from
something about the user (`B-`/`S-`/`SKILL-`, `option`, `user`), never the JD or
a company. Facts are never written by it. Every write keeps the previous file in
`profile/.state/bank_history/` (last 20). `apply learn` reviews and undoes (a question,
one wording or keyword, one claim, one fact wording). `apply wordings --review`
(`src/applyourself/apply/wordings.py`) lists the `suggest:<fact>` wordings in saved `plan.json`
files that `fact_wordings` lacks, each with a `decision` for the user; `--save`
learns the yes rows as exact `fact_wordings`, on a typed yes at a terminal only.

Scoring reads the same table: `score_filter` derives never / later / now per
country from `work_authorization.countries`, and refuses `sponsorship_need` or
`visa_free_countries` in `scoring.yaml`.

## Finding an answer

`answers.resolve()` tries, in order: structural fields (identity, uploads,
education, employment) by DOM id; EEOC and demographic by section (your
`demographics.answers` for the topic, exact or as the start of a longer
option, else the decline option); work authorization by label shape and country; then the bank,
exact wording first and keyword second (`source` is `bank:<id>` or
`bank:<id>~<keyword>`); else Tier C, which lists the closest bank questions. A
question's country is the one its label names (`src/applyourself/shared/countries.py`,
with US states and look-alikes masked), else the job's: discovery's location
in `clean.parquet`, then `state.yaml`'s, then the title, then a JD phrase saying
where the hire works. A location nothing can read is `UNKNOWN_COUNTRY`, and
every answer that depends on the job's country parks. No location, or
"Remote", means the candidate's own country.
A label in `fact_wordings` is answered from its fact before any other
work-authorization rule, unless the board's options are sentences (claims pick
those). A yes/no authorization, sponsorship or "unrestricted" label not in
`fact_wordings` is never answered by its regex family: it parks once with
`Suggested: Yes when <fact> is true` (source `suggest:<fact>`), and the
confirmed `fact` entry becomes its exact `fact_wordings` entry. Otherwise export control answers only a "U.S. person"
question, only from an explicit `us_person`; every other one parks until judged.

## Overrides: one gate

`answers_override.json` maps field id -> `{value, source, question, claim, keyword, fact, label}`.
An override answers any field except identity/EEOC. Export control takes only
a `fact` override (`{fact, yes_when}`), which is computed, never typed. A
free-text override is linted: mechanical fixes applied, a banned phrase parks the
field naming it. An id not on the form, a closed field, or a missing file lands
in `Plan.ignored_overrides`, and `plan`/`run` fail while any remain.

## Submission is bounded

- `--submit` is off by default and needs `--limit`, unless `--job-id` names one
  role. It asks for a typed confirmation unless `--yes`; a non-tty stdin without
  `--yes` is refused. `--rate` is floored at 30s. One submission per company per run.
  A batch takes the eligible roles highest fit_score first (`apply_cli.eligible_queue`;
  job_id order, with a warning, when scores can't be read), and `--submit --limit N`
  counts only roles the company rule and cooldown let through (`submission_preview`).
- The company cooldown (`company_cooldown_days` in `profile/settings.yaml`,
  `src/applyourself/apply/cooldown.py`): a batch `--submit` skips a role whose company
  (`normalize_company`) had an application within it, "cooldown until <date>".
  Every application counts: any `applied` entry in a state_history and every
  ledger line that may have reached a board. `--job-id` bypasses it; the sheet
  shows `Cooldown` instead of `Submittable`; a hand `track <id> applied` only
  warns. The "already sent" and cooldown re-check right before the click and
  its `clicking` line share the ledger's lock (`ledger.claim`).
- `applications/apply_runs/submitted.jsonl` records what was sent. A line is
  written right before every click, and a role whose latest line is anything but
  `refused` is never clicked again, whatever its `state.yaml` says. `track
  ensure` (the `/tailor` registration) refuses to re-create a lost `state.yaml`
  for such a role. Delete its lines to retry.
- A file is confirmed only by `apply confirm` (typed yes on a terminal; it also lists
  the bank's keyword guesses the file leaves standing, offline from the role's
  saved `plan.json`), whose
  stored hash must match the file's current content. `.claude/hooks/confirmed_guard.sh`
  blocks a session from writing a `.confirmed` file, by pattern (a soft guard).
  `.claude/hooks/submit_ask.sh` asks before any Bash command naming `apply` and
  `--submit`, however it is spelled.
- Without a confirmed answers file, `apply run --submit` parks a role whose plan
  holds a keyword guess. With `--submit`, one browser fills, reads the form back,
  and clicks only when every filled field holds what was planned (`form_check`).
- A click ends `accepted` (the board's submit request or confirmation text),
  `ambiguous` (no marker on an unwatched board), `unsent` (a watched board fired
  no submit request), `rejected` (the board said no: spam flag, duplicate) or
  `refused` (the board declined every click). Only `accepted` and `ambiguous`
  are tracked as `applied`.
- `submit()` is the only function that clicks submit; a static test enforces it.
- `employment.only_when_required: true` fills the employment block only where a
  board requires it.

## The status sheet is derived

The work queue is `sheet.queue()`, computed and never read back from the xlsx:
every role scored at or above scoring.yaml `cutoffs.sheet` (`score_store.view`)
that is in `clean.parquet`, whose state is not past `tailored` or closed, and
whose status is not Applied or Closed; a role whose derived (`basis: judged`)
`suggested_action` is skip only once it has `tailored_dirs` or is past `saved`
(a legacy skip stays); highest score first, then job_id. Each status, note and submit command comes from `state.yaml`, the ledger,
the newest run record naming the role, and answers waiting for confirmation.
Where a role is applied through is `apply_url.resolve()`, read by the sheet and
`resolve_url` alike, with no network call: the `apply_url` stored in
`jobs/apply_urls.json`, the clean.parquet and state.yaml urls, then a board link
in the JD. A role with none ends `Manual apply` (a real posting on a board
`/apply` cannot drive, or a careers page) or `No apply URL` (a LinkedIn or Indeed
listing), the note saying why; the summary counts both. `apply resolve-urls
--dry-run|--apply` is the only fetch: it looks the title up on the company's own
Greenhouse, Lever or Ashby board from `boards.csv` and stores hit or miss. The
title matches when normalized, or when exactly one posting differs only in words
`dedupe.titles_differ(loose=True)` ignores (that also reads "artificial
intelligence" as "ai" and drops a trailing "at <the role's company>"; discovery
merges stay strict); a miss stored under an older `apply_url.MATCHER`
is looked up again. The night never runs it.
`uv run apply sheet` rewrites `profile/settings.yaml`'s `table_path` from it
(under a lock, tmp file then rename); `--list STATUS` prints the queue at a status
(the night stages and triage read this); `--compare` prints how the xlsx differs
from the queue. `--add` is accepted and does nothing extra. Bank writes
(`learn`, `undo`) hold the bank's lock across the read-modify-write.

`apply run` writes `apply_runs/<ts>.json` (`src/applyourself/apply/run_record.py`: per role
`category`, a `ReasonCode`, `fields_parked`, timestamps) and renders `<ts>.md`
from it. The sheet and summary read the JSON. `src/applyourself/apply/run_backfill.py` is the
one markdown parser: a one-time `--dry-run|--apply` that writes JSON for old
reports; until it runs, a report with no JSON is read through it.

## Ashby reads a JSON API

`ashby.load_board` POSTs the `ApplicationForm` GraphQL query and folds in
`fetch_dom_enrichment`, one headless page load, for field descriptions.
