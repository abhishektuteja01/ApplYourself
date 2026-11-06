# /suggest-synonyms — draft file templates

Read by `/suggest-synonyms` Step 3 (not in `--auto`). One draft file,
`profile/synonyms_draft_YYYY-MM-DD.md`, holds every section below in order.

## Track 1 (Step 3)

```markdown
# Synonym & Skills Suggestions — <today's date>

Generated from the top keywords of the N roles with no resume yet.
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

## Track 2 (Step 3b)

```markdown
---

## Memory unlocks — confirm before adding

Tools and concepts you likely touched (across every context in `bullets.md`)
but haven't documented. **For each: answer yes/no.
If yes, decide whether to add it as a synonym to an existing bullet, or flag
it for a new bullet entirely.**

| tool / concept | context | confidence | why you probably touched it | which bullet to extend (or "new bullet") | question to confirm |
|---|---|---|---|---|---|
| <tool> | <the context, e.g. DEL> | likely | <what in that context's bullets makes this near-certain> | <B-ID> (<why that bullet>) | <a yes/no question naming the tool and where it would have been used> |
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

## Track 3 (Step 3c)

```markdown
---

## Track 3 — skills_master.md gap-fill (ready to add)

New `allowable_synonyms` aliases mapping a JD keyword onto an EXISTING skill
entry. Copy the patch into the corresponding `SKILL-*` block in
`profile/skills_master.md` after review.

### SKILL-<ID> (<skill name>)
**Add to `allowable_synonyms`:**
- "phrase" — maps from JD keyword "X" (seen N times across shortlist)

(repeat per skill — only include skills that have new alias suggestions)

---

## Track 3 — skills_master.md memory unlock — confirm before adding

Tools/libraries you likely used but haven't entered as a `SKILL-*` block.
**For each: answer yes/no. If yes, add a new `SKILL-*` block yourself** (this
command never writes a new skill entry directly — same discipline as bullets).

| tool / library | Skills line | anchor evidence | confidence | question to confirm |
|---|---|---|---|---|
| <library> | <the Skills line it would show on> | <B-ID> (<the claim it backs>) | likely | Did you use <library> (or an equivalent) for <the specific job it would have done> in <context>? |
| ... | ... | ... | ... | ... |
```

Cap the memory-unlock table at 8 rows — rank by how directly the tool would be
visible to a recruiter scanning the Skills section (concrete library/tool names
over vague process terms).
