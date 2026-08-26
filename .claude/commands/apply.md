---
description: Finishes a tailored role's application — drafts answers for the remaining generic, company-specific, salary and work-authorization questions, then submits (or fills and stops) via the deterministic apply CLI.
model: sonnet
effort: medium
allowed-tools:
  - Bash
  - Read
  - Edit
  - Skill
argument-hint: <job_id> [--submit]
---

# /apply — resolve Tier C questions, then fill or submit

Everything deterministic — fetching the form, resolving Tier A/A2/B0/B
fields, filling the browser, guarding the submit click — already happens in
`src/apply/` via `uv run apply`. This command exists for exactly one thing:
the questions nothing in `src/` can answer because answering them is
judgment, not a lookup — Tier C. Read `submit_plan.md` §4 and §15 if anything
below is unclear; this file is the prose half of what that spec calls out as
LLM-owned (§3's split table).

Two Tier C sub-cases, and they resolve differently:

- **C1** — generic, answerable from `profile/bullets.md`
  ("describe your experience with X"). Draft it, same NO-FAB discipline as
  `/tailor`.
- **C2** — company-specific or motivational ("why us", "what excites you
  about our product"). Resolved from that role's `company_answers.md`
  (`/cover-letter` §7b already drafted it), never freshly drafted here, and
  never from a generic template.

A third, narrower case rides alongside C1/C2: a salary/compensation
question. It resolves from the role's own `jd_snapshot.md` if the JD states
a figure, tagged `"JD"` rather than `"C1"`/`"C2"` — the one tag the
deterministic layer lets supersede a static Tier B `rules:` match, since a
figure the JD itself states should win over a generic configured default
(Step 4b).

A fourth case is not Tier C at all: a work-authorization (Tier B0) question
that already parked deterministically because no pre-configured
`status_option_candidates` string matched this board's exact wording.
Judging which of the board's full-sentence options is true given
`work_authorization`'s facts is tagged `"B0-LLM"` (Step 4c) — the one tag
that supersedes a Tier B0 park, same shape as `"JD"` for Tier B.

A fifth case double-checks the other four's foundation: a Tier B/B0 question
`src/` already resolved via a keyword/candidate match against free-form board
text. That kind of match can answer only the part of a label it recognized —
a compound label ("where did you hear about us and why do you want to work
here") gets silently half-answered and nothing re-checks it once it's
"resolved". Step 2c re-reads every such resolution for exactly this; a
correction is tagged `"AUDIT"`, the one tag that can supersede either tier,
not restricted to one category the way `"JD"`/`"B0-LLM"` are (Step 4d).

All five land in a **per-run answers override**
(`${OUT_DIR}/answers_override.json`, next to the role's other artifacts) —
never in `profile/application_answers.yaml`. That file is per-role; a
company's "why us" answer or a one-off drafted sentence must never leak into
another role's run. The one exception, a separate and much narrower path,
is §15's Tier B writeback (Step 5 below) — and that only ever carries a
reusable *fact*, never drafted prose.

---

**Before anything else, read `.claude/shared/no_fab.md`.** This command
cites NO-FAB and REPHRASE-LICENSE by name for the C1 path.

## Step 1 — prerequisites (one call, fail loud)

`uv run apply prepare` runs every deterministic check: `pipeline/$JOB_ID/state.yaml`
exists and its state is `saved`/
`tailored`, `application_answers.yaml` exists, playwright is importable —
and derives `OUT_DIR`/`VERTICAL`/`COMPANY_ANSWERS` from state.yaml and resets
that role's `answers_override.json` (§15: the `job_id` key binds the file to
this role, so `apply --answers` refuses a file drafted for another one).

```bash
cd "$(git rev-parse --show-toplevel)" || { echo "ERROR: not inside the repo."; exit 1; }
JOB_ID="$1"
test -n "$JOB_ID" || { echo "ERROR: /apply requires a job_id argument."; exit 1; }

PREPARE_OUT=$(uv run apply prepare "$JOB_ID") || exit 1
eval "$PREPARE_OUT"
echo "$PREPARE_OUT"
```

`saved` is a valid entry state, not just `tailored` — whether this role needs
a cover letter before it can become `tailored` is exactly what Step 2c
decides, per role, from the board's own form. A missing cover letter is NOT
a hard prerequisite here either; Step 2c re-checks it against this role's
actual plan, by which point it's known whether any C2 question or required
cover-letter upload genuinely exists.

If ANY check fails, exit immediately. **No partial work.**

Parse `$ARGUMENTS` for an optional `--submit` flag. Its absence means
fill-and-stop for this role — same default as `uv run apply run` (§13: never
a config default, always per invocation).

**Submission bounds** — `--submit` is off by default; `apply run --submit`
requires an explicit `--limit` (unless `--job-id` names one role); `--rate` is
clamped to a 30s minimum; at most one submission per company per run. The CLI
also prints the roles it is about to apply to and demands a typed confirmation
unless `--yes` is passed; this command always passes `--yes`, because Step 6b
is that confirmation.

## Step 2 — the first plan

```bash
cd "$(git rev-parse --show-toplevel)" && uv run apply plan "$1" --json > /tmp/apply_$1_plan.json
echo "exit code: $?"
```

- Exit `2` → the posting expired. Report `EXPIRED: <message>` to the user
  and stop. This is an ordinary outcome (§14: 6/45 live postings), not a bug
  — suggest `/track $1 skip` and stop here.
- Exit `1` → a config or fetch error. Report the message verbatim and stop.
  Do not attempt to work around it.
- Exit `0` → read `/tmp/apply_$1_plan.json` and continue to Step 2c
  regardless of `"parked"`/`"draftable"` — even a role with nothing
  outstanding in Tier C still needs Step 2c's self-promotion check (a role
  sitting at `saved` with nothing left to resolve is exactly the "board
  never asked for a cover letter" case, and nothing else fires that
  transition for it). Step 2c's later steps (4, 4b, 4c, 4d, 5) are each individually
  skippable and become no-ops when their pool is empty.

## Step 2c — classify Tier C, gate on cover-letter need, self-promote

This is the single classification pass this command ever runs — the gate
below and Steps 4/4b/5's drafting all reuse its output, so `/apply` never
reaches C1/C2 drafting without having already confirmed, in this run, that
whatever it needs is either absent or already on disk.

Read every Tier C question from the plan JSON's `"unmapped"` (filter
`"tier" == "C"`) and `"draftable"` lists together, as one pool — required or
optional makes no difference to how a question gets classified or answered,
only to what happens if it stays unresolved (parks the role vs. stays blank).
`Read` each one's `"label"` (and `"options"`/`"description"` if present) —
`"description"` is a board's own instructional text under the label
(Ashby only, so far) and can change how a question classifies or drafts.

**Also scan `"fields"` (filter `"tier" == "B"`) for a salary/compensation-shaped
label.** A board with a static Tier B `rules:` match (the common case — see
the template's own `salary`/`compensation` entry) resolves that field
successfully, so it never appears in `unmapped`/`draftable` at all — skipping
this scan would make Step 4b's JD-first check silently never fire for
exactly the case it exists to handle. Note each such field's `"id"` and
`"kind"` for Step 4b.

**Also scan every `"fields"` entry with `"tier"` `"B"` or `"B0"`** (not just
the salary-shaped ones above) for a label asking more than one thing, or
asking something its `"value"` doesn't actually cover — the compounding
failure the intro describes. Note each flagged field's `"id"`/`"label"`/
`"value"` for Step 4d. This is not about re-litigating an answer that is
merely not the wording you'd have chosen — only one the value gets wrong or
leaves part of the question unaddressed.

Classify each conservatively:

- **C2** if the label names the company (`"company"` field in the plan JSON),
  its product, or asks for motivation/opinion — "why us", "why do you want to
  work here", "what excites you about", "what do you find interesting about
  our [product/mission/culture]", "why this role". **Ambiguous → C2.**
- **C1** otherwise — a generic, factual, or experience question a person
  answers the same way regardless of employer ("describe your experience
  with X", "how many years have you worked with Y", "what's your
  availability").
- **M** (money) if the label is a salary/compensation/pay question — from
  either pool above, Tier B included. Handled separately in Step 4b — do not
  fold it into C1.

Also check `"unmapped"` and `"skipped"` for an entry with `"id" ==
"cover_letter"` — present in either list means the board has a cover-letter
upload (`plan.py` puts a missing-file optional upload in `skipped`, not
`draftable`, since a file isn't something to draft). Required or optional
makes no difference to what happens next.

**Dispatch — skip entirely if `$COMPANY_ANSWERS` is already non-empty**
(never re-research/re-draft over a file that exists):

| condition | call |
|---|---|
| a `cover_letter` entry exists (either list above) | Skill `cover-letter` `$JOB_ID` |
| no `cover_letter` entry, but a C2 question exists | Skill `company-answers` `$JOB_ID` |
| neither | none |

If the called Skill errors or hard-refuses, surface its output verbatim and
stop; state stays untouched.

After dispatch (or immediately, if skipped because `$COMPANY_ANSWERS` was
already set):

```bash
cd "$(git rev-parse --show-toplevel)"
test -f "${OUT_DIR}/company_answers.md" && COMPANY_ANSWERS="${OUT_DIR}/company_answers.md"
```

**Self-promotion** — read `.claude/shared/self_promote.md` and run its
block, with `NOTE="resume on file; board needs no cover letter, or one
already exists"`. Re-reading state from disk there catches `/cover-letter`
Step 7b already firing `saved -> tailored` this run, so this is a no-op
instead of a double transition when that happened.

Continue to Step 4 with the classification from above already in hand.

## Step 4 — resolve C2 from `company_answers.md`

**If Step 2c found no C2 question, skip this step entirely** — most boards
ask none, and the file is only needed by the ones that do.

`Read` `$COMPANY_ANSWERS` (printed in Step 1 — Step 2c's gate already
guaranteed this is non-empty if a C2 question exists). It has exactly three
sections: `why_company`, `why_role`, `what_interests_you_about_product`.

Match each C2 label to a section by keyword, the same mechanism as Tier B's
`match:` keywords (§4):

| label mentions | section |
|---|---|
| "why us", "why [Company]", "why do you want to work here", "why are you interested in [Company]" | `why_company` |
| "why this role", "why are you interested in this position/role", "why do you want this job" | `why_role` |
| "what excites you about", "what interests you about", "what do you like about our product/what we build" | `what_interests_you_about_product` |

For each match: if that section's text is the literal `INSUFFICIENT_RESEARCH`,
**do not resolve it** — leave the question exactly as the plan found it
(parked if required, blank if optional). Otherwise add
`"<field_id>": {"value": "<section text>", "tier": "C2"}` to
`$OVERRIDES_FILE`.

A C2 label matching no section is treated the same as `INSUFFICIENT_RESEARCH`
— never invent a fourth theme, never fall back to a C1 draft. A required C2
question that cannot be resolved this way parks the role; that is correct
behavior, not a bug to work around.

## Step 4b — resolve money (M) questions: JD first, config fallback

**If Step 2c found no M question, skip this step entirely.** This also
covers the case where `src/apply/answers.py`'s `_resolve_parsed_salary`
already filled the field from this job's own parsed compensation columns
(`clean.parquet`'s `salary_min`/`salary_currency`, times the vertical's
`salary_expectation.markup_pct`, computed in `apply_cli.build()`) — that
path is fully deterministic (R7), runs ahead of everything below, and
supersedes a static `rules:` default outright, so a field it resolved never
reaches Step 2c's scan in the first place. Everything from here on is the
fallback for a job with no usable parsed compensation (non-USD, unparsed, or
no `salary_expectation` configured for `$VERTICAL`).

For each M question that came from `"unmapped"`/`"draftable"` (genuinely
unresolved — no Tier B rule matched it), `Read` `${OUT_DIR}/jd_snapshot.md`
(already on disk from `/tailor`) for a stated figure or range — a number
near words like "salary", "compensation", "pay range", "OTE". This is a
judgment call over free text, so it stays here, not in `src/` (R7). If the
JD states one, add it as a `"JD"`-tagged override same as any other Tier C
answer (the option-matching a select-kind field needs is already handled
generically by `build_plan()`, same safety net C1/C2 already rely on).

For an M question that came from `"fields"` (Tier B already resolved it):
**only override it if `"kind"` is `"text"` or `"textarea"`.** A Tier B match
on a `select`/`react_select`/checkbox-kind field has no options listed in
this pool (`"fields"` entries carry no `"options"` list, unlike
`unmapped`/`draftable`), so there is no safe way to confirm a JD-derived
value matches what the widget actually offers — writing one anyway risks
parking a field that was already safely answered, turning a submittable
role into a blocked one over a cosmetic salary-figure preference. Leave a
non-text-kind Tier B field exactly as it resolved; the JD check only
applies where it can't make things worse.

Before computing a value, check whether `$VERTICAL`'s own block in
`profile/verticals.yaml` has an optional `salary_expectation` key (`Read` the
file; most verticals won't have one — that's the normal case, not an error).
This key is lane-specific policy read directly from that file, not through
`src/verticals.py`'s typed loader, and this is the only place in `/apply`
that reads it — no vertical name is ever hardcoded here, the lookup is always
by `$VERTICAL`.

- **`salary_expectation` present, JD states a figure/range:** add
  `"<field_id>": {"value": "<low end of the range × its markup_pct, as a plain
  number>", "tier": "JD"}` — computed, not the JD's own wording verbatim.
- **`salary_expectation` present, JD states nothing:** add
  `"<field_id>": {"value": "<its fallback figure>", "tier": "JD"}` — this is
  the one case where an M question resolves even without a JD figure to
  start from.
- **`salary_expectation` absent for this vertical:** fall back to the
  original behavior below — JD figure verbatim, or no override at all.

If the JD states a figure for an eligible (text/textarea) field and no
`salary_expectation` key is configured for `$VERTICAL`: add
`"<field_id>": {"value": "<the figure, as stated>", "tier": "JD"}` to
`$OVERRIDES_FILE`. This tag is the one exception that supersedes a static
Tier B `rules:` match (e.g. the template's own `match: [salary,
compensation, ...]` default) — a figure the JD itself states should win
over a generic configured fallback.

If the JD states nothing and no `salary_expectation` key is configured for
`$VERTICAL`: add no override at all. Whatever already resolves the field —
the user's own Tier B rule, or a Tier C draft/park if they haven't set one —
proceeds exactly as it would without this step. The config-driven fallback
lives entirely in the user's own `profile/application_answers.yaml` (`rules:`
— see `application_answers.example.yaml`'s own `salary`/`compensation` entry
for the pattern); never hardcode a number here.

## Step 4c — resolve work-authorization (B0) wording variants

**Scope, narrow on purpose:** only `"unmapped"` entries with `"tier" ==
"B0"` whose `"reason"` contains `status_option_candidates`. That reason
means `src/apply/answers.py` already derived the fact (`status` ->
`authorized_now`/`requires_sponsorship`) but no pre-configured candidate
string matched this board's exact wording — a wording variant of
something already answered, not a real information gap.

Every other B0 park is a genuine gap or a deliberate opt-out — leave those
parked, untouched, no matter how confident a guess would be:
- unset `nationality` / `sponsorship_followup_text` / `status_label`
- `us_person_answer` or `scope_qualified_answer` left at `"park"`
  (the user's own choice to hand the question back)
- `"names_other_country"` / `"alternation"`-shaped reasons (the plan JSON
  never carries the category name, but the reason text says so plainly)

For each in-scope entry: `Read` its `"label"` and `"options"` (every option
is a full sentence, not a bare Yes/No — that's why it reached B0 at all),
and the plan JSON's `"work_authorization"` block (`status`,
`authorized_now`, `requires_sponsorship`, `us_person_answer`,
`nationality`, `status_label`, `status_option_candidates`,
`scope_qualified_answer` — present unconditionally, one `Read` covers every
in-scope entry this run).

Judge which option's FULL sentence is true, checking every claim it makes
against those facts — never the leading word alone. Two boards can use
"Yes" for opposite legal facts (one board's "Yes" means "will need
sponsorship", another's "Yes" means "will not need it") — reading only the
leading word gets one of them backwards; reading the whole sentence against
`requires_sponsorship`/`authorized_now` resolves both correctly.

- Exactly one option's every claim holds → add
  `"<field_id>": {"value": "<that option's exact text>", "tier": "B0-LLM"}`
  to `$OVERRIDES_FILE`.
- Zero options hold, or more than one could plausibly hold → leave it
  parked. Never guess.
- An option's claim depends on a fact the block above has no key for (e.g.
  a J-1 visa's two-year home-residency requirement) → leave it parked —
  that is the real information gap this scope exists to distinguish from a
  wording variant.

`"B0-LLM"` is `build_plan()`'s one exception for a Tier B0 park (the same
shape as `"JD"` for Tier B) — tagged distinctly from `"C1"`/`"C2"`/`"JD"`
because it is a legal-status claim resolved by judgment against known
facts, not drafted prose, and worth keeping spot-checkable rather than
invisible.

## Step 4d — audit Tier B/B0 resolutions flagged in Step 2c

**If Step 2c flagged nothing, skip this step entirely.**

For each flagged field, draft the FULL answer: keep whatever part its
existing `"value"` already got right, and add whatever the label separately
asks for — from `profile/bullets.md` (C1-shaped) or `$COMPANY_ANSWERS`
(C2-shaped), same discipline as Steps 4/5. Add `"<field_id>": {"value":
"<full corrected answer>", "tier": "AUDIT"}` to `$OVERRIDES_FILE`.

## Step 5 — resolve C1: draft, and maybe write back a reusable rule

For each C1 question, draft an answer under NO-FAB / REPHRASE-LICENSE (same
discipline as `/tailor`): every claim about your experience traces to a
specific `profile/bullets.md` bullet's canonical text or its
`allowable_synonyms`. No invented tools, metrics, scopes, or dates. Keep it
short — these are form fields, not letter paragraphs; 1-3 sentences. If the
question carries a `"description"`, follow it (e.g. a length cap or a
"don't use AI" instruction) same as the label itself.

Add `"<field_id>": {"value": "<drafted text>", "tier": "C1"}` to
`$OVERRIDES_FILE` for every one, keeping the existing `job_id` key, regardless of what happens next.

**Then, separately, decide whether the question itself — not the drafted
prose — is a reusable fact worth a permanent Tier B rule.** This is a much
narrower question than "did I answer it": a rule is only for a question whose
*answer is a stable fact independent of company* ("years of experience with
Python", "willing to relocate", "do you hold a valid driver's license") that
slipped through only because no existing rule's `match:` covers its exact
wording. **Never** write back a rule for a question that needed drafted
prose to answer, or one that is subtly company-flavored despite passing as
C1 — when in doubt, skip the writeback and keep only the per-run override
(§15: "Never auto-append a rule whose answer came from Tier C1 drafting").

If, and only if, you judge a specific question worth it:

```bash
cd "$(git rev-parse --show-toplevel)"
cp profile/application_answers.yaml /tmp/apply_$1_answers_yaml.bak
```

`Edit` `profile/application_answers.yaml`: append ONE new entry under
`rules:` (never touch an existing entry), narrow `match:` keywords drawn from
the question's own label, a short factual `answer:`, and a trailing comment
naming its source:

```yaml
  - match: [<narrow keywords from the label>]
    answer: "<short factual answer>"
    # added by /apply from: "<verbatim question label>"
```

Then re-validate immediately:

```bash
cd "$(git rev-parse --show-toplevel)"
uv run python -c "
from src.apply.answers import load_answers, AnswersError
try:
    load_answers()
    print('OK')
except AnswersError as exc:
    print(f'INVALID: {exc}')
"
```

If it prints anything other than `OK` (overlap with an existing rule, a
work-authorization keyword, or any other validation failure): **revert the
append immediately**

```bash
cp /tmp/apply_$1_answers_yaml.bak profile/application_answers.yaml
```

and rely on the per-run override alone for this run — do not leave the file
in a state that blocks every future run over one bad append.

## Step 5b — no_ai_slop editing pass (before re-plan)

Run the `no_ai_slop` skill in **edit** mode over every C1, C2 and AUDIT value
this run wrote into `$OVERRIDES_FILE` (skip `"JD"`- and `"B0-LLM"`-tagged
entries — a bare figure or a board's own option text verbatim, neither
drafted prose) — the same deep pass `/cover-letter` Step 4 runs over its
drafted paragraphs, for the same structural AI-tells the banned-phrase
linter can't catch (binary contrasts, colon reveals, importance puffery,
robotic rhythm).

This is a voice/structure edit, NOT a rewrite of substance — no new claim,
tool, metric, scope, or date beyond what `profile/bullets.md` (C1) or
`company_answers.md` (C2) already attests. Take the edited text and write it
back into the same `$OVERRIDES_FILE` entries, keeping their `tier` and the
`job_id` key unchanged.

## Step 5c — lint pass over the same values

**Skip if Step 5b had no entries to edit.** `/tailor`, `/cover-letter` and
`company-answers.md` all run `src/lint.py` over their own drafted prose;
`/apply`'s C1/C2/AUDIT drafts are the same kind of fresh-generated text and
need the same pass — otherwise a mechanical artifact (an em dash, a smart
quote) rides straight into `answers_override.json` unfixed.

```bash
cd "$(git rev-parse --show-toplevel)"
uv run python <<PYEOF
import json
from pathlib import Path
from src.lint import fix_mechanical, find_phrase_violations, load_de_ai_rules

path = Path("$OVERRIDES_FILE")
content = json.loads(path.read_text())
rules = load_de_ai_rules()

lintable = [k for k, v in content.items()
            if k != "job_id" and isinstance(v, dict) and v.get("tier") in ("C1", "C2", "AUDIT")]

all_subs, all_violations = [], []
for field_id in lintable:
    text = content[field_id]["value"]
    fixed, subs = fix_mechanical(text, rules)
    content[field_id]["value"] = fixed
    all_subs.extend({**s, "field": field_id} for s in subs)
    for v in find_phrase_violations(fixed, context="resume", exempt_lines=None, rules=rules):
        all_violations.append({**v, "field": field_id})

path.write_text(json.dumps(content, indent=2))
print(json.dumps({
    "mechanical_subs": len(all_subs),
    "violations": all_violations,
}, indent=2, default=str))
PYEOF
```

If `violations` is non-empty: follow `.claude/shared/lint_loop.md` — rewrite
the flagged field's value under NO-FAB, re-run this block, at most 5
attempts, hard-refuse (leave the role parked, do not proceed to Step 6) if
violations remain after that.

## Step 6 — re-plan and confirm the overrides landed

```bash
cd "$(git rev-parse --show-toplevel)"
uv run apply plan "$1" --json --answers "$OVERRIDES_FILE" > /tmp/apply_$1_plan2.json
```

Compare `"unmapped"` between the two plan JSONs. Every required question this
command resolved (Step 4, 4b, 4c or 5) must be gone from the second one. If any
remain — the override didn't land on the field id it was meant for, most
likely — stop and report which ones, rather than proceeding to a browser with
a plan that still doesn't match what was decided above.

An AUDIT correction (Step 4d) replaces an already-filled field, not a parked
one, so it never shows up in this diff. Instead, confirm each flagged field's
`"value"` in the second plan JSON's `"fields"` list is the corrected answer,
not the original one.

If `"parked"` is still `true` (some Tier C question genuinely could not be
resolved, or it's a non-Tier-C park this command has no business touching),
report the remaining unmapped questions verbatim and stop. Do not open a
browser for a role that will not submit.

## Step 6b — user confirmation of the drafted answers (required)

Step 6 is this command checking its own work. This step is the user checking
it. Nothing drafted here goes out under their name unconfirmed.

Show the user, in the conversation, every `"C1"`, `"C2"`, `"AUDIT"`, `"JD"`
and `"B0-LLM"` entry this run wrote into `$OVERRIDES_FILE` — the question
label, the field id, the tier, and the **full value verbatim**, never a
summary or a truncation:

```
<field_id> [<tier>] — "<question label>"
<the value, in full>
```

Then ask for explicit confirmation to proceed, and **stop and wait for the
user's reply**.

- User confirms → continue to Step 7.
- User asks for a change → `Edit` `$OVERRIDES_FILE`, re-run Steps 5b, 5c and
  6, then show the revised values and ask again.
- User declines, or does not reply → stop. Report what was drafted and where
  `$OVERRIDES_FILE` sits. Do not run Step 7 with `--submit`.

If `$OVERRIDES_FILE` holds no drafted entries this run (nothing but the
`job_id` key), there is nothing to confirm — say so and continue to Step 7.

## Step 7 — fill (and submit, if asked)

```bash
cd "$(git rev-parse --show-toplevel)"
SUBMIT_FLAG=""
# if --submit was in $ARGUMENTS:
# SUBMIT_FLAG="--submit"
uv run apply run --job-id "$1" --answers "$OVERRIDES_FILE" --yes $SUBMIT_FLAG
```

Only run this step once Step 6b's confirmation is in hand. `--yes` stands in
for the CLI's own submit prompt, which this session (no tty) could not answer.

Without `--submit` this fills the form and stops — the browser stays open for
review per `apply run`'s existing behavior. **A no-submit run is not a dry
run: filling UPLOADS the resume and the cover letter to the ATS**
(`src/apply/fill.py` attaches every planned file before touching any field),
so the documents have already left the machine before any submit decision is
made. The same holds for `uv run apply fill`. Say this to the user when they
ask for a fill-only run.

With `--submit`, a successful click transitions the role to `applied` through
`/track` automatically (R10; `apply_cli` never touches `state.yaml` itself).
The submission bounds in Step 1 apply. Either way a run report lands in
`applications/apply_runs/<timestamp>.md` (§10) — read it back and surface its
category verbatim. The report's nine categories, in `src/apply_cli.py`'s own
order:

| category | what it means |
|---|---|
| `submitted` | clicked, confirmed, and transitioned to `applied` |
| `submitted_unconfirmed` | clicked, but no confirmation seen — tell the user to verify by hand |
| `submitted_untracked` | clicked, but `state.yaml` was NOT updated |
| `parked` | something stayed unresolved; nothing was submitted |
| `ready` | every field resolved; rerun with `--submit` to click |
| `manual` | no submit path for this board — apply by hand (not a failure) |
| `skipped` | another role at this company was already submitted this run (one submission per company per run) |
| `failed` | the fill or the submit guard refused |
| `expired` | the posting is gone |

`submitted_untracked` is reported **first and unmissably**, under the run
report's own banner text — `SUBMITTED BUT NOT TRACKED — fix state.yaml by
hand`. An unreported one means the role is still eligible next run and gets a
duplicate application. `submitted_untracked` and `failed` are the two
categories that exit non-zero.

## Step 8 — report

Tell the user:
```
$JOB_ID — <category from the run report>

<the report's detail line for this role, verbatim>

Command: uv run apply run --job-id <job_id> --answers <OUT_DIR>/answers_override.json --yes [--submit]

Overrides applied this run: <list of field ids resolved at C1/C2/JD/B0-LLM/AUDIT, with tier>
<if Step 2c called a Skill this run: "Also ran: company-answers|cover-letter">
<if a Tier B rule was written back: "Also added a reusable rule to
profile/application_answers.yaml for: \"<label>\"">
```

`Command:` is Step 7's invocation with every variable substituted — it must
paste into a terminal and run. On `ready` or `parked`, add a rerun line: same
command, `--submit` only for `ready`, and drop `--yes` (the user has a tty).

If the category is `submitted_untracked`, lead with the report's banner —
`SUBMITTED BUT NOT TRACKED — fix state.yaml by hand` — and name the job_id.
If it is `submitted_unconfirmed`, tell the user to verify on the board.

The overrides file at `$OVERRIDES_FILE` (`${OUT_DIR}/answers_override.json`)
stays on disk after this run — an audit trail of what was submitted and why,
next to the role's other artifacts. Each run overwrites it fresh (Step 1),
so it always reflects only the most recent run's decisions.
