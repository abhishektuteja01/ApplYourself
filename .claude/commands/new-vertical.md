---
description: Quick-add a job-search vertical — drafts the one profile/verticals.yaml block the loader requires, a classifier rule, rubric.md, tailoring.md and the scoring resume from the existing profile, then asks an experience cap and a single confirm-or-edit. Splits into pass-a (scrape-ready lane block + rule) and pass-b (the three prose files) when a mode token is given. Config only, no code edits. Deep tuning is /tune-vertical.
model: opus
effort: medium
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - AskUserQuestion
argument-hint: <vertical_snake_case_name> [pass-a|pass-b] [one-line description]
---

# /new-vertical — add a lane (quick mode)

Verticals are data-driven: `profile/verticals.yaml` +
`profile/verticals/<name>/{rubric.md, tailoring.md, resume_<name>.md}` are the
whole registry. If you find yourself editing a `.py` file or another command
file, stop — the config contract is being violated.

Quick mode writes the loader's minimum and nothing more, with two questions.
Everything optional stays out of the file.

## Modes

Optional second token in `$ARGUMENTS`, spelled exactly `pass-a` or `pass-b`.

| token | writes | asks | verifies |
|---|---|---|---|
| *(none)* | all five artifacts, one pass | both questions | full block |
| `pass-a` | the `verticals.yaml` lane block + one `classifier_rules` entry + the marker | both questions | loader + classifier only |
| `pass-b` | `rubric.md`, `tailoring.md`, `resume_<name>.md`; deletes the marker | nothing | full block |

`load_verticals()` needs only the lane block, so `pass-a` is enough to start a
scrape. The three prose files are needed only to score, and only they need
authored bullets — which is why they can wait.

Marker file: `profile/verticals/<name>/.pass_a_only`, written by `pass-a`,
deleted by `pass-b`.

**Out of scope here — this is `/tune-vertical`'s entire reason to exist. Do not
ask about it:** `skill_weights`, `title_include_terms` /
`title_strong_keep_terms`, classifier rule priority and
collisions, `disqualifier.phrases` / `title_phrases`, rubric tier boundaries,
`vertical_lean` tagging.

`title_exclude_terms` is in scope here — it is a blocklist and cannot empty a
lane. `title_include_terms` never is: a nonempty one flips the
lane to an allowlist and the gate runs during cleaning, so a blind list discards
rows before anyone can inspect them.

## Hard rules (binding)

- **No code edits.** `src/`, `tests/*.py` and other command files stay
  untouched. Never edit `tests/**/fixtures/verticals.yaml` — synthetic by
  design; a new lane needs no fixture change.
  `tests/test_real_config_drift.py` is generic over whatever lanes
  `profile/verticals.yaml` holds, so it covers the new lane unedited. Lane-specific
  structural assertions are `/tune-vertical`'s.
- **NO-FAB** — read `.claude/shared/no_fab.md`. Every drafted term, skill and
  resume line traces to `profile/bullets.md`, `profile/skills_master.md` or the
  resume onboarding already parsed. Never invent a skill and never add a new
  `SKILL-*` entry. If the lane needs skills the profile doesn't document, say so
  and point at `/suggest-synonyms`.
- **Reasoning and stamp strings are literal config fields** — draft them fresh
  (`rubric:<name-with-dashes>-jd-years-disqualifier`), never reuse another
  lane's.
- **One call, two questions.** At most one `AskUserQuestion` call, in Step 3,
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
grep -n "^  <name>:" profile/verticals.yaml
ls -d profile/verticals/<name> 2>/dev/null
ls profile/verticals/<name>/.pass_a_only 2>/dev/null
uv run python -c "
import yaml; d = yaml.safe_load(open('profile/verticals.yaml'))
print('default_vertical:', d.get('default_vertical'))
print('lanes:', list(d.get('verticals') or {}))
print('rules:', len(d.get('classifier_rules') or []))
print('out_of_lane:', bool((d.get('out_of_lane') or {}).get('reasoning')))"
```

Then, in bare mode and `pass-b` only — **not** in `pass-a`, where unauthored
bullets are the expected state:

```bash
cmp -s profile/bullets.md profile/bullets.example.md && echo "STOP: bullets.md is still the example"
cmp -s profile/skills_master.md profile/skills_master.example.md && echo "STOP: skills_master.md is still the example"
```

Either `STOP:` line means `onboard-scaffold` copied the template and nothing has
authored it yet. Stop and send the user to `/onboarding step 2`. Both files parse
fine as the example person, so drafting against them silently fills the lane's
resume, rubric and Skills layout with someone else's evidence.

**The marker decides what a partial lane means.** A hit on the `grep` or the
`ls -d` means a previous run got partway:

| marker | mode | action |
|---|---|---|
| present | `pass-b` | expected — proceed |
| present | bare | resume at pass B: do Step 2 items 3-5, Step 4b, Step 5b, Step 6b, delete the marker, finish |
| present | `pass-a` | the lane block already exists — re-show it and stop unless the user asks to redraft |
| absent | `pass-b` | stop; the lane block is not there to build on |
| absent | any | ambiguous partial run — stop; ask resume vs rename |

While the marker exists, `verticals-check` fails on the three missing files.
That is the expected mid-state, not damage to repair.

Two accepted starting states:

- **Straight out of `onboard-scaffold`** — `verticals:` and `classifier_rules:`
  are empty, `out_of_lane.reasoning` is intact, `default_vertical` is already
  the new lane name, so `verticals-check` fails. That failure is the expected
  input, not a problem to repair: adding the block plus one rule is exactly what
  makes the file load.
- **A config that already loads** — append the lane at the end of the
  `verticals:` mapping and its rule at the end of `classifier_rules`.

`default_vertical`, three cases:

| current value | action |
|---|---|
| already `<name>` | leave it — do not write it a second time |
| another configured lane | leave it — appending a lane never changes the primary |
| neither `<name>` nor a configured lane | stop; ask which lane should be default before writing anything |

Any other `verticals-check` failure — unparsable YAML, wrong `schema_version`,
missing `out_of_lane.reasoning`, or an *existing* lane missing its
rubric/tailoring/resume — stops the command. Quick mode does not repair
unrelated config.

## Step 2 — draft (no writes)

Items 1-2 are `pass-a`'s; items 3-5 are `pass-b`'s; bare mode drafts all five.

Read `profile/verticals.example.yaml` and, for items 3-5, `profile/bullets.md`,
`profile/skills_master.md` and
`profile/verticals/example_primary/{rubric.md,tailoring.md,resume_example_primary.md}`.
Draft from those plus the lane name and description. Ask nothing yet.

In `pass-a`, `search_terms` and `linkedin_terms` come from the role titles in the
description argument plus the lane name — bullets are not read.

**1. The `verticals.yaml` block.** Exactly these keys, nothing else:

- `display_name` — shortlist section header text.
- `resume_file` — `profile/verticals/<name>/resume_<name>.md` (item 5 creates
  it; `load_verticals()` does not require the file, `verticals-check` does).
- `search_terms` — 6-12 real job titles for the lane, spine first, adjacent
  after; comment the tiers like the example block does.
- `linkedin_terms` — the reduced set (429 mitigation), usually the spine only.
- `disqualifier` — `max_years` from Step 3's answer, a fresh `scored_by` stamp,
  and `reasoning_years` in the established voice ("Auto-skipped by deterministic
  pre-screen: ..."). Omit `phrases` and `title_phrases`.
- `title_exclude_terms` — a short blocklist of title words that are not this
  lane (adjacent seniorities, unrelated functions).

Omit `skill_weights`, `title_include_terms` and `title_strong_keep_terms`
entirely. A nonempty `title_include_terms` turns the lane into an allowlist
enforced during cleaning, which can empty the lane before anyone sees a row.

**2. One `classifier_rules` entry**, appended last:

- Lane is the only one configured → catch-all `pattern: '.'`.
- Other lanes exist → a word-boundary alternation over the lane's spine
  keywords, `'\b(?:kw1|kw2|kw3)\b'`. Never a catch-all here. Priority against
  the other lanes is `/tune-vertical`'s.

**3. `profile/verticals/<name>/rubric.md`** — example_primary's shape: the
four axes with maxima fixed by `profile/scoring_rubric.md` (`title_match` 0-30,
`jd_skill_overlap` 0-30, `seniority_fit` 0-20, `domain_bonus` 0-20; judges emit
the scaffold's keys `title`/`skills`/`seniority`/`domain`), plus an
`## Additional self-check items` section. State the tiers against this lane's
spine titles and the profile's real evidence. Standard bands are fine — the
boundaries are `/tune-vertical`'s. With no `skill_weights` in the config, anchor
`jd_skill_overlap` on how many of the profile's attested skills the JD demands.

**4. `profile/verticals/<name>/tailoring.md`** — bullet budget (mix summing to
≥10 non-frozen bullets), project ordering default, summary framing, section
order, all as defaults the JD may fine-tune and never override. It MUST carry
the heading spelled exactly **`**Skills layout (<n> lines):**`** followed by one
numbered category line per skills row, each naming real `SKILL-<ID>`s from
`profile/skills_master.md`. `/tailor` Step 3e reads that heading by name and
improvises the entire Skills section if it is missing.

**5. `profile/verticals/<name>/resume_<name>.md`** — the file `resume_file`
points at. Keep example_primary's section shape; fill it from the resume
onboarding parsed and the canonical bullets. No placeholder text survives:
judges score every row in this lane against this file.

## Step 3 — one confirm-or-edit (bare mode and `pass-a`; `pass-b` skips to Step 4)

Show, in one message: the YAML block in full, the classifier rule in full, and —
bare mode only — three or four lines summarizing each prose file (rubric tiers,
bullet budget + skills layout line count, resume sections). Say in one line what
you inferred and what you left for `/tune-vertical`.

Then one `AskUserQuestion` carrying both questions.

"Skip jobs asking for more than how many years of experience?" — `3 years` /
`5 years` / `8 years` / `12 years`. Say in one line that anything above the cap
is dropped before scoring, and `/tune-vertical` can change it later.

"Use this lane as drafted?":

- Use as drafted
- Edit the search terms (they give the list; re-show, then apply)
- Rename the lane
- Show the three files in full first (bare mode; in `pass-a`, `Show the block in
  full first`)

## Step 4 — apply

Insert the block at the end of the `verticals:` mapping and append the rule at
the end of `classifier_rules`, deleting the scaffold's two
`# /new-vertical <name> writes your ... here.` placeholder comments as you go.
Touch `default_vertical` only in the case Step 1's table says to.

Bare mode also writes the three prose files here, then goes to Step 5b.

**4a — `pass-a`'s last action.** Create `profile/verticals/<name>/` and write
`profile/verticals/<name>/.pass_a_only`, one line naming what is still missing:

```bash
mkdir -p profile/verticals/<name>
echo "pass-a only: rubric.md, tailoring.md, resume_<name>.md not written yet; run /new-vertical <name> pass-b" > profile/verticals/<name>/.pass_a_only
```

**4b — `pass-b`.** Write the three prose files, run Step 5b, then delete the
marker once verification is clean:

```bash
rm -f profile/verticals/<name>/.pass_a_only
```

## Step 5a — verify (`pass-a`)

```bash
uv run python -c "from src.verticals import load_verticals; c = load_verticals(); v = c.verticals['<name>']; print(c.names, c.default_vertical, len(v.search_terms), len(v.linkedin_terms), v.disqualifier_scored_by)"
uv run python -c "
from src.discovery.cleaning import classify_vertical_from_title as c
from src.verticals import load_verticals
v = load_verticals().verticals['<name>']
bad = [t for t in v.search_terms + v.linkedin_terms if c(t) != '<name>']
print('MISCLASSIFIED:', bad or 'none')"
```

Do not run `verticals-check` or `tests/test_real_config_drift.py` here — both
fail by design while the three prose files are missing.

## Step 5b — verify (bare mode and `pass-b`)

Both commands above, then:

```bash
uv run verticals-check
uv run pytest tests/test_real_config_drift.py -q -rs   # no SKIPPED line
```

Every search and LinkedIn term must classify to this lane; widen the rule until
clean, re-showing the changed rule before each write. A skipped drift test means
it never saw the lane.

## Step 6a — report (`pass-a`)

The lane block, the rule, `max_years`, `title_exclude_terms`, and the marker
path. State that the lane is scrape-ready but not yet scoreable: `uv run
discover` can run now; scoring needs `pass-b`'s three files. Name the single next
step, `/new-vertical <name> pass-b`.

## Step 6b — report (bare mode and `pass-b`)

Files written, the locked decisions (terms, rule, `max_years`, skills layout),
and:

- `uv run discover` then `/score` — their call, it hits the network.
- Board sources only reach companies already in `profile/companies.yaml` /
  `data/universe/*.csv`; if this lane's employers aren't there, LinkedIn and
  Indeed search terms are its only route.
- No `SKILL-*` entry carries a `vertical_lean` for this lane yet, so `/tailor`
  Step 3e's lean-based rank clauses stay inert until `/tune-vertical` Round 4.
- One line: once a real shortlist exists, `/tune-vertical <name>` sharpens the
  lane — skill weights, the title gate, rubric tiers — against postings you have
  actually seen.
