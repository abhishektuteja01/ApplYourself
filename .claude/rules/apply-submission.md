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

## Ashby reads a JSON API, not HTML

Ashby's form is client-rendered, so `ashby.load_board` POSTs the `ApplicationForm`
GraphQL query. The one thing that query never declares is per-field description
text, so `load_board` folds in `fetch_dom_enrichment`, one headless page load.
`scan_ashby_form` is not on that path and has no caller in `src/`: it reads a
rendered Ashby form only, held for a future fill driver.
