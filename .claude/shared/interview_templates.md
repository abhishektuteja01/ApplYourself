# /interview — artifact templates and rules

Read by `/interview` Step 5. The three files live under `interview/`.

## gap_report.md

```markdown
# Interview gap report

_Working memory for `/interview`. Judgment calls only — anything inferable from
`profile/bullets.md` or `profile/skills_master.md` belongs there, and the bullet is the
authority when the two disagree._

last_session: <session filename stem>
sessions_run: <n>
lap: <n>

**history** — verdicts BEFORE the current one, oldest first, max 3, single letters
(`F`=undefended `D`=delivery `R`=drift `N`=no-anchor `T`=story-thin `H`=held). Older is
discarded.

## Drill next

Worst-first: `stale`, then `no-anchor` on a JD headline demand, then `undefended`,
then `story-thin`,
then `drift`, then `delivery`. Max 20 rows.

| key | claim (<=6 words from canonical) | verdict | history | last drilled | vertical | note |
|---|---|---|---|---|---|---|
| B-WID-03 | rebuilt ledger reconciliation in SQL | undefended | F,D | 2026-01-14 | example_primary | no baseline for the three-day close; couldn't name the manual step |
| B-WID-07 | gizmo throughput dashboard | drift | — | 2026-01-14 | example_primary | resume says "analytics pipeline"; couldn't discuss it in those terms |
| X-sprocket-orchestration | sprocket orchestration | no-anchor | — | 2026-01-14 | example_primary | JD headline demand, Skills-only claim; needs an honest boundary line |
| B-COG-01 | cog rollout knowledge transfer | delivery | H | 2026-01-14 | example_secondary | claim holds; buried the outcome under three minutes of setup |

## Closed

Proven twice. Do not drill again until pruned. Max 15 lines.

- B-WID-01 — closed 2026-01-07 after 3 drills (F,H,H)

## Coverage ledger

Bullet ids drilled at least once **this lap**. The universe is not stored — recompute
it from `profile/bullets.md`; the never-drilled pool is the difference.

drilled: B-COG-01 B-WID-01 B-WID-03 B-WID-07
```

Keys are **stable identifiers**: `B-<CTX>-NN` ids, which this repo never renumbers, or
a `SKILL-*` id. A claim with no bullet behind it — a JD demand, or something the user
said out loud — gets a synthetic `X-<kebab-slug>` key derived from the claim itself, so
it is stable across sessions. Replace it with the real `B-` id if that unlock is ever
accepted into `bullets.md`.

**Compaction. The gap report is a worklist, not a log** — that is what keeps it a
30-second read after fifty sessions. History lives in the session files.

1. **Close a row** when the verdict is `held` *and* the rightmost history letter is
   `H`. Move it to Closed as `<key> — closed <date> after N drills (<letters>)`. A
   single `held` never closes a row.
2. **Prune Closed** when it exceeds 15 lines (oldest first), or when a line is over 120
   days old. After four months a retest is legitimate, and dropping the line returns
   that bullet to the never-drilled pool on its own.
3. **Overflow past 20 open rows:** evict oldest `delivery` first, then `no-anchor` rows
   already written into an unlocks file. **Never evict an `undefended` row** — it is the
   only class where dropping the row loses the finding entirely. Name what you evicted
   in the session report; never drop silently.
4. **Lap reset:** when the drilled set covers every `B-` id in `bullets.md`, clear
   `drilled:` and increment `lap`. Closed lines survive a lap boundary.

**Known limit, do not paper over it:** the ledger records what was *asked*, not what is
*covered*. A bullet drilled once at depth 1 and one drilled four times to depth 4 look
identical in it. So the pool can drain while real coverage is one shallow datapoint per
bullet. The Drill-next table keeps depth-bearing detail for anything that went badly,
but a shallow `held` leaves no trace once its row closes and the Closed line is pruned.
If the user asks how well covered they are, say this rather than reporting the ledger
as if it were coverage.

## sessions/<date>_<vertical>[_<job_id>].md

```markdown
# Interview session — <date>

mode: role
job_id: <8-hex>
vertical: <lane>
role: <company> — <title>
jd_source: applications/<vertical>/<dir>/jd_snapshot.md
threads: 3
questions_asked: 7
opened_from_gap_report: B-WID-03, B-COG-01

## Thread 1 — B-WID-03

**opened:** "<the question>"
**ladder depth:** 3 follow-ups, then pivoted.
**pivot trigger:** <the exact shape of the failure>
**verdict:** undefended
**why:** <one line>

**model answer** (B-WID-03 canonical + its allowable_synonyms only):
> <2-4 sentences>
> sources: B-WID-03

## Debrief

- Held: <keys>
- Delivery only: <keys>
- Undefended: <keys>
- Off-bullet material surfaced: <n> items -> interview/unlocks_<date>.md

## Gap report delta written

- B-WID-03: undefended, history F,D
- X-sprocket-orchestration: new row, no-anchor
- coverage ledger += B-WID-03
```

**`pivot trigger` is the most valuable line in the file.** It is the specific shape of
the failure, and it is what makes a retest sharper than the first drill. `ladder depth`
earns its line because a `held` at depth 1 and a `held` at depth 4 are different
findings.

**Do not persist:** verbatim turn-by-turn Q&A — it is bulky, never re-read, and the
most PII-dense thing this pipeline would produce; your reasoning about how you picked
targets; any encouragement; the JD text (cite the path); the bullet's canonical text
(cite the id); model answers for threads that held. Condense answers to their
substance. The next session needs what was and was not demonstrated, not a recording.

## unlocks_<date>.md

Write it only when the session actually surfaced undocumented work. This is the
memory-unlock half of the command, and it follows `/suggest-synonyms`' discipline: you
never write to `profile/bullets.md`, `profile/skills_master.md` or
`profile/stories.md` — the user does,
after reviewing.

It is a separate file from `/suggest-synonyms`' draft.

Cap 8 rows total. A session is 6-8 questions; more than 8 rows is padding.

```markdown
# Interview unlocks — <date>

From session `<session stem>`. Things you said defending a bullet that are **not
currently in `profile/bullets.md` or `profile/skills_master.md`**.

**This file never edits the profile.** Confirm each row, then you make the edit. Only
add what you can stand behind on a call — these become attested claims.

NO-FAB note: none of this material appeared in this session's model answers. A model
answer may use only text traceable to an existing bullet's `canonical` plus its
`allowable_synonyms`; anything you said beyond that lands here instead.

## Extend an existing bullet

| claim as you said it | source context | anchor | why it isn't covered today | proposed action | confirm |
|---|---|---|---|---|---|

## New bullet candidates

| claim as you said it | source context | why no existing bullet fits | overlaps | confirm |
|---|---|---|---|---|

## skills_master candidates

| tool / concept | category | evidence it would cite | confirm |
|---|---|---|---|

## Not proposing

Claims that surfaced but would be drift if written down — analogy is not equivalence
(NO-DRIFT). Recorded so the call is visible rather than silently swallowed.

| claim | why not |
|---|---|
```

Rules for the tables:

- `source context` is the `source:` value of the context the claim belongs to, so the
  user knows which employment or project it attaches to.
- Every row ends in a **question**, never an assertion. You are proposing a hypothesis
  about work the user described; they rule on it. Same rule as `/suggest-synonyms`
  Track 2: never assert the user did something.
- Any draft claim text may only restate what the user said in this session. It carries
  no metric they did not state, and it is marked a draft pending their confirmation.
- Only material from **this** session goes in. Do not mine `bullets.md` for adjacent
  tooling the user never mentioned — that is `/suggest-synonyms`' job, and mixing the
  two would blur which claims the user actually confirmed out loud.
