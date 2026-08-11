---
description: Resolve the remaining Tier C application questions for one tailored role and submit it (or fill and stop). Classifies each unanswered question as generic (drafted from bullets.md) or company-specific (resolved from that role's company_answers.md), then hands a per-run answers override to the deterministic apply CLI. Never writes profile/application_answers.yaml except through the append-only Tier B writeback path.
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

All three land in a **per-run answers override**
(`${OUT_DIR}/answers_override.json`, next to the role's other artifacts) —
never in `profile/application_answers.yaml`. That file is per-role; a
company's "why us" answer or a one-off drafted sentence must never leak into
another role's run. The one exception, a separate and much narrower path,
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
case "$STATE" in
    saved|tailored) ;;
    *)
        echo "ERROR: pipeline/$JOB_ID/state.yaml has state '$STATE', not 'saved' or 'tailored'. Run /tailor first."
        exit 1
        ;;
esac
# `saved` is now a valid entry state, not just `tailored` -- whether this
# role needs a cover letter before it can become `tailored` is exactly what
# Step 2b decides, per role, from the board's own form. It used to be a
# blanket prerequisite here; that pre-filtered roles the deterministic queue
# (`apply_cli.eligible_queue`) would otherwise happily submit.

TAILORED_DIR=$(uv run python -c "
import yaml
d = yaml.safe_load(open('pipeline/$JOB_ID/state.yaml')) or {}
dirs = d.get('tailored_dirs') or []
print(dirs[-1] if dirs else '')
")
test -n "$TAILORED_DIR" || {
    echo "ERROR: pipeline/$JOB_ID/state.yaml has no tailored_dirs[]. Run /tailor first."
    exit 1
}
OUT_DIR="applications/${TAILORED_DIR}"

COVER_DIR=$(uv run python -c "
import yaml
d = yaml.safe_load(open('pipeline/$JOB_ID/state.yaml')) or {}
letters = d.get('cover_letters') or []
print(letters[-1] if letters else '')
")
COMPANY_ANSWERS=""
if [ -n "$COVER_DIR" ]; then
    CANDIDATE="applications/${COVER_DIR}/company_answers.md"
    test -f "$CANDIDATE" && COMPANY_ANSWERS="$CANDIDATE"
fi
# A missing cover letter is NOT a hard prerequisite here. Step 2b re-checks
# it against this role's actual plan, by which point it's known whether any
# C2 question or required cover-letter upload genuinely exists.

OVERRIDES_FILE="${OUT_DIR}/answers_override.json"
# Persisted next to the role's other artifacts (company_answers.md,
# jd_snapshot.md) instead of a /tmp scratch file -- an audit trail of what
# was submitted and why. Overwritten fresh at the start of every run, so
# every key present by the end belongs to this run only.
# The job_id key binds this file to this role. Tier C2 answers are
# company-specific, so `apply --answers` refuses a file drafted for another
# role — but only if the key is here.
printf '{"job_id": "%s"}\n' "$JOB_ID" > "$OVERRIDES_FILE"

echo "job_id=$JOB_ID"
echo "state=$STATE"
echo "out_dir=$OUT_DIR"
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
- Exit `0` → read `/tmp/apply_$1_plan.json` and continue to Step 2c
  regardless of `"parked"`/`"draftable"` — even a role with nothing
  outstanding in Tier C still needs Step 2c's self-promotion check (a role
  sitting at `saved` with nothing left to resolve is exactly the "board
  never asked for a cover letter" case, and nothing else fires that
  transition for it). Step 2c's later steps (4, 4b, 5) are each individually
  skippable and become no-ops when their pool is empty.

## Step 2c — classify Tier C, gate on cover-letter need, self-promote

This is the single classification pass this command ever runs — the gate
below and Steps 4/4b/5's drafting all reuse its output, so `/apply` never
reaches C1/C2 drafting without having already confirmed, in this run, that
whatever it needs is either absent or already on disk.

Two pools of Tier C questions, both from the plan JSON's `"unmapped"` (filter
`"tier" == "C"`) and `"draftable"` lists — the required-and-parked ones and
the optional-and-blank ones. `Read` each one's `"label"` (and `"options"` if
present).

**Also scan `"fields"` (filter `"tier" == "B"`) for a salary/compensation-shaped
label.** A board with a static Tier B `rules:` match (the common case — see
the template's own `salary`/`compensation` entry) resolves that field
successfully, so it never appears in `unmapped`/`draftable` at all — skipping
this scan would make Step 4b's JD-first check silently never fire for
exactly the case it exists to handle. Note each such field's `"id"` and
`"kind"` for Step 4b.

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

Also check `"unmapped"` for a required entry with `"id" == "cover_letter"` —
a board whose file-upload field is required and still unresolved.

**The gate:** if there is any C2 question, OR a required `cover_letter`
entry, AND `$COMPANY_ANSWERS` is empty (Step 1 found no cover letter on
file): stop here, report `"$JOB_ID needs a cover letter -- run /cover-letter
for this role first"`, and leave state untouched. Do not draft a C2 answer
without `/cover-letter`'s own Step 2b research — that is exactly the
fabrication its Step 2b exists to prevent.

**Otherwise** — no cover letter is genuinely needed, or one already exists.
If `$STATE` is still `saved`, fire the transition right here, the same
`saved`-only guard `/cover-letter` Step 7b uses (R10; routed through
`/track`, never written directly):

```bash
cd "$(git rev-parse --show-toplevel)"
if [ "$STATE" = "saved" ]; then
    uv run track "$JOB_ID" tailored --note "resume on file; board needs no cover letter, or one already exists"
fi
```

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

**If Step 2c found no M question, skip this step entirely.**

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

If the JD states a figure for an eligible (text/textarea) field: add
`"<field_id>": {"value": "<the figure, as stated>", "tier": "JD"}` to
`$OVERRIDES_FILE`. This tag is the one exception that supersedes a static
Tier B `rules:` match (e.g. the template's own `match: [salary,
compensation, ...]` default) — a figure the JD itself states should win
over a generic configured fallback.

If the JD states nothing: add no override at all. Whatever already resolves
the field — the user's own Tier B rule, or a Tier C draft/park if they
haven't set one — proceeds exactly as it would without this step. The
config-driven fallback lives entirely in the user's own
`profile/application_answers.yaml` (`rules:` — see
`application_answers.example.yaml`'s own `salary`/`compensation` entry for
the pattern); never hardcode a number here.

## Step 5 — resolve C1: draft, and maybe write back a reusable rule

For each C1 question, draft an answer under NO-FAB / REPHRASE-LICENSE (same
discipline as `/tailor`): every claim about your experience traces to a
specific `profile/bullets.md` bullet's canonical text or its
`allowable_synonyms`. No invented tools, metrics, scopes, or dates. Keep it
short — these are form fields, not letter paragraphs; 1-3 sentences.

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

Run the `no_ai_slop` skill in **edit** mode over every C1 and C2 value this
run wrote into `$OVERRIDES_FILE` (skip `"JD"`-tagged entries — a bare figure,
not prose) — the same deep pass `/cover-letter` Step 4 runs over its drafted
paragraphs, for the same structural AI-tells the banned-phrase linter can't
catch (binary contrasts, colon reveals, importance puffery, robotic rhythm).

This is a voice/structure edit, NOT a rewrite of substance — no new claim,
tool, metric, scope, or date beyond what `profile/bullets.md` (C1) or
`company_answers.md` (C2) already attests. Take the edited text and write it
back into the same `$OVERRIDES_FILE` entries, keeping their `tier` and the
`job_id` key unchanged.

## Step 6 — re-plan and confirm the overrides landed

```bash
cd "$(git rev-parse --show-toplevel)"
uv run apply plan "$1" --json --answers "$OVERRIDES_FILE" > /tmp/apply_$1_plan2.json
```

Compare `"unmapped"` between the two plan JSONs. Every required question this
command resolved (Step 4, 4b or 5) must be gone from the second one. If any
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

Overrides applied this run: <list of field ids resolved at C1/C2/JD, with tier>
<if a Tier B rule was written back: "Also added a reusable rule to
profile/application_answers.yaml for: \"<label>\"">
```

The overrides file at `$OVERRIDES_FILE` (`${OUT_DIR}/answers_override.json`)
stays on disk after this run — an audit trail of what was submitted and why,
next to the role's other artifacts. Each run overwrites it fresh (Step 1),
so it always reflects only the most recent run's decisions.
