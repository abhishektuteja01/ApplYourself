---
description: Three-track synonym/skills audit. Track 1 — gap-fill allowable_synonyms from shortlist JD keywords (profile/bullets.md). Track 2 — memory unlock prompts for adjacent tools/concepts the user likely touched but hasn't documented, across every context in bullets.md (each employment and each project). Track 3 — same gap-fill + memory-unlock pattern applied to profile/skills_master.md. Never writes to bullets.md or skills_master.md directly.
model: sonnet
effort: medium
allowed-tools:
  - Bash
  - Read
  - Write
---

# /suggest-synonyms — gap-fill + memory unlock for bullets.md and skills_master.md

This command runs three tracks:

**Track 1 — JD gap-fill (bullets):** audits the gap between shortlist JD vocabulary and
`allowable_synonyms` already in `profile/bullets.md`. Proposes new synonym entries
that let `/tailor` mirror high-frequency JD terms.

**Track 2 — memory unlock (bullets):** independently reasons about tools, interfaces, and
processes the user *likely touched but hasn't documented*, given the specifics of
each context in `profile/bullets.md` — every distinct `source:` value is one
context (an employment, a teaching role, a personal project). These are surfaced as
**confirm-before-adding prompts** — the user says yes/no, then decides whether to
add a new bullet or extend an existing one.

**Track 3 — skills_master gap-fill + memory unlock:** same two-part pattern applied
to `profile/skills_master.md` instead of `profile/bullets.md`. Gap-fill checks
shortlist JD keywords against existing skill entries/`allowable_synonyms` and
proposes new alias entries. Memory unlock reasons about specific tools/libraries
the user likely used (across every `bullets.md` context) but hasn't entered as a
`SKILL-*` block.

The key distinction across tracks:
- Track 1/3-gapfill = "you did X; here's better JD vocabulary for it"
- Track 2/3-unlock = "you probably touched Y given your role/projects; did you? If yes, add it"

**Hard constraints (same as /tailor's NO-FAB / NO-DRIFT):**
- A synonym must re-package the SAME underlying process or claim.
- **Analogy is not equivalence.** A specialized process is not its generic
  industry cousin, and integration-level exposure to a component is not
  configuring or owning it. Never propose a relabel that widens scope, even when
  the JD's vocabulary invites it. Each bullet's `canonical` text plus `evidence`
  field is the outer bound of what it may be restated as; the vertical's
  `tailoring.md` names any domain-specific relabels that are banned outright.
- Memory-unlock suggestions are hypotheses only — never assert the user did something;
  always frame as a question ("Did you use X? If yes, this would be addable.")
- If a JD keyword has no honest anchor, write `UNMAPPABLE` — do not invent.
- Track 3 skill suggestions must trace to evidence already in `bullets.md` or to a
  confirmed Track 2/3 memory-unlock answer — never invent a skill with no anchor.

Output is a draft file for the user to review. You never write to `profile/bullets.md`
or `profile/skills_master.md` directly.

---

**Before anything else, read `.claude/shared/no_fab.md`.** This command
cites NO-FAB and REPHRASE-LICENSE by name; their definitions live there,
not in this file.

## Step 1 — load profile and today's scored shortlist

```bash
# Get today's shortlist file
SHORTLIST=$(ls -t shortlist/*.md 2>/dev/null | head -1)
echo "shortlist: $SHORTLIST"

# Extract all keywords_to_mirror from scored.parquet for shortlist rows
uv run python -c "
import json, pandas as pd
from pathlib import Path

scored = pd.read_parquet('jobs/scored.parquet')
# company lives only in clean.parquet; scored.parquet has no company column
clean = pd.read_parquet('jobs/clean.parquet', columns=['job_id', 'company'])
scored = scored.merge(clean, on='job_id', how='left')
# Only shortlist-worthy rows (fit >= 50, not ineligible)
keepers = scored[
    (scored.fit_score >= 50) &
    (scored.sponsorship_label != 'ineligible')
].copy()

# Collect all keywords
all_kw = []
for _, row in keepers.iterrows():
    kws = row.get('keywords_to_mirror', None)
    if kws is None:
        continue
    import numpy as np
    if isinstance(kws, (list, np.ndarray)):
        for kw in kws:
            kw = str(kw).strip()
            if kw:
                all_kw.append({'keyword': kw, 'job_id': row['job_id'],
                               'company': row.get('company') or '', 'fit': float(row.get('fit_score',0))})
    elif isinstance(kws, str) and kws:
        all_kw.append({'keyword': kws.strip(), 'job_id': row['job_id'],
                       'company': row.get('company') or '', 'fit': float(row.get('fit_score',0))})

# Deduplicate + count frequency
from collections import Counter
freq = Counter(item['keyword'] for item in all_kw)
# Top 40 by frequency, then by fit of first-appearing row
seen = set()
top = []
for item in sorted(all_kw, key=lambda x: -x['fit']):
    if item['keyword'] not in seen:
        seen.add(item['keyword'])
        top.append({**item, 'frequency': freq[item['keyword']]})
    if len(top) >= 40:
        break
top.sort(key=lambda x: -x['frequency'])
print(json.dumps(top, indent=2, default=str))
" > /tmp/shortlist_keywords.json
echo "keywords dumped: $(python3 -c "import json; d=json.load(open('/tmp/shortlist_keywords.json')); print(len(d))")"
```

Then `Read` these files:
- `/tmp/shortlist_keywords.json`
- `profile/bullets.md` — canonical bullets + existing `allowable_synonyms` per bullet

## Step 2 — build the gap map

For each keyword in `/tmp/shortlist_keywords.json`, do the following analysis in
your head before writing any output:

1. **Check coverage:** Is this keyword (or a close variant) already present in
   any bullet's `allowable_synonyms` list or in the canonical text of that bullet?
   If yes → mark `already_covered`. No new entry needed.

2. **Find the best anchor bullet:** Which canonical bullet, if any, describes the
   same underlying process/claim that this JD keyword refers to? Build the
   candidate index from `profile/bullets.md` itself as you read it — one entry per
   `B-*` ID, summarising that bullet's claim from its `canonical` text and `tags`.
   Do not work from a remembered index: bullets are added and reworded, so the
   file you just read is the only valid list.

3. **Honest mapping only:** The synonym must be a re-phrasing of what the bullet
   already claims — same scope, same tool, same metric. If the JD keyword implies
   a capability no bullet attests (typically a configure/own claim where the
   evidence is integration-level exposure), write
   `UNMAPPABLE — no canonical anchor`.

4. **Draft the synonym text:** A short phrase (2–6 words) using JD vocabulary that
   a reader would recognize as equivalent to the canonical claim.

## Step 3 — write the draft output

Write to `profile/synonyms_draft_YYYY-MM-DD.md` (use today's date).

Format:

```markdown
# Synonym & Skills Suggestions — <today's date>

Generated from top-N shortlist keywords in `scored.parquet` (fit ≥ 50 rows).
**Review each entry before adding to profile/bullets.md or profile/skills_master.md.**
Only add entries you can stand behind on a call — these are attested claims.

---

## Ready to add

These map cleanly to a canonical bullet. Copy the `allowable_synonyms:` patch
block into the corresponding bullet in `profile/bullets.md` after review.

### <B-ID> (<short label for that bullet's claim>)
**Add to `allowable_synonyms`:**
- "phrase one" — maps from JD keyword "X" (seen N times across shortlist)
- "phrase two" — maps from JD keyword "Y" (seen M times across shortlist)

### <next B-ID> (<short label>)
**Add to `allowable_synonyms`:**
- ...

(repeat per bullet — only include bullets that have new suggestions)

---

## Unmappable keywords

JD terms that appeared in the shortlist but have no honest anchor in any
canonical bullet. Do NOT add these as synonyms.

| keyword | frequency | reason unmappable |
|---|---|---|
| "<a configure/own claim from a JD>" | 8 | no bullet attests ownership; evidence is integration-level only |
| ...                                 |   |                                                                 |

---

## Already covered

Keywords already present in a bullet's canonical text or allowable_synonyms.
No action needed.

| keyword | frequency | covered by |
|---|---|---|
| "<keyword>" | 12 | <B-ID> canonical |
| ...         |    |                  |
```

Rules for the "Ready to add" section:
- Only include bullets where you have ≥1 genuinely new synonym to propose.
- If a bullet has no gaps, omit it entirely — don't pad with zeroes.
- Each suggestion line must show: the proposed synonym phrase AND the JD keyword
  it mirrors AND the frequency count so the user can prioritize.

Rules for "Unmappable":
- Be specific about WHY it's unmappable (drift, a fabricated scope claim, or
  "no canonical anchor").
- Do not soft-pedal fabrication — if it's a standalone module config claim, say so.

## Step 3b — Track 2: memory unlock audit

Independently of the JD keyword list, reason about the user's specific role context
and generate a list of tools/interfaces/processes they **probably encountered** but
haven't documented in any bullet's canonical text or `allowable_synonyms`.

**Build the inference base yourself from `profile/bullets.md`** — do not work from
a remembered description of the user's background. Group the bullets by their
`source:` field: each distinct value is one context (an employment, a teaching
role, a personal project). For each context, read across its bullets' `canonical`
text, `tags` and `evidence` to establish the domain, the platform or stack, the
scale, the seniority, and the period. That grouping IS the inference base, and it
stays correct as bullets are added or reworded.

Then, per context, think through: what tools, interfaces, commands and adjacent
processes does someone in this *exact* environment almost certainly encounter,
even if no bullet mentions them? Reason from the specifics you just read — a
platform implies its own tooling, an ops cadence implies ticketing, a rollout
implies methodology artifacts.

Reason along these axes, which apply to most contexts (not exhaustive, and not
every axis fits every context):

- **UI / access layer** — what the user actually clicked or typed into daily
- **Change & release management** — how a change reached production
- **Testing** — the framework or suite behind any validation/QA claim
- **Ticketing / ITSM / collaboration** — implied by any incident, defect or
  support cadence
- **Reporting & analytics** — the companion tools to any reporting claim
- **Data access** — how records were actually pulled for a reconciliation,
  investigation or analysis claim
- **Domain-specific commands/transactions/APIs** — the named operations
  characteristic of that platform
- **Methodology artifacts** — implied by any rollout, migration or transformation
- **Documentation & knowledge transfer** — implied by any KT, enablement or
  onboarding claim
- **For a software project specifically:** the dependency/library layer behind
  each claim, data ingestion and validation, the testing approach, deployment
  target, and observability

For each candidate, rate your confidence: `likely` (would be surprising if not
used given the context) vs `possible` (plausible but not universal). Only include
`likely` items in the output — omit `possible` unless it appears in the shortlist
keywords, in which case note it as lower-confidence.

Apply the same `likely`/`possible` discipline to every context, employment and
project alike — a project's dependency choices are as unlogged as an employer's
internal tooling.

Also check the shortlist keywords for any terms that fall into any context's
"memory unlock" bucket — JD terms the user likely knows from practice but hasn't
documented.

Add a section to the draft output file:

```markdown
---

## Memory unlocks — confirm before adding

Tools and concepts you likely touched (across every context in `bullets.md`)
but haven't documented. **For each: answer yes/no.
If yes, decide whether to add it as a synonym to an existing bullet, or flag
it for a new bullet entirely.**

| tool / concept | source context | confidence | why you probably touched it | which bullet to extend (or "new bullet") | question to confirm |
|---|---|---|---|---|---|
| <tool> | <the `source:` this context came from> | likely | <what in that context's bullets makes this near-certain> | <B-ID> (<why that bullet>) | <a yes/no question naming the tool and where it would have been used> |
| ... | ... | ... | ... | ... | ... |
```

Only include entries where the confidence is `likely`. Cap at 12 rows total
across all contexts — if you have more candidates, rank employment items by
shortlist keyword frequency and project items by how specific they are to that
project, then cut the tail.

For items that appear in the shortlist keywords AND are memory-unlock candidates,
add a note: `(also appears in shortlist — high value if confirmable)`.

After the user confirms items from this table, they can either:
a) Add a phrase to an existing bullet's `allowable_synonyms` (if it's a synonym
   for something already claimed), or
b) Draft a new canonical bullet for a genuinely undocumented capability — which
   is outside this command's scope; `/tailor` will prompt them when needed.

## Step 3c — Track 3: skills_master gap-fill + memory unlock

`Read` `profile/skills_master.md` (every `SKILL-*` block: `name`, `category`,
`evidence`, `allowable_synonyms`, `vertical_lean`).

**Gap-fill half:** reuse `/tmp/shortlist_keywords.json` from Step 1. For each
keyword that names a tool/technology/library (skip process-y keywords already
handled by Track 1 — those belong to bullets, not skills), check:

1. **Already covered?** Does the keyword (or a close variant) already match a
   `SKILL-*` entry's `name` or `allowable_synonyms`? If yes → no action.
2. **Honest anchor in an existing skill?** If the keyword is just a different
   display name for a skill already listed (e.g. JD says "LLM orchestration"
   and `SKILL-MULTIAGENT-LLM` already covers this underlying capability), propose
   it as a new `allowable_synonyms` entry on that skill.
3. **No anchor at all?** Do NOT propose a brand-new `SKILL-*` entry from a JD
   keyword alone — a skill entry asserts the user has used something; the
   gap-fill track only ever proposes *aliases* on existing entries, same
   no-fabrication discipline as bullets' Track 1. Route anything with no
   existing skill anchor to the memory-unlock half below instead, since that's
   the part of this command that explicitly asks the user to confirm.

**Memory-unlock half:** reason about specific tools/libraries the user likely
used but hasn't entered as a `SKILL-*` block, anchored to evidence already
established in `profile/bullets.md` (the same per-`source:` contexts you grouped
in Step 3b above) — e.g. if a Track 2 memory-unlock item gets
confirmed `yes` by the user in a prior run, or is independently inferable as
`likely` from the bullet text already on record, propose it here too. Frame
every row as a yes/no question, never an assertion.

Add a section to the draft output file:

```markdown
---

## Track 3 — skills_master.md gap-fill (ready to add)

New `allowable_synonyms` aliases mapping a JD keyword onto an EXISTING skill
entry. Copy the patch into the corresponding `SKILL-*` block in
`profile/skills_master.md` after review.

### SKILL-MULTIAGENT-LLM (Multi-Agent LLM Systems)
**Add to `allowable_synonyms`:**
- "phrase" — maps from JD keyword "X" (seen N times across shortlist)

(repeat per skill — only include skills that have new alias suggestions)

---

## Track 3 — skills_master.md memory unlock — confirm before adding

Tools/libraries you likely used but haven't entered as a `SKILL-*` block.
**For each: answer yes/no. If yes, add a new `SKILL-*` block yourself** (this
command never writes a new skill entry directly — same discipline as bullets).

| tool / library | category | anchor evidence | confidence | question to confirm |
|---|---|---|---|---|
| <library> | <a skills_master category> | <B-ID> (<the claim it backs>) | likely | Did you use <library> (or an equivalent) for <the specific job it would have done> in <context>? |
| ... | ... | ... | ... | ... |
```

Cap the memory-unlock table at 8 rows — rank by how directly the tool would be
visible to a recruiter scanning the Skills section (concrete library/tool names
over vague process terms).

## Step 4 — report

Tell the user:
- Path to the draft file
- Track 1: N ready-to-add synonyms across M bullets, K unmappable keywords
- Track 1: top 3 highest-frequency unmappable keywords (market wants this; profile can't claim it)
- Track 2: X memory-unlock prompts (Y flagged as "also in shortlist — high value if confirmable"),
  broken out by `source:` context
- Track 3: P ready-to-add skill aliases across Q skills, R memory-unlock prompts
  for skills_master.md
- Reminder: "Answer the yes/no questions in the Memory unlocks tables first — that's
  the fastest path to new claimable vocabulary and new skill entries. Then review
  the Ready-to-add synonyms/aliases. Only add what you can stand behind on a
  recruiter call. New skills_master.md entries are written by you, not this command."
