---
description: Interactive interview that onboards a brand-new job-search vertical — one profile/verticals.yaml block, classifier rules, title gate, rubric.md + tailoring.md, scoring resume, doc pointers. No code edits; everything derives from the config. Four batched rounds, ~10-15 min: user gives hints, the command drafts, user confirms, verify.
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
argument-hint: "<vertical_snake_case_name> [one-line description]"
---

# /new-vertical — onboard a new vertical (config-only)

Verticals are data-driven: `profile/verticals.yaml` +
`profile/verticals/<name>/{rubric.md, tailoring.md}` are the single
registry; `src/` and the other commands derive everything from them.
Onboarding a vertical is therefore a **pure data exercise** — if you find
yourself editing a `.py` file or a command file other than the doc pointers
in Round 4, stop: either the config contract is being violated or something
regressed.

## How this runs (binding)

Four rounds, ~10-15 minutes total. Per round: **draft everything from the
user's hints → show the full proposed text → user confirms or edits → apply
exactly what was confirmed → run the round's verify → report → next round.**

- **The user supplies hints, not finished config.** Draft generously: infer
  the obvious adjacent titles, the collision cases, the tier boundaries. Say
  in one line what you inferred and what you deliberately left out. Ask only
  what you genuinely cannot derive from `profile/` plus their hints.
- **Batch the questions.** One `AskUserQuestion` call per group of related
  decisions (max 4 questions per call), never one question per turn. Pre-fill
  options from what you actually read in `profile/` — real bullet ids, real
  title families, real neighboring lanes.
- **Never apply unconfirmed text.** A round may cover several files; show all
  of them before applying any. If the user edits a draft, re-show the revised
  text before applying.
- If interrupted, re-run this command: Round 1's checks report what already
  exists and the interview resumes at the first incomplete round.

## Hard rules (from CLAUDE.md — binding)

- **No code edits.** `src/` and `score.md`/`rescore.md`/`tailor.md` stay
  untouched — they are already generic over the config. `tests/*.py` is
  likewise untouched; the test fixtures are synthetic and must not be
  edited (see Round 4).
- **No fabrication:** `skill_weights`, rubric tier anchors, and
  `vertical_lean` tags come ONLY from the user's answers plus existing
  `profile/bullets.md` / `profile/skills_master.md` content. Never invent
  skills or experience; never add a new `SKILL-*` entry — only tag existing
  ones. If the vertical needs skills the profile doesn't document, point the
  user at `/suggest-synonyms`; do not fill the gap.
- **Reasoning/stamp strings are literal config fields** — draft them fresh
  for the new vertical (pattern: `rubric:<name-with-dashes>-jd-...`), never
  reuse another vertical's stamp.
- The new vertical's rows appear only from FUTURE discovery runs and new
  inbox clips (existing parquet rows are never reclassified). An empty
  shortlist section at first is expected, not a bug.

## Round 1 — preflight & charter (~3 min)

No file edits.

1. Parse `$ARGUMENTS`: first token = snake_case vertical id (must match
   `^[a-z][a-z0-9_]*$`); rest, if present, = one-line description.
2. Preflight — the config must be valid before it is touched, and a prior
   aborted run leaves traces in three places:
   ```bash
   uv run verticals-check
   grep -n "^  <name>:" profile/verticals.yaml
   ls -d profile/verticals/<name> 2>/dev/null
   grep -c "vertical_lean:.*<name>" profile/skills_master.md
   ```
   Any hit means a previous run got partway. Stop and ask (resume vs rename)
   before drafting.
3. Read `profile/bullets.md`, `profile/skills_master.md`, and an existing
   block in `profile/verticals.yaml` as the style reference (else
   `profile/verticals.example.yaml`). Come to the user with candidates, not
   blank questions.
4. **One batched `AskUserQuestion`** (a second call only if something in the
   first answer opens a real fork):
   - the lane's title spine — offer the title families you inferred
   - the title families that are explicitly out-of-lane (this is the
     collision surface Round 2 prices in)
   - position in the `verticals:` mapping — order is shortlist section order,
     first = primary; offer the concrete slots relative to existing lanes
   - which attested anchors carry the lane — options are real bullet ids /
     `SKILL-*` ids you just read
5. Show the charter as a short block, including seniority band and whether
   this lane has a deterministic JD kill-signal (you propose; they correct).
   User confirms before anything is edited.

## Round 2 — the `verticals.yaml` block + classifier rules (~4 min)

Draft both, show both, apply on one confirmation.

### The block (at the confirmed position in the `verticals:` mapping)

- `display_name` — shortlist section header text.
- `resume_file` — REQUIRED. Repo-relative path to the resume judges score
  this lane against (read by `score-judge.md`); convention is
  `profile/verticals/<name>/resume_<name>.md`. Round 3 creates the file; the
  block won't load until it exists.
- `search_terms` — full discovery query list; `linkedin_terms` — the
  reduced LinkedIn set (429 mitigation; may equal `search_terms`). Mirror the
  comment style of the reference block (spine vs adjacent/tail tiers).
- `skill_weights` — 0-10 integer blocks anchored ONLY to attested
  bullets/skills; name the evidence in inline comments like the
  existing blocks do.
- **The title gate** — `title_include_terms`, `title_exclude_terms`,
  `title_strong_keep_terms`. Optional to the loader, load-bearing in
  `cleaning.apply_title_exclusion`: a scraped row is kept iff
  `strong_keep OR (include_ok AND NOT exclude)`. **An empty
  `title_include_terms` turns the include-gate OFF for this lane** — every
  title the classifier matches is kept unless it trips an exclude. Decide it
  deliberately: draft an include list when the lane's spine is namable in a
  handful of terms, add excludes for the in-lane leakage that passes the
  gate, and pair `title_strong_keep_terms` with unambiguous in-lane titles
  that would otherwise trip an exclude. Mirror an existing lane's tiering.
- `disqualifier` — `max_years` (default 4 unless the user argues otherwise),
  `phrases` (does this lane have a deterministic JD kill-signal? `[]` is
  fine), `title_phrases` (do job boards fuzzy-match this lane's search terms
  into adjacent title families worth killing at the title level? `[]` is
  fine), `scored_by` stamp, `reasoning_years` (+ `reasoning_phrase` iff
  phrases nonempty, + `reasoning_title` iff title_phrases nonempty) — write
  fresh reasoning texts in the established voice ("Auto-skipped by
  deterministic pre-screen: ...").

Two self-collision traps, both enforced by the Round 4 drift test — check
them while drafting, not after:

- no `title_phrases` entry may appear inside any of this lane's own
  `search_terms`; a hit auto-skips every row that term finds, before any
  judge sees it.
- no `title_exclude_terms` entry may word-match one of its own
  `search_terms` unless a `title_strong_keep_terms` entry covers it.

### `classifier_rules`

Ordered, first match wins, and position decides collisions (e.g. a build-lane
rule must sit AFTER a risk lane's, so "AI Risk Engineer" stays in the risk
lane; a trailing catch-all usually stays last). A vertical may own multiple
rules at different priorities. Two forms, both compiled by the loader:

- `pattern: '<regex>'` — compiled IGNORECASE. `'\b(?:a|b|c)\b'` for a
  keyword family; a plain literal (`"AI Engineer"`) when the phrase is
  unambiguous on its own.
- `pattern: {match: <str>, require_any: [<str>, ...]}` — the match string
  must appear AND at least one `require_any` entry must too; comparison
  normalizes case, hyphens and NBSP. Use this when a title word is ambiguous
  alone — e.g. `match: "ML Engineer"` only claimed for this lane when the
  title also says LLM/GenAI/Generative.

Propose ~6 spot-check titles yourself with expected outputs — in-lane hits,
collision titles that must keep their current vertical, one unclassifiable —
and have the user confirm the **rulings**, not the regexes.

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
```

Every `search_terms` and `linkedin_terms` entry must classify to this
vertical. Fix pattern/priority (with confirmation) until clean.
`verticals-check` still fails until Round 3 creates the prose files —
expected; don't chase it yet.

## Round 3 — `rubric.md` + `tailoring.md` + the scoring resume (~4 min)

Draft all three, show all three, apply on one confirmation. Read an existing
lane's files as the shape reference (the committed
`profile/verticals/example_*/` show the neutral template).

- **the scoring resume** — the file `<name>.resume_file` points at (Round 2
  set it). `resume_file` is REQUIRED and its target must exist, so Round 2's
  block won't load until this file is on disk. Ask which existing resume this
  lane should be judged against: copy that one and reframe its summary and
  section order for the lane, or start from
  `profile/verticals/example_primary/resume_example_primary.md`. Everything
  in it must be attested in `profile/bullets.md`.
- **`rubric.md`** — a header stating who reads it, then the four axes, whose
  **maxima** are fixed by `profile/scoring_rubric.md` (which also fixes the
  JSON keys a judge emits: `title` 0-30, `skills` 0-30, `seniority` 0-20,
  `domain` 0-20). The axis *labels* inside a rubric are prose, not those
  keys — every lane in the repo, the `example_*` ones included, spells them
  `title_match` / `jd_skill_overlap` / `seniority_fit` / `domain_bonus`.
  Follow that convention. Cover: title 0-30 (which exact titles hit which
  band, out-of-lane → 0), skills 0-30 (this vertical's `skill_weights` as
  the anchor, standard bands, plus any lane-specific soft signals),
  seniority 0-20 (usually identical heuristic across verticals; note that
  rows over `max_years` are pre-screened out), domain 0-20 tiers — plus any
  LLM-judged caps and an `## Additional self-check items` section. Every tier
  anchored to the user's REAL evidence from Round 1; propose the top-band and
  bottom-band boundaries and have them confirmed.
- **`tailoring.md`** — page-budget hard floor (bullet mix summing to ≥10
  non-frozen bullets), project ordering default + JD fine-tune allowances,
  summary framing (what leads, what supports), WORK EXPERIENCE vs PROJECTS
  section order. It MUST carry a heading spelled
  **`Skills layout (<n> lines):`** followed by a numbered category line per
  skills row, each naming the eligible `SKILL-<ID>`s — `/tailor` Step 3e
  reads that heading by name and improvises the whole Skills section if it is
  absent. All under the binding meta-rule: vertical sets the default, JD
  fine-tunes, never the reverse.

Verify:

```bash
uv run verticals-check   # must now pass fully
```

## Round 4 — tests, tagging, audit (~3 min)

1. **Do NOT touch `tests/**/fixtures/verticals.yaml`.** They are synthetic
   (`example_primary/secondary/tertiary`) and deliberately do not track the
   real config; copying a real block in would put live strategy into a public
   file. A new vertical needs no fixture change.

   `tests/test_real_config_drift.py` runs against `profile/verticals.yaml`
   when present and is what covers the new vertical. It fails if any of its
   `search_terms`/`linkedin_terms` misclassifies, if its
   `rubric.md`/`tailoring.md`/`resume_file` are missing, if its `scored_by`
   stamp collides with another lane's, if a `title_exclude_terms` entry kills
   one of its own search terms, or if a `disqualifier.title_phrases` entry
   matches one of its own search terms. Add structural assertions there if
   the lane needs more — never a literal real term; that file is committed.

2. Propose `vertical_lean` additions **on existing `SKILL-*` entries only**
   in `profile/skills_master.md` (values must be configured vertical names —
   the file's header says so). Show the proposed entry list; the user
   approves the set.

3. Final audit:
   ```bash
   uv run verticals-check
   uv run pytest tests -q
   uv run pytest tests/test_real_config_drift.py -q -rs   # no SKIPPED line
   grep -c "vertical_lean:.*<name>" profile/skills_master.md
   ```
   Full suite green is a hard gate, and the drift test skipping means it
   never saw the new lane.

4. Completion summary: every file touched, every locked decision (terms,
   title gate, rule position + collision rulings, disqualifier, rubric
   anchors, budget), anything skipped, and the user's remaining steps:
   - `uv run discover` (their call; hits the network) to start filling the
     vertical, then `/score`.
   - Board sources only reach companies already in `profile/companies.yaml` /
     `data/universe/*.csv`. If this lane's employers aren't there, LinkedIn
     and Indeed search terms are its only route — name the gap and let them
     decide whether to add companies.
   - Existing rows are never reclassified; `/rescore --vertical <name>` only
     matters once the vertical has rows.

`CLAUDE.md` needs no edit: its "Verticals: the config spine" section describes
the mechanism, never the list of configured lanes.
