---
description: Interactive interview that onboards a brand-new job-search vertical — one profile/verticals.yaml block, classifier rules, rubric.md + tailoring.md, doc pointers. No code edits; everything derives from the config. Every stage is draft → user confirms → apply → verify.
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
in Stage 6, stop: either the config contract is being violated or something
regressed.

## The interview contract (binding)

- Work **one stage at a time**, in order. Per stage: **draft → show the full
  proposed text → wait for explicit user confirmation → apply exactly what
  was confirmed → run the stage's verify → report → next stage.**
- Never apply without confirmation. Never batch-apply multiple stages. If
  the user edits a draft, re-show the revised draft before applying.
- If interrupted, re-run this command: Stage 0's checks show what already
  exists and the interview resumes at the first incomplete stage.

## Hard rules (from CLAUDE.md — binding)

- **No code edits.** `src/` and `score.md`/`rescore.md`/`tailor.md` stay
  untouched — they are already generic over the config. `tests/*.py` is
  likewise untouched; the test fixtures are synthetic and must not be
  edited (see Stage 4).
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

## Stage 0 — preflight & charter

No file edits.

1. Parse `$ARGUMENTS`: first token = snake_case vertical id (must match
   `^[a-z][a-z0-9_]*$`); rest, if present, = one-line description. Ask for
   whatever is missing.
2. Checks:
   ```bash
   uv run python -m src.verticals   # current config must be valid before touching it
   grep -rn "<name>" profile/verticals.yaml || echo "name unused"
   ```
   If the name already appears, stop and ask (resume vs rename).
3. Interview the **charter**: target job titles/lanes, what's explicitly
   out-of-lane, seniority band, and which real experience anchors it — Read
   `profile/bullets.md` and `profile/skills_master.md`, name the candidate
   anchors, and let the user pick. Also ask: primary or secondary (i.e.
   where in the `verticals:` mapping order it slots — order = shortlist
   section order, first = primary).
4. Show the charter; user confirms before anything is edited.

## Stage 1 — the `verticals.yaml` block

Interview, then draft the new vertical's block (place it at the confirmed
position in the `verticals:` mapping):

- `display_name` — shortlist section header text.
- `resume_file` — REQUIRED. Repo-relative path to the resume judges score
  this lane against (read by `score-judge.md`); convention is
  `profile/verticals/<name>/resume_<name>.md`. Stage 3 creates the file; the
  block won't load until it exists.
- `search_terms` — full discovery query list; `linkedin_terms` — the
  reduced LinkedIn set (429 mitigation; may equal `search_terms`). Mirror
  the existing blocks' comment style (spine vs adjacent/tail tiers, rationale).
- `skill_weights` — 0-10 integer blocks anchored ONLY to attested
  bullets/skills; name the evidence in inline comments like the
  existing blocks do.
- `disqualifier` — `max_years` (default 4 unless the user argues otherwise),
  `phrases` (interview whether this lane has a deterministic JD kill-signal;
  `[]` is fine), `title_phrases` (interview whether job boards fuzzy-match
  this lane's search terms into adjacent title families worth killing at the
  title level; `[]` is fine), `scored_by` stamp, `reasoning_years`
  (+ `reasoning_phrase` iff phrases nonempty, + `reasoning_title` iff
  title_phrases nonempty) — write fresh reasoning texts in the established
  voice ("Auto-skipped by deterministic pre-screen: ...").

Verify:

```bash
uv run python -c "from src.verticals import load_verticals; c = load_verticals(); v = c.verticals['<name>']; print(c.names, len(v.search_terms), len(v.linkedin_terms), v.disqualifier_scored_by)"
```

(`python -m src.verticals` will still fail until Stage 3 creates the prose
files — expected; don't chase it yet.)

## Stage 2 — classifier rules

Draft the `classifier_rules` entries. **Interview the priority explicitly**
— the list is ordered, first match wins, and position decides collisions
(e.g. a build-lane rule must sit AFTER a risk lane's, so "AI Risk
Engineer" stays in the risk lane; a trailing catch-all usually stays last). A vertical may own multiple rules at different priorities. Patterns
are single-quoted YAML, `\b(?:...)\b`-wrapped, compiled IGNORECASE by the
loader.

Walk the user through ~6 spot-check titles (new-lane hits, collision titles
that must keep their old vertical, one unclassifiable), confirm expected
outputs, apply, then verify:

```bash
uv run python -c "from src.discovery.cleaning import classify_vertical_from_title as c; print([c(t) for t in ['<title1>', '<title2>', '...']])"
```

**Consistency requirement (enforced by the drift test in Stage 4):**
every `search_terms` and `linkedin_terms` entry from Stage 1 must classify
to this vertical. Check now:

```bash
uv run python -c "
from src.verticals import load_verticals
from src.discovery.cleaning import classify_vertical_from_title as c
v = load_verticals().verticals['<name>']
bad = [t for t in v.search_terms + v.linkedin_terms if c(t) != '<name>']
print('MISCLASSIFIED:', bad or 'none')"
```

Fix pattern/priority (with confirmation) until clean.

## Stage 3 — `rubric.md` + `tailoring.md` + the scoring resume

Create `profile/verticals/<name>/`:

- **the scoring resume** — the file `<name>.resume_file` points at (Stage 1
  set it; `profile/verticals/<name>/resume_<name>.md` is the convention).
  `resume_file` is REQUIRED and its target must exist, so Stage 1's block
  won't load until this file is on disk. Ask the user which existing resume
  this lane should be judged against: copy that one and reframe its summary
  and section order for the lane, or start from
  `profile/verticals/example_primary/resume_example_primary.md`. Everything
  in it must be attested in `profile/bullets.md`.

- **`rubric.md`** — mirror the existing files' shape (read
  an existing lane's `profile/verticals/<name>/rubric.md` as the reference;
  the committed
  `profile/verticals/example_*/rubric.md` shows the neutral template): a
  header stating who reads it, then the four axes — `title_match` tiers
  (which exact titles hit which band, out-of-lane → 0), `jd_skill_overlap`
  (this vertical's `skill_weights` as the anchor, standard bands, plus any
  lane-specific soft signals), `seniority_fit` (usually identical heuristic
  across verticals; note that rows over `max_years` are pre-screened out),
  `domain_bonus` tiers — plus any LLM-judged caps and an "Additional
  self-check items" section. Every tier anchored to the user's REAL evidence
  from Stage 0; interview what makes a top-band vs bottom-band match.
- **`tailoring.md`** — same reference pattern: page-budget hard floor
  (bullet mix summing to ≥10 non-frozen bullets), project ordering default
  + JD fine-tune allowances, summary framing (what leads, what supports),
  skills category-line order, WORK EXPERIENCE vs PROJECTS section order.
  All under the binding meta-rule: vertical sets the default, JD fine-tunes,
  never the reverse.

Verify:

```bash
uv run python -m src.verticals   # must now pass fully
```

## Stage 4 — verify against the tests

**Do NOT touch `tests/**/fixtures/verticals.yaml`.** They are synthetic
(`example_primary/secondary/tertiary`) and deliberately do not track the real
config; copying a real block in would put live strategy into a public file.
A new vertical needs no fixture change.

`tests/test_real_config_drift.py` runs against `profile/verticals.yaml` when
present and is what covers the new vertical: it fails if any of its
`search_terms`/`linkedin_terms` misclassifies (the Stage 2 requirement), if
its `rubric.md`/`tailoring.md`/`resume_file` are missing, if its `scored_by`
stamp collides with another lane's, or if a `title_exclude_terms` entry kills
one of its own search terms. Add structural assertions there if the lane
needs more — never a literal real term; that file is committed.

Verify:

```bash
uv run python -m pytest -q
```

Full suite green is a hard gate.

## Stage 5 — skills_master tagging

Propose `vertical_lean` additions **on existing `SKILL-*` entries only** in
`profile/skills_master.md` (values must be configured vertical names — the
file's header says so). Show the proposed entry list; the user approves the
set. Verify: `grep -c "vertical_lean:.*<name>" profile/skills_master.md`.

## Stage 6 — doc pointers + final audit

1. `CLAUDE.md` — extend the "Currently configured" sentence.
2. Final audit + report:
   ```bash
   uv run python -m src.verticals && uv run python -m pytest -q
   uv run python -m pytest tests/test_real_config_drift.py -q  # must not skip
   ```
5. Completion summary: every file touched, every locked decision (terms,
   rule position + collision rulings, disqualifier, rubric anchors, budget),
   anything skipped, and the user's remaining steps —
   `uv run python -m src.discovery` (their call; hits the network) to start
   filling the vertical, then `/score`. Existing rows are never
   reclassified; `/rescore --vertical <name>` only matters once the vertical
   has rows.
