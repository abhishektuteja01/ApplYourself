---
description: Deep tuning pass on a vertical that already exists — run it after your first shortlist, against postings you have actually seen. Sharpens search terms, skill_weights, the title gate, classifier rules and collisions, disqualifier phrases, rubric tier boundaries and vertical_lean tags. Config only, no code edits. Use /new-vertical to create a lane, this to sharpen one.
model: opus
effort: high
allowed-tools:
  - Bash
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
  `tests/test_real_config_drift.py` is committed. Add structural assertions
  there if the lane needs them; nothing quoted from `profile/`.
- **NO-FAB** — read `.claude/shared/no_fab.md`. `skill_weights`, rubric anchors
  and `vertical_lean` tags come only from the user's answers plus existing
  `profile/bullets.md` / `profile/skills_master.md` content. Never invent a
  skill; never add a new `SKILL-*` entry, only tag existing ones. Gaps go to
  `/suggest-synonyms`.
- **Reasoning and stamp strings are literal config fields.** A new `phrases` or
  `title_phrases` list requires its partner (`reasoning_phrase`,
  `reasoning_title`) written fresh in the established voice; never reuse another
  lane's stamp.
- Editing terms or rules does **not** reclassify existing rows. The effect shows
  up on the next `discover`; `/rescore --vertical <name>` re-judges what is
  already there.

## Round 0 — preflight & evidence (no writes)

`$ARGUMENTS` = one configured lane name. It must already load; if not, this is
`/new-vertical`'s job, not this command's.

```bash
uv run verticals-check
uv run python -c "
from src.verticals import load_verticals
c = load_verticals(); v = c.verticals['<name>']
print('lanes:', c.names, 'default:', c.default_vertical)
print('search:', len(v.search_terms), 'linkedin:', len(v.linkedin_terms))
print('title gate: include', len(v.title_include_terms), 'exclude', len(v.title_exclude_terms), 'strong_keep', len(v.title_strong_keep_terms))
print('dq: max_years', v.disqualifier_max_years, 'phrases', len(v.disqualifier_phrases), 'title_phrases', len(v.disqualifier_title_phrases))
print('rules for this lane:', [i for i, (vt, _) in enumerate(c.classifier_rules) if vt == '<name>'], 'of', len(c.classifier_rules))"
```

Read the lane's current block in `profile/verticals.yaml` and its
`rubric.md` in full. Then the rows — this is the evidence every later round
argues from. `jobs/clean.parquet` holds the 14-day window;
`jobs/scored.parquet` is the longer history.

```bash
# What the lane actually caught.
uv run python -c "
import pandas as pd
d = pd.read_parquet('jobs/clean.parquet'); v = d[d.vertical == '<name>']
print(len(v), 'in lane /', len(d), 'total')
print(v.title.value_counts().head(40).to_string())"

# What it did not: neighbouring lanes and unclassified titles.
uv run python -c "
import pandas as pd
d = pd.read_parquet('jobs/clean.parquet')
for lane, g in d[d.vertical != '<name>'].groupby(d.vertical.fillna('')):
    print('--', lane or '(out-of-lane)', len(g)); print(g.title.value_counts().head(15).to_string())"

# What the deterministic pre-screen killed, by stamp.
uv run python -c "
import pandas as pd
s = pd.read_parquet('jobs/scored.parquet'); s = s[s.vertical == '<name>']
print(s.scored_by_model.value_counts().to_string())
print(s.fit_score.describe().to_string())
print(s.sort_values('fit_score', ascending=False)[['fit_score','suggested_action','keywords_to_mirror']].head(15).to_string())"
```

Report in five lines: lane volume, the title families that dominate it, the
off-lane families closest to it, how many rows the pre-screen killed and under
which stamp, and the score distribution. That report is Round 1's input.

## Round 1 — terms and the title gate

The question this round answers: is the lane catching the right titles, and
only those.

- **`search_terms` / `linkedin_terms`** — add the in-lane title families the
  evidence shows the current terms miss; drop terms whose rows the user rejects
  on sight. `linkedin_terms` stays the reduced set (429 mitigation).
- **The title gate** — `title_include_terms`, `title_exclude_terms`,
  `title_strong_keep_terms`. Optional to the loader, load-bearing in
  `cleaning.apply_title_exclusion`: a scraped row is kept iff
  `strong_keep OR (include_ok AND NOT exclude)`. **An empty
  `title_include_terms` leaves the include-gate OFF** — everything the
  classifier matches is kept unless it trips an exclude. Turn the gate on only
  when the observed title pool shows the spine is namable in a handful of terms
  and the noise is too varied for a blocklist. Pair `title_strong_keep_terms`
  with unambiguous in-lane titles that would otherwise trip an exclude.

Two self-collision traps, both enforced by the Round 4 drift test — check them
while drafting:

- no `title_phrases` entry may appear inside any of this lane's own
  `search_terms`; a hit auto-skips every row that term finds, before any judge.
- no `title_exclude_terms` entry may word-match one of its own `search_terms`
  unless a `title_strong_keep_terms` entry covers it.

Ask the user to rule on ~8 concrete titles pulled from the evidence — in-lane
keeps, off-lane kills, and the genuinely ambiguous ones. Derive the lists from
their rulings.

Verify:

```bash
uv run python -c "from src.verticals import load_verticals; v = load_verticals().verticals['<name>']; print(len(v.search_terms), len(v.linkedin_terms), len(v.title_include_terms), len(v.title_exclude_terms), len(v.title_strong_keep_terms))"
uv run pytest tests/test_real_config_drift.py -q -rs   # no SKIPPED line
```

## Round 2 — classifier rules, collisions, disqualifiers

**`classifier_rules`.** Ordered, first match wins, and position decides
collisions (a build-lane rule must sit AFTER a risk lane's, so "AI Risk
Engineer" stays in the risk lane; a trailing catch-all stays last). A lane may
own several rules at different priorities. Two forms, both compiled by the
loader:

- `pattern: '<regex>'` — compiled IGNORECASE. `'\b(?:a|b|c)\b'` for a keyword
  family; a plain literal (`"AI Engineer"`) when the phrase is unambiguous.
- `pattern: {match: <str>, require_any: [<str>, ...]}` — the match string must
  appear AND at least one `require_any` entry must too; comparison normalizes
  case, hyphens and NBSP. Use it when a title word is ambiguous alone.

This is where a `/new-vertical` catch-all `'.'` usually gets replaced: once a
second lane exists, or once the out-of-lane pool in Round 0 shows real noise
reaching the shortlist, swap it for a spine alternation and place it against the
other lanes' rules deliberately. Every row misfiled in Round 0's evidence is a
rule-position bug — name the row, propose the move, have the user confirm the
**ruling**, not the regex.

**`disqualifier`** — this is where `phrases` and `title_phrases` earn their
place, from rows the pre-screen should have killed and didn't:

- `max_years` — raise or lower only against observed JDs.
- `phrases` — case-insensitive JD substrings that are a deterministic kill for
  this profile. Nonempty requires `reasoning_phrase`.
- `title_phrases` — case-insensitive title substrings, checked before
  phrases/years, for the adjacent title families job boards fuzzy-match your
  search terms into. Nonempty requires `reasoning_title`.

Verify:

```bash
uv run python -c "from src.verticals import load_verticals; c = load_verticals(); v = c.verticals['<name>']; print(c.names, len(v.search_terms), len(v.linkedin_terms), v.disqualifier_scored_by, len(v.title_include_terms), len(v.title_exclude_terms))"
uv run python -c "from src.discovery.cleaning import classify_vertical_from_title as c; print([c(t) for t in ['<title1>', '<title2>', '...']])"
uv run python -c "
from src.verticals import load_verticals
from src.discovery.cleaning import classify_vertical_from_title as c
v = load_verticals().verticals['<name>']
bad = [t for t in v.search_terms + v.linkedin_terms if c(t) != '<name>']
print('MISCLASSIFIED:', bad or 'none')"
uv run pytest tests/test_real_config_drift.py -q -rs   # no SKIPPED line
```

Every `search_terms` and `linkedin_terms` entry must classify to this lane, and
the spot-check list must include the collision titles that have to keep their
current vertical. Fix pattern or priority (with confirmation) until clean.

Then replay the change over the rows already on disk, so the user sees what
would move before the next scrape:

```bash
uv run python -c "
import pandas as pd
from src.discovery.cleaning import classify_vertical_from_title as c
d = pd.read_parquet('jobs/clean.parquet')
now = d.title.map(c)
moved = d[now != d.vertical.fillna('')]
print(len(moved), 'rows would reclassify')
print(moved.assign(new=now[moved.index])[['title','vertical','new']].head(30).to_string())"
```

Rows only actually move on the next `discover`; `/rescore --vertical <name>`
re-judges what is already there.

## Round 3 — `skill_weights` and rubric tiers

- **`skill_weights`** — 0-10 integer blocks, grouped, anchored ONLY to attested
  bullets and skills, each with an inline comment naming the evidence. Set them
  from what the shortlist's JDs actually demanded: Round 0's
  `keywords_to_mirror` column is the demand side, `profile/bullets.md` and
  `profile/skills_master.md` are the supply side. A skill the JDs keep asking
  for and the profile cannot attest is a `/suggest-synonyms` item, not a weight.
- **`rubric.md` tier boundaries** — the four axes and their maxima are fixed by
  `profile/scoring_rubric.md` (`title` 0-30, `skills` 0-30, `seniority` 0-20,
  `domain` 0-20; the rubric prose labels them `title_match` /
  `jd_skill_overlap` / `seniority_fit` / `domain_bonus`). Only the *tiers* are
  tunable here. Retune them against the scored rows: if the top-scoring rows are
  ones the user would not apply to, the title or domain tiers are too generous;
  if roles they liked landed mid-pack, they are too tight. Once `skill_weights`
  exist, re-anchor `jd_skill_overlap` on the weight sum. Propose the top-band
  and bottom-band boundaries and have them confirmed; keep the
  `## Additional self-check items` section current.

Verify:

```bash
uv run verticals-check
```

Then have the user look at what changed:
`/rescore --vertical <name>`, then the shortlist. It re-judges every row in the
window, so it costs a full scoring pass — their call.

## Round 4 — tagging, tests, audit

1. Propose `vertical_lean` additions **on existing `SKILL-*` entries only** in
   `profile/skills_master.md` (values must be configured vertical names — the
   file's header says so). Drive it from Round 3's weights and the shortlist's
   `keywords_to_mirror`: a skill this lane leans on should carry the tag, since
   `/tailor` Step 3e ranks the Skills section by it. Show the proposed entry
   list; the user approves the set.

2. `tests/test_real_config_drift.py` runs against `profile/verticals.yaml` when
   present and is what covers this lane. It fails if any
   `search_terms`/`linkedin_terms` entry misclassifies, if
   `rubric.md`/`tailoring.md`/`resume_file` are missing, if the `scored_by`
   stamp collides with another lane's, if a `title_exclude_terms` entry kills
   one of its own search terms, or if a `disqualifier.title_phrases` entry
   matches one of its own search terms. Add structural assertions there if this
   round's changes need more — never a literal real term.

3. Final audit:

   ```bash
   uv run verticals-check
   uv run pytest tests -q
   uv run pytest tests/test_real_config_drift.py -q -rs   # no SKIPPED line
   grep -c "vertical_lean:.*<name>" profile/skills_master.md
   ```

   Full suite green is a hard gate. The drift test skipping means it never saw
   the lane.

4. Completion summary: every file touched, every locked decision (terms, title
   gate, rule positions and collision rulings, disqualifier, weights, rubric
   boundaries, tags), anything deliberately left alone, and the two follow-ups —
   `uv run discover` for the term and rule changes to take effect, and
   `/rescore --vertical <name>` for the weight and rubric changes to reach rows
   already scored.
