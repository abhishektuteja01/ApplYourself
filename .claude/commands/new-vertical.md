---
description: Add a job-search lane: lane.yaml, classifier rule, rubric, tailoring and resume shape. pass-a / pass-b split it in two. Config only.
model: opus
effort: medium
allowed-tools:
  - Bash(cd *)
  - Bash(git rev-parse *)
  - Bash(uv run verticals-check*)
  - Bash(uv run config *)
  - Bash(uv run python -c *)
  - Bash(uv run pytest tests/repo/test_real_config_drift.py *)
  - Bash(ls *)
  - Bash(cmp *)
  - Bash(echo *)
  - Bash(mkdir -p profile/verticals/*)
  - Bash(rm -f profile/verticals/*)
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - AskUserQuestion
argument-hint: <vertical_snake_case_name> [pass-a|pass-b] [one-line description]
---

# /new-vertical — add a lane (quick mode)

Verticals are data-driven: `profile/verticals.yaml` (`lanes:` order,
`defaults:`, `classifier_rules`) + `profile/verticals/<name>/lane.yaml` (the lane
block, including its `resume:` shape, merged over `defaults:`) +
`profile/verticals/<name>/{rubric.md, tailoring.md}` +
the lane's picks on the shared sources (`lanes:` in `profile/resume.yaml`,
`default_for` in `profile/bullets.md`, `show` in `profile/skills_master.md`)
are the whole registry. If you find yourself editing a `.py` file or another command
file, stop — the config contract is being violated.

Quick mode writes the loader's minimum and nothing more, with three questions.
Everything optional stays out of the file.

## Modes

Optional second token in `$ARGUMENTS`, spelled exactly `pass-a` or `pass-b`.

| token | writes | asks | verifies |
|---|---|---|---|
| *(none)* | all five artifacts, one pass | all three questions | full block |
| `pass-a` | the lane's `lane.yaml` + its `lanes:` entry + one `classifier_rules` entry + the marker | all three questions | loader + classifier only |
| `pass-b` | `rubric.md`, `tailoring.md`, the resume shape; deletes the marker | nothing | full block |

`pass-a` is enough to start a scrape; scoring needs `pass-b`.

Marker file: `profile/verticals/<name>/.pass_a_only`, written by `pass-a`,
deleted by `pass-b`.

**Out of scope here — this is `/tune-vertical`'s entire reason to exist. Do not
ask about it:** `skill_weights`, `title_variants`, the `defaults:` ban list,
classifier rule priority and collisions, rubric tier boundaries.

A lane never gets its own `title_include_terms`, `title_exclude_terms` or
`title_strong_keep_terms`: the title gate is lenient (a title passes when it
fits the lane's rule or one of its search terms and holds no word of the one
global ban list in `defaults:`), and level is the judge's call.

## Hard rules (binding)

- **No code edits.** `src/`, `tests/*.py` and other command files stay
  untouched. Never edit `tests/**/fixtures/verticals.yaml` — synthetic by
  design; a new lane needs no fixture change.
  `tests/repo/test_real_config_drift.py` is generic over whatever lanes
  `profile/verticals.yaml` holds, so it covers the new lane unedited. Lane-specific
  structural assertions are `/tune-vertical`'s.
- **NO-FAB** — read `.claude/shared/no_fab.md`. Every drafted term, skill and
  resume line traces to `profile/bullets.md`, `profile/skills_master.md` or the
  resume onboarding already parsed. Never invent a skill and never add a new
  `SKILL-*` entry or bullet — pass-b only adds this lane to existing entries. If the lane needs skills the profile doesn't document, say so
  and point at `/suggest-synonyms`.
- **One call, three questions.** At most one `AskUserQuestion` call, in Step 3,
  after the whole draft is on screen — carried by bare mode and by `pass-a`.
  `pass-b` asks nothing. Never apply unconfirmed text; if the user edits, re-show
  the changed part before writing.
- The lane's rows come only from FUTURE discovery runs and new inbox clips.
  Existing parquet rows are never reclassified.

## Step 1 — preflight (no writes)

`$ARGUMENTS`: first token = snake_case lane id, must match `^[a-z][a-z0-9_]*$`;
an optional second token `pass-a` or `pass-b` selects the mode; the rest, if
present, is a one-line description — in `pass-a` that description is the role
titles the user stated in plain text, and it is the primary source for
`search_terms`. No token → derive a name from the resume and carry it into the
Step 3 draft as part of what gets confirmed.

```bash
uv run verticals-check
ls profile/verticals/<name>/lane.yaml 2>/dev/null
ls -d profile/verticals/<name> 2>/dev/null
ls profile/verticals/<name>/.pass_a_only 2>/dev/null
uv run python -c "
import yaml; d = yaml.safe_load(open('profile/verticals.yaml'))
print('default_vertical:', d.get('default_vertical'))
print('lanes:', d.get('lanes'))
print('defaults:', d.get('defaults'))
print('rules:', len(d.get('classifier_rules') or []))"
```

Then, in bare mode and `pass-b` only — **not** in `pass-a`, where unauthored
bullets are the expected state:

```bash
cmp -s profile/bullets.md profile/bullets.example.md && echo "STOP: bullets.md is still the example"
cmp -s profile/skills_master.md profile/skills_master.example.md && echo "STOP: skills_master.md is still the example"
```

Either `STOP:` line means the file is still the example person: stop and send
the user to `/onboarding step 2`.

**The marker decides what a partial lane means.** A hit on the `lane.yaml` or the
`ls -d` means a previous run got partway:

| marker | mode | action |
|---|---|---|
| present | `pass-b` | expected — proceed |
| present | bare | resume at pass B: do Step 2 items 3-5, Step 4b, Step 5b, Step 6b, delete the marker, finish |
| present | `pass-a` | the lane block already exists — re-show it and stop unless the user asks to redraft |
| absent | `pass-b` | stop; the lane block is not there to build on |
| absent | any | ambiguous partial run — stop; ask resume vs rename |

While the marker exists, `verticals-check` fails on the missing prose and resume shape.
That is the expected mid-state, not damage to repair.

Two accepted starting states:

- **Straight out of `onboard-scaffold`** — `lanes: []` and `classifier_rules:`
  are empty, `default_vertical` is already
  the new lane name, so `verticals-check` fails. That failure is the expected
  input, not a problem to repair: adding the block plus one rule is exactly what
  makes the file load.
- **A config that already loads** — append the lane at the end of `lanes:`
  and its rule at the end of `classifier_rules`.

`default_vertical`, three cases:

| current value | action |
|---|---|
| already `<name>` | leave it — do not write it a second time |
| another configured lane | leave it — appending a lane never changes the primary |
| neither `<name>` nor a configured lane | stop; ask which lane should be default before writing anything |

Any other `verticals-check` failure — unparsable YAML, wrong `schema_version`,
or an *existing* lane missing its
rubric/tailoring/resume — stops the command. Quick mode does not repair
unrelated config.

## Step 2 — draft (no writes)

Items 1-2 are `pass-a`'s; items 3-5 are `pass-b`'s; bare mode drafts all five.
Items 1-4 write files; item 5 edits the lane's `lane.yaml` and three shared sources.

Read `profile/verticals.example.yaml` and, for items 3-5, `profile/bullets.md`,
`profile/skills_master.md` and
`profile/resume.yaml`, `profile/verticals/example_primary/{rubric.md,tailoring.md}`
and the `resume:` block in `profile/verticals/example_primary/lane.yaml`.
Draft from those plus the lane name and description. Ask nothing yet.

In `pass-a`, `search_terms` and `linkedin_terms` come from the role titles in the
description argument plus the lane name — bullets are not read.

**1. The lane's `lane.yaml`.** Exactly these keys, nothing else. A key in
`verticals.yaml` `defaults:` sits under it: a list there follows the lane's own,
any other key the lane sets replaces the default's.

- `display_name` — shortlist section header text.
- `search_terms` — 6-12 real job titles for the lane, spine first, adjacent
  after; comment the tiers like the example block does.
- `linkedin_terms` — the reduced set (429 mitigation), usually the spine only.
- `years` — required years mapped to a seniority cap (0-20), largest key =
  Step 3's cap, lower caps for higher asks (cap 5 → `{3: 14, 4: 10, 5: 6}`).
  A JD the judge reads asking more than the largest key is forced to skip.
  Leave it out when `defaults:` already holds the same table.
- A `# Target level: <answer>` comment line right above `years` (or where it
  would be): Step 3's level answer, which item 3 turns into the rubric's
  seniority tiers.

Omit `skill_weights`, `drop_phrases` and the lane title lists entirely.

**2. One `classifier_rules` entry**, appended last:

- Lane is the only one configured → catch-all `pattern: '.'`.
- Other lanes exist → a word-boundary alternation over the lane's spine
  keywords, `'\b(?:kw1|kw2|kw3)\b'`. Never a catch-all here. Priority against
  the other lanes is `/tune-vertical`'s.

**3. `profile/verticals/<name>/rubric.md`** — example_primary's shape: the
four axes with maxima fixed in code (`score_store.AXIS_MAXIMA`: `title` 0-30,
`skills` 0-30, `seniority` 0-20, `domain` 0-20). State the tiers against this lane's
spine titles and the profile's real evidence; the `seniority` tiers put the
lane's `# Target level:` band on top and score titles above or below it down.
Standard bands are fine — the boundaries are `/tune-vertical`'s. With no `skill_weights` in the config, anchor
`skills` on how many of the profile's attested skills the JD demands.

**4. `profile/verticals/<name>/tailoring.md`** — judgment only, in
example_primary's shape: summary framing and when to change the baseline picks.
Name projects in words; `verticals-check` rejects bullet
or skill ids here, because they belong in the sources.

**5. The resume shape** — what `/tailor` builds from and `/score`'s judges read:
- a `resume:` key in the lane block: `sections` (the order of summary, work,
  projects, education and skills), `bullets: [min, max]` (at least 10, and
  enough to fill one page), `skill_lines` (the line labels, in order) and a
  baseline `summary` that states only what the lane's baseline bullets attest
- `lanes: {<name>: {min, max, order}}` on each `profile/resume.yaml` context
  this lane shows
- `<name>` added to `default_for` on each bullet the baseline picks, within
  those budgets
- `<name>: "<line label>"` added to `show` on each skill a line lists

## Step 3 — one confirm-or-edit (bare mode and `pass-a`; `pass-b` skips to Step 4)

Show, in one message: the YAML block in full, the classifier rule in full, and —
bare mode only — three or four lines summarizing each artifact (rubric tiers,
the tailoring framing, the resume shape's sections, budget, baseline picks and
skill lines). Say in one line what
you inferred and what you left for `/tune-vertical`. After the answers land,
restate the locked decisions including the `years` table and the level answer.

Then one `AskUserQuestion` carrying all three questions, in this order — the
confirm-or-edit stays last.

"Skip jobs asking for more than how many years of experience?" — `3 years` /
`5 years` / `8 years` / `12 years`. Say in one line that anything above the cap
is skipped by the judge, and `/tune-vertical` can change it later.

"What level are you targeting?" — `Entry / associate` / `Mid-level` /
`Senior / staff` / `Lead / director and up`. The answer becomes the lane's
`# Target level:` line and the rubric's seniority tiers. Say in one line that
no title is dropped for its level: the judge scores titles outside the band
down.

"Use this lane as drafted?":

- Use as drafted
- Edit the search terms (they give the list; re-show, then apply)
- Rename the lane
- Show every artifact in full first (bare mode; in `pass-a`, `Show the block in
  full first`)

## Step 4 — apply

Write the block to `profile/verticals/<name>/lane.yaml`, append `<name>` to
`lanes:` and the rule to the end of `classifier_rules`, deleting the scaffold's
two `# /new-vertical <name> ... here.` placeholder comments as you go.
Touch `default_vertical` only in the case Step 1's table says to.

Bare mode also writes items 3-5 here, then goes to Step 5b.

**4a — `pass-a`'s last action.** Create `profile/verticals/<name>/` and write
`profile/verticals/<name>/.pass_a_only`, one line naming what is still missing:

```bash
mkdir -p profile/verticals/<name>
echo "pass-a only: rubric.md, tailoring.md and the resume shape not written yet; run /new-vertical <name> pass-b" > profile/verticals/<name>/.pass_a_only
```

**4b — `pass-b`.** Write items 3-5, run Step 5b, then delete the
marker once verification is clean:

```bash
rm -f profile/verticals/<name>/.pass_a_only
```

## Step 5a — verify (`pass-a`)

```bash
uv run config verticals <name>
uv run config misclassified <name>
```

`config verticals` loads the config, which is what catches a malformed `years` table or title
term before the scrape.

Do not run `verticals-check` or `tests/repo/test_real_config_drift.py` here — both
fail by design until pass-b writes the prose and resume shape.

## Step 5b — verify (bare mode and `pass-b`)

Both commands above, then:

```bash
uv run verticals-check
uv run pytest tests/repo/test_real_config_drift.py -q -rs   # no SKIPPED line
```

Every search and LinkedIn term must classify to this lane; widen the rule until
clean, re-showing the changed rule before each write. A skipped drift test means
it never saw the lane.

## Step 6a — report (`pass-a`)

The lane block, the rule, the `years` table, the level answer, and the marker
path. State that the lane is scrape-ready but not yet scoreable: `uv run
discover` can run now; scoring needs `pass-b`'s prose and resume shape. Name the single next
step, `/new-vertical <name> pass-b`.

## Step 6b — report (bare mode and `pass-b`)

Files written, the locked decisions (terms, rule, the `years` table, the level answer,
the resume shape),
and:

- `uv run discover` then `/score` — their call, it hits the network.
- Board sources only reach boards listed in `data/universe/boards.csv`; if this lane's employers aren't there, LinkedIn and
  Indeed search terms are its only route.
- One line: once a real shortlist exists, `/tune-vertical <name>` sharpens the
  lane — skill weights, the title gate, rubric tiers — against postings you have
  actually seen.
