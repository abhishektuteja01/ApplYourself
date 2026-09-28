---
description: Finish one role's application — answer what the answer bank does not know yet, get the user's OK, fill the form, and submit only with --submit. Every confirmed answer is saved to the bank, so the next form asks less.
model: opus
effort: medium
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Skill
argument-hint: <job_id> [--submit] [--letter]
---

# /apply — answer the new questions, confirm, fill, learn

`src/apply/` does everything that is a lookup: it reads the form, answers every
question the bank (`profile/application_answers.yaml`) already knows, lints
drafted text, fills the browser and guards the click. This session does the one
thing that is judgment: deciding what a question the bank has not seen means.

Every answer the user confirms here is saved to the bank by `apply run`, so the
same question — in any wording — is answered on its own next time. Only "why
this company / this role" answers stay per role, in `company_answers.yaml`.

Read `.claude/shared/no_fab.md` first. Parse `$ARGUMENTS`: the `job_id`,
whether `--submit` is present (without it this fills and stops), and whether
`--letter` is present (write a cover letter even where the board's upload is
optional).

Every command below takes literal values: write the real paths you were shown,
never a shell variable from an earlier call.

## 1. Prepare

```bash
cd "$(git rev-parse --show-toplevel)" && uv run apply prepare <job_id>
```

It checks the role (`saved` or `tailored`, a resume on file), the bank and
Playwright, and prints `OUT_DIR`, `VERTICAL`, `COMPANY_ANSWERS` and
`OVERRIDES_FILE` (reset to `{"job_id": ...}`). Any error: stop and report it.

## 2. Plan

```bash
cd "$(git rev-parse --show-toplevel)" && uv run apply plan <job_id> --questions
cd "$(git rev-parse --show-toplevel)" && uv run apply plan <job_id> --json > <OUT_DIR>/plan.json
```

- Exit 2: the posting is gone. Report `EXPIRED` and suggest `/track <job_id> skip`.
- Exit 1: report the error verbatim and stop.

`--questions` groups the form by who answers it: **KNOWN** (the bank or your
facts), **GUESSED BY KEYWORD** (the bank guessed from a keyword — check each
one), **NEW** (not in the bank), **BLOCKING** (required and unanswered). The
JSON has every field id, label, options and `work_authorization` (the facts for
this job's country).

## 3. Materials

From the plan:

- The board **requires** a cover letter (it parks on `cover_letter`), or it
  offers an optional one and `--letter` was passed, and the folder has no
  letter → run the `cover-letter` skill with `<job_id>`. An optional upload
  without `--letter` stays blank.
- A question asks why this company, why this role, or what excites you about
  the product (it parks naming `company_answers.yaml`, or it is NEW and
  company-specific) and `COMPANY_ANSWERS` is empty → run the `company-answers`
  skill with `<job_id>`.

Check each on its own; both can apply. If a skill fails, report its output and
stop. If either ran, re-run step 2: the bank's `why_company`, `why_role` and
`what_interests_you_about_product` questions now fill from the new file.

Then, only if the plan has no cover-letter park:

```bash
cd "$(git rev-parse --show-toplevel)" && uv run track <job_id> tailored --if saved --note "resume on file; board needs no cover letter, or one exists"
```

## 4. Answer what the bank does not know

Work through every **GUESSED BY KEYWORD**, **NEW** and **BLOCKING** item, plus
anything in **KNOWN** whose value is plainly wrong for its label. Run
`uv run apply learn` once to see every question id the bank has.

For each, decide which question it really asks — start from the plan's
**closest in the bank** ids, and only invent a new id when none of them asks the
same thing — then write one entry into `OVERRIDES_FILE` keyed by the field id:

```json
"<field_id>": {"value": "...", "question": "<bank id>", "source": "<where it came from>", "label": "<the board's label>"}
```

`value` is a string, or a list for a multi-select. For a choice it must be one
of the board's own options, verbatim. `question` is what makes the run learn:
use the id of the bank question this label asks, or a new short id
(`project_proud`, `years_experience`) when none does. A new question is saved
only when its `source` is about you — a `B-`/`S-`/`SKILL-` id, `option`, or
`user`; an answer from the JD or `company_answers.yaml` stays with this role.
If the bank already answers this question differently from what this form
needs, it is a different question: give it its own id.

For a question boards word many ways, also add `"keyword"`: two to four words
every wording of it shares and no other question does (`"proud of"`, `"hardest
problem"`, `"ai assisted coding"` — never `"experience"` or `"tell us"`). The
next board's wording is then guessed from it and shown to the user as a guess.
A keyword that would shadow another question is refused on its own; the answer
is still saved.

| the label asks | answer from | `source` |
|---|---|---|
| the same thing as a bank question, in other words | that question's answer | the id |
| a keyword guess, and the guess is right | nothing — leave it; the run saves the wording | — |
| a keyword guess, and the guess is wrong or half an answer | the full right answer, under the right id | as below |
| why this company / role / product | `company_answers.yaml`, the matching key; `question` is `why_company`, `why_role` or `what_interests_you_about_product` | `company_answers` |
| a salary figure, and the JD states one | the figure; `question: salary` | `jd` |
| a work-authorization option the code could not pick | the option whose every claim is true for `work_authorization`; add `"claim": {"authorized": ..., "requires_sponsorship": ...}` stating what the option says | `work_auth` |
| a choice the board lists every answer to | the true option, from your facts, `bullets.md` or `skills_master.md` | `option` |
| your experience, a decision, a hard problem, a project | `profile/stories.md` for how you decided (match its `kind`), `profile/bullets.md` for what you built; 1–3 sentences, NO-FAB and REPHRASE-LICENSE, never from an `evidence:` line | the `S-` or `B-` id |

A question the bank already answers but that this role needs said differently
(a length cap in the board's `description`) gets a fresh answer under the same
`question` id.

**Leave these parked**, however obvious a guess looks, and say why in step 6:

- export control or "U.S. person" — the user's to answer, every time;
- consent the user has not stated (AI notetakers, recordings);
- a preference between the employer's own options (which office) that your
  files do not settle;
- a choice where no offered option is true (a clearance list with no "None");
- a fact about the user that no file states — ask for it in step 6.

## 5. Polish new prose

Run the `no_ai_slop` skill in edit mode over every value you drafted in step 4
(not an option string, not a figure, not an answer copied from the bank or
`company_answers.yaml`). Voice and structure only: no new claim, tool, metric
or date. Write the edited text back into the same entries.

## 6. Check, then ask the user

```bash
cd "$(git rev-parse --show-toplevel)" && uv run apply plan <job_id> --questions --answers <OVERRIDES_FILE>
```

- Exit 1 with `OVERRIDES IGNORED`: an id not on the form, an identity/EEOC
  field, or export control. Fix the entry and re-run.
- A field parked with `lint: banned phrase(s)`: rewrite that sentence
  (`no_fab.md`'s fix loop, at most 5 plans), then re-run.

Show the user, in full, never summarized:

```
<field_id> — "<label>"
  <value>                      [<source>] -> saved as <question id>
```

for every entry you wrote, then every **GUESSED BY KEYWORD** line that you left
standing (it will be saved as that question's wording), then every question
still parked and why. Say plainly that confirming saves these to the answer
bank. **Stop and wait for the reply.**

- Confirmed → step 7.
- A change → edit the file, re-run this step, ask again.
- A fact the user states for a parked question → add it as an entry (source
  `user`), re-run this step, ask again.
- No, or no reply → stop. Report where `OVERRIDES_FILE` is. Nothing is saved.

## 7. Fill, and submit only with `--submit`

```bash
cd "$(git rev-parse --show-toplevel)" && uv run apply run --job-id <job_id> --answers <OVERRIDES_FILE> --yes --manifest <OUT_DIR>/form_manifest.json [--submit]
```

One browser, one fill. It saves the confirmed answers to the bank, fills the
form, reads every field back into the manifest, and — with `--submit` — clicks
only if every filled field holds what was planned. A mismatch refuses the click
(`failed`, "form check refused the click") and nothing is sent. **Filling
uploads the resume and cover letter to the board**, submit or not.

`--yes` stands in for the CLI's typed prompt; step 6 was the confirmation. The
CLI refuses a role already in `applications/apply_runs/submitted.jsonl`, writes
that ledger line before the click, and moves the role to `applied` through
`/track` only when the board accepted it.

Read the manifest and check:

1. a `filled` row with `matches: false` — the page holds something else;
2. a `parked`, `left blank` or `skipped` row with a non-empty `actual` — the
   board or its resume parser put an answer there;
3. `options` on a row whose plan had none — a parked choice may now be
   answerable.

For anything found, fix or add the entry (step 4's rules), re-run step 6, show
the change, and run this step again. Two rounds at most; if the same field
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
