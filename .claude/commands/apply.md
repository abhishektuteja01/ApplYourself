---
description: Resolve the remaining Tier C application questions for one tailored role and submit it (or fill and stop). Classifies each unanswered question as generic (drafted from bullets.md) or company-specific (resolved from that role's company_answers.md), then hands a per-run answers override to the deterministic apply CLI. Never writes profile/application_answers.yaml except through the append-only Tier B writeback path.
model: sonnet
effort: medium
allowed-tools:
  - Bash
  - Read
  - Edit
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

Both land in a **per-run answers override** (`/tmp/apply_$1_answers.json`) —
never in `profile/application_answers.yaml`. That file is global; a
company's "why us" answer or a one-off drafted sentence must never leak into
the next role's run. The one exception, a separate and much narrower path,
is §15's Tier B writeback (Step 5 below) — and that only ever carries a
reusable *fact*, never drafted prose.

---

**Before anything else, read `.claude/shared/no_fab.md`.** This command
cites NO-FAB and REPHRASE-LICENSE by name for the C1 path.

## Step 1 — prerequisites (one block, fail loud)

```bash
cd "$(git rev-parse --show-toplevel)" || { echo "ERROR: not inside the repo."; exit 1; }
JOB_ID="$1"
test -n "$JOB_ID" || { echo "ERROR: /apply requires a job_id argument."; exit 1; }
test -f "pipeline/$JOB_ID/state.yaml" || { echo "ERROR: pipeline/$JOB_ID/state.yaml missing."; exit 1; }
test -f profile/application_answers.yaml || {
    echo "ERROR: profile/application_answers.yaml missing. Copy profile/application_answers.example.yaml and fill it in."
    exit 1
}
uv run python -c "import playwright" 2>/dev/null || {
    echo "ERROR: playwright not installed. Run: uv sync --group apply && PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 uv run playwright install chrome"
    exit 1
}

STATE=$(uv run python -c "
import yaml
d = yaml.safe_load(open('pipeline/$JOB_ID/state.yaml')) or {}
print(d.get('state', ''))
")
test "$STATE" = "tailored" || {
    echo "ERROR: pipeline/$JOB_ID/state.yaml has state '$STATE', not 'tailored'. Run /tailor and /cover-letter first."
    exit 1
}

COVER_DIR=$(uv run python -c "
import yaml
d = yaml.safe_load(open('pipeline/$JOB_ID/state.yaml')) or {}
letters = d.get('cover_letters') or []
print(letters[-1] if letters else '')
")
test -n "$COVER_DIR" || {
    echo "ERROR: pipeline/$JOB_ID/state.yaml has no cover_letters[]. Run /cover-letter first -- /apply's Tier C2 questions resolve from its company_answers.md output."
    exit 1
}
COMPANY_ANSWERS="applications/${COVER_DIR}/company_answers.md"
test -f "$COMPANY_ANSWERS" || {
    echo "ERROR: ${COMPANY_ANSWERS} missing. Re-run /cover-letter -- it should have written this alongside the letter (§7b)."
    exit 1
}

OVERRIDES_FILE="/tmp/apply_${JOB_ID}_answers.json"
echo '{}' > "$OVERRIDES_FILE"

echo "job_id=$JOB_ID"
echo "company_answers=$COMPANY_ANSWERS"
echo "overrides_file=$OVERRIDES_FILE"
```

If ANY check fails, exit immediately. **No partial work.**

Parse `$ARGUMENTS` for an optional `--submit` flag. Its absence means
fill-and-stop for this role — same default as `uv run apply run` (§13: never
a config default, always per invocation).

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
- Exit `0` → read `/tmp/apply_$1_plan.json`. If `"parked"` is `false` **and**
  `"draftable"` is empty, there is nothing for this command to do — skip to
  Step 6 with no overrides file.

## Step 3 — classify every Tier C question

Two pools of Tier C questions, both from the JSON's `"unmapped"` (filter
`"tier" == "C"`) and `"draftable"` lists — the required-and-parked ones and
the optional-and-blank ones. `Read` each one's `"label"` (and `"options"` if
present).

Classify each conservatively:

- **C2** if the label names the company (`"company"` field in the plan JSON),
  its product, or asks for motivation/opinion — "why us", "why do you want to
  work here", "what excites you about", "what do you find interesting about
  our [product/mission/culture]", "why this role". **Ambiguous → C2.**
- **C1** otherwise — a generic, factual, or experience question a person
  answers the same way regardless of employer ("describe your experience
  with X", "how many years have you worked with Y", "what's your
  availability").

## Step 4 — resolve C2 from `company_answers.md`

`Read` `$COMPANY_ANSWERS` (printed in Step 1). It has exactly three sections:
`why_company`, `why_role`, `what_interests_you_about_product`.

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

## Step 5 — resolve C1: draft, and maybe write back a reusable rule

For each C1 question, draft an answer under NO-FAB / REPHRASE-LICENSE (same
discipline as `/tailor`): every claim about your experience traces to a
specific `profile/bullets.md` bullet's canonical text or its
`allowable_synonyms`. No invented tools, metrics, scopes, or dates. Keep it
short — these are form fields, not letter paragraphs; 1-3 sentences.

Add `"<field_id>": {"value": "<drafted text>", "tier": "C1"}` to
`$OVERRIDES_FILE` for every one, regardless of what happens next.

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

## Step 6 — re-plan and confirm the overrides landed

```bash
cd "$(git rev-parse --show-toplevel)"
uv run apply plan "$1" --json --answers "$OVERRIDES_FILE" > /tmp/apply_$1_plan2.json
```

Compare `"unmapped"` between the two plan JSONs. Every required question this
command resolved (Step 4 or Step 5) must be gone from the second one. If any
remain — the override didn't land on the field id it was meant for, most
likely — stop and report which ones, rather than proceeding to a browser with
a plan that still doesn't match what was decided above.

If `"parked"` is still `true` (some Tier C question genuinely could not be
resolved, or it's a non-Tier-C park this command has no business touching),
report the remaining unmapped questions verbatim and stop. Do not open a
browser for a role that will not submit.

## Step 7 — fill (and submit, if asked)

```bash
cd "$(git rev-parse --show-toplevel)"
SUBMIT_FLAG=""
# if --submit was in $ARGUMENTS:
# SUBMIT_FLAG="--submit"
uv run apply run --job-id "$1" --answers "$OVERRIDES_FILE" $SUBMIT_FLAG
```

Without `--submit` this fills the form and stops — the browser stays open for
review per `apply run`'s existing behavior. With `--submit`, a successful
click transitions the role to `applied` through `/track` automatically (R10;
`apply_cli` never touches `state.yaml` itself). Either way a run report lands
in `applications/apply_runs/<timestamp>.md` (§10) — read it back and surface
its category (`submitted`/`parked`/`ready`/`failed`) to the user verbatim.

## Step 8 — report

Tell the user:
```
$JOB_ID — <category from the run report>

<the report's detail line for this role, verbatim>

Overrides applied this run: <list of field ids resolved at C1/C2, with tier>
<if a Tier B rule was written back: "Also added a reusable rule to
profile/application_answers.yaml for: \"<label>\"">
```

The overrides file at `/tmp/apply_$1_answers.json` is per-job and disposable
— nothing depends on it surviving past this run.
