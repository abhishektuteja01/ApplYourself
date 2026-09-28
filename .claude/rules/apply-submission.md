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
  `demographics`, and `work_authorization` (`us_person`, `nationalities`, and a
  per-country table of `authorized`, `requires_sponsorship`, `unrestricted`,
  `status_label`, `followup`, `start_in_days`, with a required `default` row);
- **`claims`**: a board's full-sentence work-authorization option -> the facts it
  states. An option is picked only when every stated fact is true for the
  question's country;
- **`questions`**: one entry per real question. `answer` is text, a list of
  spellings, `{fact: ...}`, `{computed: salary | start_date | how_heard}`, or
  `{role_file: ...}` (the role's `company_answers.yaml`). `wordings` are exact
  labels, `keywords` substring guesses, `not` labels a keyword must never answer.

`src/apply/bank.py` is its only writer. `apply run --answers <file>` calls it with
what the user confirmed in `/apply`: a label mapped to a question joins its
`wordings`, an unchallenged keyword guess becomes an exact wording, a corrected
guess joins that question's `not`, a board's option spelling joins the candidates,
a new answer becomes a question, a judged option becomes a claim. Facts are never
written by it, and "why this company" text never enters the bank. Every write
validates the whole bank first and keeps `<file>.bak`. `apply learn` reviews and
undoes.

## Finding an answer

`answers.resolve()` tries, in order: structural fields (identity, uploads,
education, employment) by DOM id; EEOC and demographic by section (always the
decline option); work authorization by label shape and country; then the bank,
exact wording first and keyword second (`source` is `bank:<id>` or
`bank:<id>~<keyword>`); else Tier C. A question's country is the one its label
names, else the job's (parsed from its location), else the candidate's own.
Export control answers only a "U.S. person" question, only from an explicit
`us_person`; every other export-control question always parks.

## Overrides: one gate

`answers_override.json` maps field id -> `{value, source, question, claim, label}`.
An override answers any field except identity/EEOC and export control. A
free-text override is linted: mechanical fixes applied, a banned phrase parks the
field naming it. An id not on the form, a closed field, or a missing file lands
in `Plan.ignored_overrides`, and `plan`/`run` fail while any remain.

## Submission is bounded

- `--submit` is off by default and needs `--limit`, unless `--job-id` names one
  role. It asks for a typed confirmation unless `--yes`; a non-tty stdin without
  `--yes` is refused. `--rate` is floored at 30s. One submission per company per run.
- `applications/apply_runs/submitted.jsonl` records what was sent. A line is
  written right before every click, and a role with any line is never clicked
  again, whatever its `state.yaml` says. Delete its lines to retry.
- A click ends `accepted` (the board's submit request or confirmation text),
  `ambiguous` (no marker on an unwatched board), `unsent` (a watched board fired
  no submit request), `rejected` (the board said no: spam flag, duplicate) or
  `refused` (the board declined every click). Only `accepted` and `ambiguous`
  are tracked as `applied`.
- `submit()` is the only function that clicks submit; a static test enforces it.
- `employment.only_when_required: true` fills the employment block only where a
  board requires it.

## Ashby reads a JSON API

`ashby.load_board` POSTs the `ApplicationForm` GraphQL query and folds in
`fetch_dom_enrichment`, one headless page load, for field descriptions.
