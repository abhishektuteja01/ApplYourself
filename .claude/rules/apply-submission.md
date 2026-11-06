---
paths:
  - "src/apply/**"
  - "src/apply_cli.py"
---

# Submission (`/apply`)

Takes a role with a resume on file (`saved` or `tailored`), fills the board's form
from the answer bank, and either submits (moving the role to `applied` through
`/track`) or parks it on what it could not answer. Greenhouse, Lever and Ashby
submit. Workday, LinkedIn, Indeed and company careers pages are manual; the run
report lists them under `manual`, not `failed`.

## One answer bank, one writer

`profile/application_answers.yaml` (schema 2) is the whole bank:

- **facts the user states**: `identity`, `education`, `employment`,
  `demographics.answers` (topic -> spellings: `gender`, `race`,
  `hispanic_ethnicity`, `veteran_status`, `disability_status`), and `work_authorization` (`us_person`, `nationalities`, and a
  per-country table of `authorized`, `requires_sponsorship`, `unrestricted`,
  `status_label`, `followup`, `start_in_days`, `visa_salary_floor`
  (`{amount, currency, per: year|month, note}`, the visa's lowest gross salary),
  with a required `default` row);
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

`src/apply/bank.py` is its only writer. `apply run --answers <file>` calls it with
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
`profile/.bank_history/` (last 20). `apply learn` reviews and undoes (a question,
one wording or keyword, one claim, one fact wording).

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
question's country is the one its label names (discovery's country vocabulary,
with US states and look-alikes masked), else the job's: discovery's location
in `clean.parquet`, then `state.yaml`'s, then the title, then a JD phrase saying
where the hire works. A location nothing can read is `UNKNOWN_COUNTRY`, and
every answer that depends on the job's country parks. No location, or
"Remote", means the candidate's own country.
A label in `fact_wordings` is answered from its fact before any other
work-authorization rule. Otherwise export control answers only a "U.S. person"
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
- `applications/apply_runs/submitted.jsonl` records what was sent. A line is
  written right before every click, and a role whose latest line is anything but
  `refused` is never clicked again, whatever its `state.yaml` says. `track
  ensure` (the `/tailor` registration) refuses to re-create a lost `state.yaml`
  for such a role. Delete its lines to retry.
- A file is confirmed only by `apply confirm` (typed yes on a terminal; it also lists
  the bank's keyword guesses the file leaves standing), whose
  stored hash must match the file's current content. `.claude/hooks/confirmed_guard.sh`
  blocks a session from writing a `.confirmed` file.
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

`uv run apply sheet [--add]` (`src/apply/sheet.py`) rebuilds every row's status,
note and submit command in `profile/automation.yaml`'s `table_path` from
`state.yaml`, the ledger, the newest run report naming the role, and answers
waiting for confirmation. Nothing else writes those cells.

## Ashby reads a JSON API

`ashby.load_board` POSTs the `ApplicationForm` GraphQL query and folds in
`fetch_dom_enrichment`, one headless page load, for field descriptions.
