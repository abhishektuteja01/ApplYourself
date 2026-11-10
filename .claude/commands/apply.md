---
description: Finish one role's application — answer what the answer bank does not know yet, get the user's OK, fill the form, and submit only with --submit. Every confirmed answer is saved to the bank, so the next form asks less.
model: opus
effort: medium
allowed-tools:
  - Bash(uv run apply prepare *)
  - Bash(uv run apply plan *)
  - Bash(uv run apply judge-pack *)
  - Bash(uv run apply judge-check *)
  - Bash(uv run apply run *)
  - Bash(uv run apply learn *)
  - Bash(uv run app *)
  - Bash(uv run track *)
  - Read
  - Write
  - Edit
  - Skill
  - Agent
argument-hint: <job_id> [--submit] [--letter]
---

# /apply — judge the form, ask what is left, fill, learn

`src/applyourself/apply/` answers identity, EEOC, education, employment, uploads
and export control, fills the browser and guards the click. The `apply-judge`
agent answers every other field from the bank (`profile/application_answers.yaml`).
This session runs the judge and asks the user only what it could not settle.
Each answer the user gives is saved to the bank as a fact.

Read `.claude/shared/no_fab.md` first. Parse `$ARGUMENTS`: the `job_id`,
whether `--submit` is present (without it this fills and stops), and whether
`--letter` is present (write a cover letter even where the board's upload is
optional).

Every command below takes literal values: write the real paths you were shown,
never a shell variable from an earlier call.

## 1. Prepare

```bash
uv run apply prepare <job_id>
```

It checks the role (`saved` or `tailored`, a resume on file), the bank and
Playwright, and prints `OUT_DIR`, `VERTICAL`, `COMPANY_ANSWERS` and
`OVERRIDES_FILE` (reset to `{"job_id": ...}`). Any error: stop and report it.

```bash
uv run app dupes <job_id>
```

Each entry in `duplicates` is another job_id that may be this same role and is
already tailored, applied or submitted. If there is any, show each (job_id,
company, state, `submitted`) and ask the user whether to go on before step 2;
on a no, stop and suggest `/track <job_id> skip`.

## 2. Plan

```bash
uv run apply plan <job_id> --questions
uv run apply plan <job_id> --json > <OUT_DIR>/plan.json
```

- Exit 2: the posting is gone. Report `EXPIRED` and suggest `/track <job_id> skip`.
- Exit 1: report the error verbatim and stop.

`--questions` groups the form by who answers it. The JSON has every field id,
label, options and `work_authorization` (the facts for this job's country).

## 3. Materials

From the plan:

- The plan parks on `cover_letter` (the board requires one, or the bank says
  `cover_letter: when_offered`), or the board offers an optional one and
  `--letter` was passed, and the folder has no letter → run the `cover-letter`
  skill with `<job_id>`. Otherwise an optional upload stays blank.
- A question asks why this company, why this role, or what excites you about
  the product (it parks naming `company_answers.yaml`, or it is NEW and
  company-specific) and `COMPANY_ANSWERS` is empty → run the `company-answers`
  skill with `<job_id>`.

Check each on its own; both can apply. If a skill fails, report its output and
stop. If either ran, re-run step 2: the bank's `why_company`, `why_role` and
`what_interests_you_about_product` questions now fill from the new file.

Then, only if the plan has no cover-letter park:

```bash
uv run track <job_id> tailored --if saved --note "resume on file; board needs no cover letter, or one exists"
```

## 4. Judge

```bash
uv run apply plan <job_id> --json > <OUT_DIR>/plan.json
uv run apply judge-pack <job_id>
```

It prints one line, `judge <packet> -> <out>`. Spawn the `apply-judge` agent
with that exact line as its prompt, then:

```bash
uv run apply judge-check <job_id>
```

Exit 0 merges the judged entries into `OVERRIDES_FILE`. Exit 1 prints the
problems: spawn the judge once more with the same line and re-check. Still
exit 1: stop and report the problems.

## 5. Prose

The judge drafts new prose under NO-FAB, and `apply plan` lints it. Do not edit
it.

## 6. Ask only what is parked

```bash
uv run apply plan <job_id> --questions --answers <OVERRIDES_FILE>
```

- Exit 1 with `OVERRIDES IGNORED`: report it verbatim and stop.
- Nothing parked: go to step 7. No confirmation.
- Something parked (a judge `ask`, or a field the code parks): show the user,
  in full, each parked field with its reason, and for an `ask` the judge's
  question:

  ```
  <field_id> — "<label>"
    <the judge's question, or the park reason>
  ```

  Also list each new drafted prose answer (label, value, basis). It does not
  block. **Stop and wait for the reply.**

- The user answers an `ask` → write it into `OVERRIDES_FILE` keyed by the field
  id, `{"value": "...", "source": "user", "label": "<the board's label>"}`, and
  tell the user this fact will be saved to the bank. Re-run this step. Once
  nothing they can answer is left, tell them to run
  `! uv run apply confirm <OVERRIDES_FILE>` and type yes; never run it yourself
  and never write `"confirmed"` into the file. Once it prints `Confirmed:`,
  step 7.
- No, or no reply → stop. Report where `OVERRIDES_FILE` is. Nothing is saved.
- Scheduled run with something parked → stop; `uv run apply sheet` marks the
  role "Needs your OK".

## 7. Fill, and submit only with `--submit`

```bash
uv run apply run --job-id <job_id> --answers <OVERRIDES_FILE> --yes --manifest <OUT_DIR>/form_manifest.json [--submit]
```

One browser, one fill. It saves the confirmed answers to the bank, fills the
form, reads every field back into the manifest, and — with `--submit` — clicks
only if every filled field holds what was planned. A mismatch refuses the click
(`failed`, "form check refused the click") and nothing is sent. **Filling
uploads the resume and cover letter to the board**, submit or not.

`--yes` stands in for the CLI's typed prompt; step 6's `apply confirm`, when there was one, was the confirmation. The
CLI refuses a role already in `applications/apply_runs/submitted.jsonl`, writes
that ledger line before the click, and moves the role to `applied` through
`/track` only when the board accepted it.

Read the manifest and check:

1. a `filled` row with `matches: false` — the page holds something else;
2. a `parked`, `left blank` or `skipped` row with a non-empty `actual` — the
   board or its resume parser put an answer there;
3. `options` on a row whose plan had none — a parked choice may now be
   answerable.

For anything found, re-run steps 4 and 6, show the change, and run this step
again. Two rounds at most; if the same field
still disagrees, stop and report it.

## 8. Report

Read the run report the command printed and tell the user:

```
<job_id> — <category>
<the report's detail line for this role>
Answered from the bank: <n> · saved to the bank: <n>
  <each "learned" line>
Still parked: <label> — <why>, one per line
Rerun: uv run apply run --job-id <job_id> --answers <OVERRIDES_FILE> --manifest <OUT_DIR>/form_manifest.json [--submit]
```

Categories: `submitted` (accepted), `submitted_unconfirmed` (clicked, no
confirmation text — check the board), `submitted_untracked` (sent but
`state.yaml` not updated — lead with this and name the job), `rejected` (the
board refused it; read its reason), `unsent` (the click fired no request —
verify by hand), `parked`, `ready` (rerun with `--submit`), `manual` (no submit
path for this board), `skipped` (already sent, or another role at this company
went out this run), `failed`, `expired`.
