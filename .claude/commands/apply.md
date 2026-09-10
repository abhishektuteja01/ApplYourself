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

A sixth case is the one that parks the most roles: a question the board
renders as a **choice**, where nothing configured equals any option it
offers. A Tier B rule matched the label and its candidates missed; or no rule
matched at all and the question is Tier C with an option list. Either way the
employer already wrote every permissible answer, so the resolution is to pick
the true one rather than to draft anything — tagged `"PICK"` (Step 4e), which
supersedes a Tier B or Tier C park and nothing else.

All six land in a **per-run answers override**
(`${OUT_DIR}/answers_override.json`, next to the role's other artifacts) —
never in `profile/application_answers.yaml`. That file is per-role; a
company's "why us" answer or a one-off drafted sentence must never leak into
another role's run. Anything worth keeping past
this role goes to the agent-owned learned store instead (Step 7c) — a board
wording, an option spelling, or a veto, never drafted prose. Nothing here
edits `profile/application_answers.yaml`.

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
cd "$(git rev-parse --show-toplevel)" && uv run apply plan "$1" --questions
```

- Exit `2` → the posting expired. Report `EXPIRED: <message>` to the user
  and stop. This is an ordinary outcome (§14: 6/45 live postings), not a bug
  — suggest `/track $1 skip` and stop here.
- Exit `1` → a config or fetch error. Report the message verbatim and stop.
  Do not attempt to work around it.
The `--questions` view is the same plan grouped by who answers each question
rather than by what `fill.py` does with it: **FACTS** (answered from config,
each showing the rule keyword that answered it and the options it was chosen
from), **QUESTIONS** (need judgment), **BLOCKING** (required and unanswered,
with the reason and the offered options). Read it first — it is the fastest
way to see a wrong answer, which the JSON shows only if you compare `value`
against `options` field by field. The JSON stays the source for every
field id you write into `$OVERRIDES_FILE`.

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
`"value"`/`"source"`/`"options"` for Step 4d. `"source"` is the rule keyword
that answered it and `"options"` is what the widget offered — a value that
matched on one keyword of a long label, or that is one of several offered
options, is exactly the shape that flags. This is not about re-litigating an answer that is
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

Every field this step reads was already answered. A Tier B answer is a
keyword match against prose the employer wrote, and `"source"` names the one
keyword that matched — so the audit is: does that keyword's answer actually
answer *this* label? Three failure shapes, all seen live:

- **Half-answered.** The label asks two things and the value covers one. Seen
  live: `"We encourage employees to bring their whole selves to work — share
  your preferred name and pronouns"` matched on `preferred name` and answered
  with the name alone, saying nothing about pronouns.
- **Backwards.** The keyword is in the label but the value states the
  opposite of what was asked. A rule for an AI-policy acknowledgment
  (`source: "ai policy"`, answer `"Yes"`) also matches *"Did you use AI to
  prepare this application?"* — where `"Yes"` is a claim the user never made.
  Check the polarity against the label every time, never the keyword.
- **Wrong of several offered.** `"options"` now reaches the plan JSON for
  filled fields too. Where a value was picked from a list, confirm it is the
  right one of those options and not merely a matching string.

For each flagged field, draft the FULL answer: keep whatever part its
existing `"value"` already got right, and add whatever the label separately
asks for — from `profile/bullets.md` (C1-shaped) or `$COMPANY_ANSWERS`
(C2-shaped), same discipline as Steps 4/5. Add `"<field_id>": {"value":
"<full corrected answer>", "tier": "AUDIT"}` to `$OVERRIDES_FILE`.

Where the correction is simply a different one of the board's own options,
use `"tier": "PICK"` instead and keep `"AUDIT"` for a rewritten answer — the
two tags read differently in the run report, and the distinction is what
makes a wrong pick traceable later.

## Step 4e — resolve a parked choice from the options the board offers

**Scope:** `"unmapped"` entries with `"tier"` `"B"` or `"C"` that carry a
non-empty `"options"` list. Nothing else. A Tier B0 entry is Step 4c's, and a
Tier A or A2 park is neither step's — `build_plan()` will drop the override
and name it in `"ignored_overrides"`.

This is the "park only as a last resort" step. Measured over the 237-board
harvest, 189 required parks are option-bearing questions of exactly this
shape: the board wrote every permissible answer itself, and the role parked
because no configured string equalled one of them.

A Tier B entry here means a rule matched the label — `"source"` names the
keyword — and then offered nothing the widget takes. The board's own option
text is the answer; `"source"` is also the group a learned option spelling
belongs to (Step 7c).

For each in-scope entry, resolve it **only** from something already on disk.
The categories that came out of the harvest, and what each may draw on:

| the question is | answer from | example |
|---|---|---|
| a fact about where you live or your history | `application_answers.yaml`, `profile/preferences.md` | "Do you currently live in San Francisco?" |
| a claim about your experience | `profile/bullets.md` / `skills_master.md`, under NO-FAB | "Have you used Python professionally?" |
| a known value that needs bucketing | the same file the value came from | "How many years... 0-2 / 2-5 / 5+" |
| an instruction to follow | the label and `"description"` | "select the SECOND option" |
| an acknowledgment with one permissible answer | the option itself | "GDPR Disclosure" -> "Acknowledge/Confirm" |

And the three that **stay parked**, however obvious a guess looks:

- **A consent decision** — "we may use AI notetakers: Yes, I consent / No, I
  do not consent". Consent is the user's to give, and no file on disk states
  it. Park it and say so; if they want it answered every time, that is a
  named key in `profile/preferences.md`, not a judgment here.
- **A preference between the employer's own alternatives** — "which office
  location do you prefer, Atlanta or Houston?" Park unless
  `profile/preferences.md` settles it.
- **Nothing offered is true** — "Active Security Clearance(s)" listing eight
  clearance levels and no "None". Picking the least-false option would state
  a qualification the user does not hold. This park is correct behavior.

Resolve an in-scope entry to `"<field_id>": {"value": "<the board's exact
option text>", "tier": "PICK"}` in `$OVERRIDES_FILE`. The value must be one
of the strings in that entry's `"options"`, verbatim — `build_plan()` re-checks
it against the widget and parks the field again if it is not, so an
approximation fails loudly rather than quietly.

`"PICK"` is deliberately separate from `"AUDIT"`: it only ever names an
option the employer wrote, and it only ever reaches a question nothing else
could answer. Multi-select entries (`"multi": true`) take a list of option
strings; pick every one that is true and no more.

## Step 5 — resolve C1: draft from what the profile attests

Two sources, and the question decides which. `profile/bullets.md` attests what
you built; `profile/stories.md` attests how you decided. A question asking for
a decision, a mistake, a disagreement or the hardest thing you have done is
answerable only from `stories.md` — `bullets.md` has no such claim in it, and
drafting one from a bullet is fabrication.

**Read first:** `profile/stories.md`, the `C1 story priority` line in
`profile/verticals/${VERTICAL}/tailoring.md`, and `${OUT_DIR}/keywords_to_mirror.md`.
If `profile/stories.md` does not exist, skip straight to the bullets-only path
below; it is an optional profile file.

**Route each C1 question:**

1. Classify the label to a `kind` — `bug_caught`, `judgment_call`, `reversal`,
   `negative_result`, `hardest`, `conflict`, `failure`. A label that maps to no
   kind takes the bullets-only path.
2. Collect every `stories.md` entry with that `kind`. If exactly one, use it.
3. If several, rank by the lane's `C1 story priority`, then by how many of the
   entry's `tags` and `allowable_synonyms` appear in `keywords_to_mirror.md`.
   A `kind` match always beats a higher-priority project whose `kind` does not
   match — priority breaks ties within a kind, it does not override the kind.
4. If no entry has that kind, take the bullets-only path and say so in the
   Step 7 report rather than reaching for a story of a different kind.

**Drafting, either path.** NO-FAB / REPHRASE-LICENSE, same discipline as
`/tailor`. From a story: every claim traces to that entry's
`situation`/`action`/`outcome`/`retrospect` text or its `allowable_synonyms`.
From a bullet: to that bullet's canonical text or its `allowable_synonyms`. A
bullet's `evidence:` line is attestation for the user and licenses nothing —
never draft from it. No invented tools, metrics, scopes, or dates, and no
mixing two stories into one answer. Keep it short — these are form fields, not
letter paragraphs; 1-3 sentences. If the question carries a `"description"`,
follow it (e.g. a length cap or a "don't use AI" instruction) same as the label
itself.

Add `"<field_id>": {"value": "<drafted text>", "tier": "C1"}` to
`$OVERRIDES_FILE` for every one, keeping the existing `job_id` key, regardless of what happens next.
**Also add `"source_id"`** to each C1 entry — the `S-` id or `B-` id the answer
drafted from, so a run is auditable after the fact. It is a record, not a tier.

**Then, separately, note whether the question itself — not the drafted prose
— is a reusable fact.** If it is, Step 7c records it in the learned store;
nothing is written here. This step's only output is `$OVERRIDES_FILE`.

Nothing in this command ever edits `profile/application_answers.yaml`. That
file holds what the user stated — their identity, their work-authorization
status, their real preferences — and it is the file a person reads when an
answer looks wrong. Everything learned goes to `profile/.apply_learned.jsonl`
instead (Step 7c), which is append-only, validated against the same loader,
and deletable one line at a time.

## Step 5b — no_ai_slop editing pass (before re-plan)

Run the `no_ai_slop` skill in **edit** mode over every C1, C2 and AUDIT value
this run wrote into `$OVERRIDES_FILE` (skip `"JD"`-, `"B0-LLM"`- and
`"PICK"`-tagged entries — a bare figure or a board's own option text
verbatim, neither drafted prose) — the same deep pass `/cover-letter` Step 4 runs over its
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

**Then check `"ignored_overrides"` in the second plan JSON.** A non-empty list
means an entry was written into `$OVERRIDES_FILE`, validated, and then dropped
because its tier cannot supersede that field's resolution tier — a `"PICK"`
aimed at a Tier B0 work-authorization field, or any override aimed at Tier A
or A2. The plan looks correct in every other respect, so nothing else in this
step catches it. Report each one and do not treat that question as resolved.

If `"parked"` is still `true` (some Tier C question genuinely could not be
resolved, or it's a non-Tier-C park this command has no business touching),
report the remaining unmapped questions verbatim and stop. Do not open a
browser for a role that will not submit.

## Step 6b — user confirmation of the drafted answers (required)

Step 6 is this command checking its own work. This step is the user checking
it. Nothing drafted here goes out under their name unconfirmed.

Show the user, in the conversation, every `"C1"`, `"C2"`, `"AUDIT"`, `"JD"`,
`"B0-LLM"` and `"PICK"` entry this run wrote into `$OVERRIDES_FILE` — the question
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

## Step 7 — fill, check the finished form, then submit

Everything before this step reasoned about the form from its *schema*. This
step is the first time anyone sees the form itself, and two classes of problem
exist only here:

- A value the page holds that nobody planned — a board default, or its own
  resume parser writing into a field after the upload. On a **parked**
  question that means an answer is already sitting there under the user's
  name, and until the manifest existed nothing ever read those fields back.
- A select whose real option list exists nowhere but the DOM (Greenhouse's
  `candidate-location`, Ashby's comboboxes, Lever's location box). At plan
  time `"options"` is empty for these and any value is accepted blind.

### 7a — fill and read the form back

```bash
cd "$(git rev-parse --show-toplevel)"
MANIFEST="${OUT_DIR}/form_manifest.json"
uv run apply run --job-id "$1" --answers "$OVERRIDES_FILE" --yes --manifest "$MANIFEST"
```

No `--submit`: this fills and stops. **A no-submit run is not a dry run —
filling UPLOADS the resume and the cover letter to the ATS**
(`src/apply/fill.py` attaches every planned file before touching any field),
so the documents have already left the machine before any submit decision is
made. Say that to the user whenever they ask for a fill-only run.

`Read` `$MANIFEST`. One row per question, with `planned` against `actual`,
plus `prefilled`, `invalid` (the browser's own `checkValidity()` verdict) and
`options` (preferring what the browser actually read). Check three things:

1. **`matches: false` on a `group: "filled"` row** — the page holds something
   other than what was planned. Nothing downstream catches this; the submit
   guard's own re-verify silently re-writes some kinds and skips others.
2. **A non-empty `actual` on a `group` of `"parked"`, `"left blank"` or
   `"skipped"`** — an unanswered question the page already has a value for.
   This is the case that sends a wrong answer while every other check passes.
   If that value is wrong, correct it; if it is right, still resolve the
   question explicitly (Step 4e) so the record says so.
3. **`options` on a row whose plan-time list was empty** — now that the real
   list is known, a question parked for want of it may be resolvable at Step
   4e after all.

For anything found, add or correct the entry in `$OVERRIDES_FILE` — same tiers
and same rules as Steps 4–5 (`PICK` for one of the board's own options,
`AUDIT` for a corrected answer). Then re-run Step 6, show the user the change
under Step 6b, and come back here. **Two rounds at most**: if the same field
still disagrees after a second fill, stop and report it — something is
rewriting that field and one more attempt will not settle it.

### 7b — submit

Only with `--submit` in `$ARGUMENTS`, and only once Step 6b's confirmation is
in hand:

```bash
cd "$(git rev-parse --show-toplevel)"
uv run apply run --job-id "$1" --answers "$OVERRIDES_FILE" --yes --submit \
  --manifest "${OUT_DIR}/form_manifest.submit.json"
```

This fills the form a second time and clicks. The second manifest is the
record of what was on screen at the moment of the click. `--yes` stands in for
the CLI's own submit prompt, which this session (no tty) could not answer.

Without `--submit` in `$ARGUMENTS`, stop after 7a and report `ready`.

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

## Step 7c — learn from this form

**After 7a (and 7b, if it ran), and only for what the user confirmed in Step
6b.** This is the step that makes the next form easier. It writes to
`profile/.apply_learned.jsonl` — the agent-owned store — and **never** to
`profile/application_answers.yaml`, which holds only what the user stated.

Run `uv run apply learn --list` first: it prints every rule group, its
keywords, its candidate answers, and what has already been learned into it.
Learn into an existing group wherever one fits. A new group is the last resort,
not the first move — the store's whole point is that the file grows with the
number of *questions*, not the number of boards.

Four record kinds, and the question decides which:

| what happened this run | record | example |
|---|---|---|
| a Tier B rule matched but no candidate was offered, and Step 4e picked the board's own option | `option`, into the group `"source"` named | `--kind option --group "how did you hear" --option "careers site" --mode contains` |
| a question Step 4e resolved that no rule matched at all, whose answer is a stable fact | `answer` | `--kind answer --wording "have you deployed production grade applications" --answer Yes` |
| a group's keyword matched a label it answers *wrongly*, and Step 4d corrected it | `veto` | `--kind veto --group "ai policy" --wording "did you use ai to prepare this application"` |
| a question a group already answers, worded in a way its keywords miss | `wording` | `--kind wording --group "how did you hear" --wording "where did you come across this role"` |

Always pass `--job-id "$1"` and `--board <the plan's board>` so the record
names the form that taught it.

**Prefer the generic string over the company-specific one.** An option like
`"<Company> Careers Site"` teaches one employer; `"careers site"` with
`--mode contains` teaches every board that prefixes its own name, and 83% of
labels in the 237-board harvest appear on exactly one board — a
company-specific record earns almost nothing.

**Never learn:**
- anything that needed drafted prose to answer (C1/C2). Those are per-role by
  construction and belong in `$OVERRIDES_FILE` alone.
- a company-specific answer of any kind, even one that passed as C1.
- a work-authorization fact. The loader rejects those keywords outright, and
  `apply learn` will refuse the record.
- a consent decision or a preference the user has not stated. If Step 4e
  parked one of those, the fix is a named key in `profile/preferences.md`,
  proposed to the user — not a learned record.

Run each record with `--dry-run` first and read what it prints: the group's
keywords and candidates *after* the record would be folded in. `apply learn`
validates by loading the entire answer config with the record in place, so an
overlap with an existing rule, a work-auth keyword or a too-short wording
fails with the loader's own message and nothing is written. If a record is
refused, report it and move on — a refused record is never worth working
around.

Show the user what was learned in the Step 8 report, one line per record.

## Step 8 — report

Tell the user:
```
$JOB_ID — <category from the run report>

<the report's detail line for this role, verbatim>

Command: uv run apply run --job-id <job_id> --answers <OUT_DIR>/answers_override.json --yes --manifest <OUT_DIR>/form_manifest.json [--submit]

Overrides applied this run: <list of field ids resolved at C1/C2/JD/B0-LLM/AUDIT/PICK, with tier>
<if Step 2c called a Skill this run: "Also ran: company-answers|cover-letter">
<if Step 7c learned anything: "Learned for next time:" then one line per
record — kind, group, and the wording or option, e.g.
"option -> group 'how did you hear': \"careers site\" (contains)">
```

`Command:` is Step 7's invocation with every variable substituted — it must
paste into a terminal and run. On `ready` or `parked`, add a rerun line: same
command, `--submit` only for `ready`, and drop `--yes` (the user has a tty).

If the category is `submitted_untracked`, lead with the report's banner —
`SUBMITTED BUT NOT TRACKED — fix state.yaml by hand` — and name the job_id.
If it is `submitted_unconfirmed`, tell the user to verify on the board.

`${OUT_DIR}/form_manifest.json` stays on disk too — the read-back of what the
form actually held, which is the only record of the page as opposed to the
plan. The overrides file at `$OVERRIDES_FILE` (`${OUT_DIR}/answers_override.json`)
stays on disk after this run — an audit trail of what was submitted and why,
next to the role's other artifacts. Each run overwrites it fresh (Step 1),
so it always reflects only the most recent run's decisions.
