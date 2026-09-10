---
paths:
  - "src/apply/**"
  - "src/apply_cli.py"
---

# Submission (`/apply`)

Consumes roles with a resume on file (`saved` or `tailored`; a cover letter is
produced only when the board's own form asks for one), fills the board's
application form from `profile/application_answers.yaml`, and either submits
(transitioning to `applied` through `/track`) or parks the role on whatever it
could not resolve.

Greenhouse, Lever and Ashby all submit. Workday is discovered and scored like any
other row but is **manual-apply only** — `src/apply/` never submits to one. Workday
is not alone: the shortlist and `/apply run`'s report both derive the flag from
`apply.detect.is_auto_submittable`, so LinkedIn, Indeed and company careers pages
carry it too, and the run report gives them their own `manual` category rather than
`failed`.

Read `submit_plan.md` (gitignored) for the phase detail.

## Submission is bounded

- `--submit` is off by default.
- `apply run --submit` requires an explicit `--limit`, unless `--job-id` names one
  role.
- It prints the roles it is about to apply to and requires a typed confirmation
  unless `--yes` is passed. A non-tty stdin without `--yes` is refused, never
  auto-confirmed. `/apply` passes `--yes` because its Step 6b is the confirmation.
- `--rate` is clamped to a 30s minimum.
- At most one role per company is submitted per run.

## Two answer stores, and only one of them is the user's

`profile/application_answers.yaml` is hand-written and holds what the user
*stated*: identity, work-authorization status, education, real preferences. It is
fail-closed — any validation error refuses the run — and **no command ever edits
it**. A person reads this file when an answer looks wrong, so it stays small.

`profile/.apply_learned.jsonl` (`src/apply/learned.py`) holds what `/apply` has
*learned* from forms already seen: another wording of a question, a spelling of an
option some board offers, a veto stopping a keyword from matching a label it gets
backwards, or a standalone question with a stable factual answer. Append-only,
gitignored, no `.example` template (the leading dot is what exempts it — see
`profile-templates.md`), and fail-open: a malformed line is skipped with a warning,
because a convenience must never block a submission.

`load_answers(learned_path=...)` folds the store into `rules:` **before**
`_parse_rules` validates anything, so a learned wording faces the same overlap
check, keyword-length floor and work-authorization keyword ban as a written one. A
learned wording is absorbed into the rule it belongs to rather than appended as a
new rule, because a new rule whose keyword is a superstring of an existing one is
exactly what the overlap check rejects. Learned candidates always append *after*
configured ones, so a stated preference wins.

`uv run apply learn` is the store's sole writer. **R7 is intact:** the command
session decides what is worth learning, `src/` validates and appends — the same
split as `/track` owning `state.yaml` through `src/state_io.py`.

## The plan names what answered each field, and what it was chosen from

`Resolution.source` carries the `match:`/`exact:` keyword that fired (or
`how_heard`, or `override:<tier>`), and `FieldPlan.options` carries what the widget
offered even for a field that filled. Both reach the plan JSON. `/apply`'s audit
step exists because a Tier B answer is a keyword match against employer-authored
prose: the same keyword can answer one label correctly and another backwards, and
neither case is visible from the value alone.

Override tiers are declared in `apply_cli.OVERRIDE_TIERS`; which of them may
supersede which resolution tier is decided in `build_plan` alone. `PICK` reaches a
parked Tier B or C choice and names one of the board's own options. Tier B0 keeps
`B0-LLM`; Tier A and A2 accept nothing, and an override aimed at one is reported in
`Plan.ignored_overrides` rather than dropped in silence.

## Ashby reads a JSON API, not HTML

Ashby's form is client-rendered, so `ashby.load_board` POSTs the `ApplicationForm`
GraphQL query. The one thing that query never declares is per-field description
text, so `load_board` folds in `fetch_dom_enrichment`, one headless page load.
`scan_ashby_form` is not on that path and has no caller in `src/`: it reads a
rendered Ashby form only, held for a future fill driver.
