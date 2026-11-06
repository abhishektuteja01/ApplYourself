---
description: Sharpen an existing lane against real postings: terms, title gate, classifier, rubric tiers, resume shape. Config only.
model: opus
effort: high
disable-model-invocation: true
allowed-tools:
  - Bash(cd *)
  - Bash(git rev-parse *)
  - Bash(uv run verticals-check*)
  - Bash(uv run config *)
  - Bash(uv run score lane *)
  - Bash(uv run pytest tests *)
  - Bash(grep *)
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - AskUserQuestion
argument-hint: "<existing_vertical_name>"
---

# /tune-vertical — sharpen an existing lane against real rows

`/new-vertical` gets a lane loading on the loader's minimum; this is the pass
that makes it good. Requires `discover` and `/score` to have run.

Four rounds, ~10-15 minutes. Per round: **read the real rows → draft the change
→ show the full proposed text → user confirms or edits → apply exactly what was
confirmed → run the round's verify → report → next round.**

- **Editing, not creating.** Show the lane's current value beside every proposed
  one. A round that changes nothing is a valid outcome; say so and move on.
- **The user rules on outcomes, not syntax.** Ask "should this title be in your
  lane?", never "should this regex sit above that one?". You translate.
- **Batch.** One `AskUserQuestion` per round, up to four related questions,
  every option pre-filled from rows you actually read.
- **Never apply unconfirmed text.** If the user edits a draft, re-show it before
  applying.
- Interruptible: re-run and the preflight reports which rounds already landed.

## Hard rules (binding)

- **No code edits.** `src/` and the other command files are generic over the
  config. **Never touch `tests/**/fixtures/verticals.yaml`**, and never copy a
  real block into it.
- **Never put a literal real search term in a test.**
  `tests/repo/test_real_config_drift.py` is committed. Add structural assertions
  there if the lane needs them; nothing quoted from `profile/`.
- **NO-FAB** — read `.claude/shared/no_fab.md`. `skill_weights`, rubric anchors
  and the resume shape come only from the user's answers plus existing
  `profile/bullets.md` / `profile/skills_master.md` content. Never invent a
  skill; never add a new `SKILL-*` entry or bullet, only place existing ones. Gaps go to
  `/suggest-synonyms`.
- Editing terms or rules does **not** reclassify existing rows. The effect shows
  up on the next `discover`; `/score --all --vertical <name>` re-judges what is
  already there.

## Round 0 — preflight & evidence (no writes)

`$ARGUMENTS` = one configured lane name. It must already load; if not, this is
`/new-vertical`'s job, not this command's.

```bash
uv run verticals-check
uv run config verticals <name>
```

Read the lane's `profile/verticals/<name>/lane.yaml`, the `defaults:` in
`profile/verticals.yaml` that sit under it (a default list follows the lane's
own; any other key the lane sets replaces the default), and its
`rubric.md` in full. Then the rows — this is the evidence every later round
argues from. `jobs/clean.parquet` holds the 14-day window;
`jobs/scored.parquet` is the longer history.

```bash
# What the lane caught, what it did not (other lanes, out-of-lane titles), and its
# judged scores. Drops are not stored: the last `/score` printed them as
# dropped_<code>; the night's summary.md lists only first-time drops as Filtered.
uv run score lane <name>
```

Report in five lines: lane volume, the title families that dominate it, the
off-lane families closest to it, how many rows the last `/score` dropped and
under which code, and the score distribution. That report is Round 1's input.

## Round 1 — terms and the title gate

The question this round answers: is the lane catching the right titles, and
only those.

- **`search_terms` / `linkedin_terms`** — add the in-lane title families the
  evidence shows the current terms miss; drop terms whose rows the user rejects
  on sight. `linkedin_terms` stays the reduced set (429 mitigation).
- **The title gate** (`title_gate.lane_for`) is lenient: a title passes when
  it fits the lane (one of its `classifier_rules`, or one of its search terms,
  matched loosely) and holds no word of the one global ban list,
  verticals.yaml `defaults: title_exclude_terms`. A stray title reaching the
  judge is fine; a lost real job is the failure. An in-lane title family the
  lane misses goes in as a search term or a `terms` rule; a spelling the
  boards use (another language, an abbreviation) goes in `title_variants`. Add
  a ban word only for a title the user would never take, and check it first:
  no job scored 70 or more may hold it. Level words (intern, senior, lead,
  VP) stay with the judge. A lane's own `title_include_terms`,
  `title_strong_keep_terms` and `title_exclude_terms` are retired: never add
  them (`uv run config migrate` moves old ones out).

One self-collision trap, enforced by the Round 4 drift test — check it
while drafting:

- no ban word may match one of the lane's own search terms.

Ask the user to rule on ~8 concrete titles pulled from the evidence — in-lane
keeps, off-lane kills, and the genuinely ambiguous ones. Derive the changes from
their rulings. The night's run report (`jobs/runs/<run>.md`, "Title gate
drops") counts every drop per reason with sample titles.

Verify:

```bash
uv run config verticals <name>
uv run pytest tests/repo/test_real_config_drift.py -q -rs   # no SKIPPED line
```

## Round 2 — classifier rules, collisions, years

**`classifier_rules`.** Ordered, first match wins, and position decides
collisions (a build-lane rule must sit AFTER a risk lane's, so "AI Risk
Engineer" stays in the risk lane; a trailing catch-all stays last). A lane may
own several rules at different priorities. Three forms, all compiled by the
loader:

- `pattern: '<regex>'` — compiled IGNORECASE. `'\b(?:a|b|c)\b'` for a keyword
  family; a plain literal (`"AI Engineer"`) when the phrase is unambiguous.
- `pattern: {match: <str or list>, require_any: [<str>, ...]}` — a match term
  must appear AND, elsewhere in the title, one `require_any` entry too, each
  matched loosely (case, hyphens, spaces, plurals, word endings,
  `title_variants`; never from inside a word, so "ML Engineer" misses "HTML
  Engineer"; a short last require word runs on, "LLM" finds "LLMOps"). On a
  title a search for this lane found, the match term alone is enough. Use it
  for a word that is ambiguous alone ("agent", a two-letter module code).
- `terms: [<str>, ...]` — any one term, matched loosely the same way; with
  `keep: true` a title it matches passes even when it holds a ban word. Keep
  single words out of `terms`: they pull every title holding them into the
  lane.

This is where a `/new-vertical` catch-all `'.'` usually gets replaced: once a
second lane exists, or once the out-of-lane pool in Round 0 shows real noise
reaching the shortlist, swap it for a spine alternation and place it against the
other lanes' rules deliberately. Every row misfiled in Round 0's evidence is a
rule-position bug — name the row, propose the move, have the user confirm the
**ruling**, not the regex.

**`years`** — required years mapped to a seniority cap; a JD the judge reads
asking more than the largest key is forced to skip. Change only against
observed JDs. Nothing else drops a JD before judging but scoring.yaml's
`ineligible` phrases, plus its `no_sponsorship` phrases when your home need is
"now": `drop_phrases` no longer drops, so put what the lane must avoid in its
rubric.

Verify:

```bash
uv run config verticals <name>
uv run config classify '<title1>' '<title2>' '...'
uv run config misclassified <name>
uv run pytest tests/repo/test_real_config_drift.py -q -rs   # no SKIPPED line
```

Every `search_terms`, `linkedin_terms` and `abroad_terms` entry must route to
this lane as a board title (`config misclassified`), and the spot-check list
must include the collision titles that have to keep their
current vertical. Fix pattern or priority (with confirmation) until clean.

Then replay the change over the rows already on disk, so the user sees what
would move before the next scrape:

```bash
uv run config reclassify
```

Rows only actually move on the next `discover`; `/score --all --vertical <name>`
re-judges what is already there.

## Round 3 — `skill_weights` and rubric tiers

- **`skill_weights`** — 0-10 integer blocks, grouped, anchored ONLY to attested
  bullets and skills, each with an inline comment naming the evidence. Set them
  from what the shortlist's JDs actually demanded: Round 0's
  `keywords_to_mirror` column is the demand side, `profile/bullets.md` and
  `profile/skills_master.md` are the supply side. A skill the JDs keep asking
  for and the profile cannot attest is a `/suggest-synonyms` item, not a weight.
- **`rubric.md` tier boundaries** — the four axes and their maxima are fixed in
  code (`score_store.AXIS_MAXIMA`: `title` 0-30, `skills` 0-30, `seniority`
  0-20, `domain` 0-20). Only the *tiers* are
  tunable here. Retune them against the scored rows: if the top-scoring rows are
  ones the user would not apply to, the title or domain tiers are too generous;
  if roles they liked landed mid-pack, they are too tight. Once `skill_weights`
  exist, re-anchor `skills` on the weight sum. Propose the top-band
  and bottom-band boundaries and have them confirmed.

Verify:

```bash
uv run verticals-check
```

Then have the user look at what changed:
`/score --all --vertical <name>`, then the shortlist. It re-judges every row in the
lane's window, so it costs a full scoring pass — their call.

## Round 4 — resume shape, tests, audit

1. Propose changes to this lane's resume shape, driven by Round 3's weights and
   the shortlist's `keywords_to_mirror`:
   - `show: {<name>: "<line label>"}` on existing `SKILL-*` entries in
     `profile/skills_master.md` that the shortlist keeps asking for; drop the
     lane from `show` on skills no posting names
   - `default_for` on the bullets that land the most keywords, within each
     context's `{min, max}` in `profile/resume.yaml`
   Show the proposed changes; the user approves the set.

2. `tests/repo/test_real_config_drift.py` runs against `profile/verticals.yaml` when
   present and is what covers this lane. It fails if any
   `search_terms`/`linkedin_terms` entry misclassifies, if
   the source check fails or a baseline resume does not build, or if a
   search term does not pass its own lane's title gate. Add structural assertions there if this
   round's changes need more — never a literal real term.

3. Final audit:

   ```bash
   uv run verticals-check
   uv run pytest tests -q
   uv run pytest tests/repo/test_real_config_drift.py -q -rs   # no SKIPPED line
   grep -c "<name>:" profile/skills_master.md
   ```

   Full suite green is a hard gate. The drift test skipping means it never saw
   the lane.

4. Completion summary: every file touched, every locked decision (terms, title
   gate, rule positions and collision rulings, drop rules, weights, rubric
   boundaries, resume shape), anything deliberately left alone, and the two follow-ups —
   `uv run discover` for the term and rule changes to take effect, and
   `/score --all --vertical <name>` for the weight and rubric changes to reach rows
   already scored.
