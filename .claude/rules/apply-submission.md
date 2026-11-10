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

## The answer bank

`profile/application_answers.yaml` (schema 4) is the whole bank:

- **what the user states**: `identity`, `education`, `employment` (every key
  left out is read from `profile/resume.yaml` by `src/applyourself/apply/resume_facts.py`:
  name, mailto:/tel: contact, the first plain contact line, `education[0]`, the
  work context that ended last; employment only when the bank has the block),
  `demographics.answers` (topic -> spellings: `gender`, `race`,
  `hispanic_ethnicity`, `veteran_status`, `disability_status`,
  `sexual_orientation`), and `work_authorization` (`us_person`, `nationalities`, and a
  per-country table of `authorized`, `requires_sponsorship`, `unrestricted`,
  `status_label`, `followup`, `start_in_days`, `visa_salary_floor`
  (`{amount, currency, per: year|month, note}`, the visa's lowest gross salary),
  `salary` (`{amount, currency, markup_pct, lanes}`, what `{computed: salary}`
  answers there; `markup_pct`, home row only, scales a posting's minimum in
  `currency`), with a required `default` row);
- **`facts`**: `F-<ID>` (`^F-[A-Z0-9-]+$`) -> one plain sentence about the user
  (citizenship, visa, sponsorship now and later, onsite scope, years per skill,
  standing consents and attestations). The judge answers from these;
- **`cover_letter`**: `when_required` (default) or `when_offered`, which parks
  any cover-letter upload the folder has no letter for;
- **`questions`**: one canonical entry per question, `{answer, only_when_required?}`.
  `answer` is text, a list of spellings, `{fact: ...}`, `{computed: salary |
  start_date | how_heard}`, or `{role_file: ...}` (the role's
  `company_answers.yaml`). `{computed: ..., fallback: [...]}` adds literal
  spellings for when the computed value can't answer (salary's bare Yes/No
  widget, how_heard with no source match). `mode: exact` (default) or
  `contains` sets how a spelling matches an offered option: whole, or anywhere
  inside it. An approved prose answer also has `source`, its `B-`/`S-` ids. No
  wordings or keywords: the judge picks the question.

Scoring reads the same table: `score_filter` derives never / later / now per
country from `work_authorization.countries`, and refuses `sponsorship_need` or
`visa_free_countries` in `scoring.yaml`; it also reads `questions.languages_fluent`.

## Who answers what

Code answers identity, uploads, education and employment by DOM id (tiers A/A2),
and EEOC and demographic fields by section (your `demographics.answers` for the
topic, exact or as the start of a longer option, else the decline option).
Export control is answered by code only for a "U.S. person" question from an
explicit `us_person`. A question's country is the one its label names
(`src/applyourself/shared/countries.py`, with US states and look-alikes masked),
else the job's (`role_facts.job_country`, never read from place names): the
session's optional `"country"` in the answers file, else the score judge's one
country (`judge_answer.country`), else the one code the postings' source gave
(the store's `country`), else `UNKNOWN_COUNTRY`, and every answer that depends on
the job's country parks. `apply confirm` shows it.

The apply-judge (`.claude/agents/apply-judge.md`) answers every other field, on
every role. `uv run apply judge-pack <id>` writes its packet from the saved
`plan.json` (fields, facts, questions, the country's work-authorization row);
`uv run apply judge-check <id>` refuses malformed output, naming each problem,
and merges the rest into `answers_override.json`, never over an entry without a
`decision`. One entry per field, one decision:

- `question`: code renders that bank question (computed and role-file answers too);
- `fact`: `{fact: authorized|requires_sponsorship|unrestricted|us_person, yes_when}`;
  code computes Yes/No from `work_authorization` for the question's country;
- `option`: a board option, matched word for word;
- `text`: prose, linted, or short factual text;
- `ask`: parks the field with the question for the user;
- `skip`: optional junk only; a skip on a required field parks.

`option` and `text` need a `basis`: `F-` facts, `B-`/`S-`/`SKILL-` profile ids,
bank question ids, work-authorization keys, `jd`, `company_answers` or
`option`. Every id must exist, or the field parks.

## One gate

Every `answers_override.json` entry, judged or the session's, passes
`plan.apply_override`:

- identity and EEOC (closed tiers A/A2) take no override;
- a judged `fact` answers only a work-authorization label;
- export control takes only a `us_person` fact, or an `option` once `us_person`
  is yes or no (never on Yes/No options), never a typed value;
- an option must be one the widget offers; a single checkbox takes only Yes;
- free text is linted: mechanical fixes applied, a banned phrase parks the field
  naming it;
- a judged entry the gate refuses parks its field, the refusal as the reason; one
  whose field id is not on the form still lands in `Plan.ignored_overrides`;
- a session entry it refuses (an id not on the form, a closed field, a missing
  file) lands in `Plan.ignored_overrides`, and `plan`/`run` fail while any remain.

Judged answers that pass go straight through. Only parked fields (`ask`, a refused
entry) reach the user.

## Learning: one writer

`src/applyourself/apply/bank.py` is the bank's only writer, under a typed
confirmation and never unattended. It writes two things: a user's answer to an
`ask`, as a new `facts` line, once the answers file passes `apply confirm`; and
an approved new prose answer, as a new `questions` entry with its `source`.
New prose fills without waiting; `uv run apply prose --review` lists the pending
ones and `--save` saves the approved rows, on a typed yes at a terminal only.
Each lesson is tried alone and kept only if the bank still loads; a refused
lesson is named and the rest stand. Every write keeps the previous file in
`profile/.state/bank_history/` (last 20). `apply learn` reviews and undoes.

## Evals

Run after any change to the judge prompt, the packet or the renderer:
`scripts/eval_apply.sh public|private [MODEL] [REPEAT_RUN_DIR]`, which runs
`uv run apply-eval stage`, one apply-judge per slice, `owed` retries, then
`grade` (deterministic: each entry goes through `apply_override`). The public
set is `evals/apply/` (synthetic personas and cases, tracked); the private set is
`applications/evals/apply/` (from the real bank and past runs, gitignored). The
gate: 100% on the gate groups (`evals.cases.GATE_GROUPS`), 98% or more overall,
and 99% or more agreement with a repeat run.

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
- A file is confirmed only by `apply confirm` (typed yes on a terminal), whose
  stored hash must match the file's current content. `.claude/hooks/confirmed_guard.sh`
  blocks a session from writing a `.confirmed` file, by pattern (a soft guard).
  `.claude/hooks/submit_ask.sh` asks before any Bash command naming `apply` and
  `--submit`, however it is spelled.
- With `--submit`, one browser fills, reads the form back,
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
every role scored at or above its lane's cutoff (`score_filter.lane_cutoffs`:
the lane's `score.cutoff`, else scoring.yaml's older `cutoffs.sheet`;
`score_store.view`) that is in `clean.parquet`, whose state is not past
`tailored` or closed, and whose status is not Applied or Closed; a role whose
judged (`basis: judged` or `v1`) `suggested_action` is skip (on a v2 answer, a
blocker or a zero rule) only once it has `tailored_dirs` or is past `saved` (a
legacy skip stays); highest score first, then job_id. Each status, note and submit command comes from `state.yaml`, the ledger,
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
