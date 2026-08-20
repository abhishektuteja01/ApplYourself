---
description: Mock interviewer that drills you on your own resume. Pass a job_id for a role-specific loop against that role's JD and tailored resume, or a vertical name for a JD-independent pass over profile/bullets.md. Depth-ladder questioning — pushes deeper until an answer thins out, then pivots to a new area, like a real interviewer. Persists a cross-session gap report that every later session reads first, and proposes profile edits for undocumented work it surfaces. Never writes to bullets.md, skills_master.md or state.yaml.
model: opus
effort: high
allowed-tools:
  - Bash
  - Read
  - Write
  - Glob
  - Grep
  - AskUserQuestion
argument-hint: "<job_id | vertical>"
---

# /interview — drill the user on their own claims

The pipeline generates claims and never tests whether the user can defend them out
loud. A bullet that survives NO-FAB on paper can still collapse under one follow-up,
and `/tailor` knowingly ships resumes where a JD's headline demand has no attributable
bullet behind it. This command finds those spots before an interviewer does.

Two modes, chosen by the argument:

- **role mode** — `$ARGUMENTS` is an 8-hex job_id. Drills the claims that actually
  landed on the resume sent to that company, against that role's JD.
- **vertical mode** — `$ARGUMENTS` is a vertical name. JD-independent pass over
  `profile/bullets.md`, biased toward claims never drilled before.

---

**Before anything else, read `.claude/shared/no_fab.md`.** This command cites NO-FAB,
NO-DRIFT and REPHRASE-LICENSE by name; their definitions live there.

## Contract (binding)

- **6-8 questions per session, hard cap.** A thread that dies early frees budget for
  another thread; it never extends the session. Count every question you ask the user,
  including follow-ups.
- **A ladder goes at most 4 deep.** Depth 1 opens the thread, depths 2-4 push. Abandon
  on the first thin answer — do not rescue it, do not hint, do not ask it a second way.
- **No coaching, scoring or feedback until the debrief.** Not even "good answer". A
  neutral acknowledgement and the next question, nothing else.
- **One question per turn, then stop and wait.** Never stack two questions in one
  message, and never answer your own question.
- **Never invent an achievement inside a question.** Questions probe what the profile
  claims. Asking "how did you scale that to ten million rows?" when no bullet says so
  puts a fabrication in the user's mouth — that is NO-FAB applied to questions.
- **The user's answers are evidence, not claims.** Nothing the user says in a session
  enters `bullets.md` or `skills_master.md` from this command. It goes to an unlocks
  file for them to review.

## Step 0 — resolve the argument

```bash
cd "$(git rev-parse --show-toplevel)" || { echo "ERROR: not inside the repo."; exit 1; }
```

An 8-hex-character argument is a job_id; anything else is a vertical name. No argument:
read the configured lanes and ask which one with `AskUserQuestion`.

Read the valid lane names from config — never hardcode them:

```bash
uv run python -c "
from src.verticals import get_config
c = get_config()
print('lanes:', ' '.join(sorted(c.verticals)))
print('default:', c.default_vertical)
"
```

**Vertical mode:** if the name is not a configured lane, print the lane list and stop.

**Role mode:**

```bash
JOB_ID="<the 8-hex id>"
sed -n '1,40p' "pipeline/$JOB_ID/state.yaml"
```

From that state file take `vertical`, `company`, `title`, and the **last** entry of
`tailored_dirs[]` — the same resolution `src/apply_cli.py:resolve_out_dir` uses. Join
it to `applications/` to get the role's output dir.

If `state.yaml` is missing, or `tailored_dirs[]` is empty, **stop**. The role was never
tailored, so there is no JD snapshot and no landed-claim list; interviewing JD-blind
would invent the questions. Tell the user to run `/tailor <job_id>` first, or to run
`/interview <vertical>` instead.

State is **not** an eligibility gate. Any role with a tailored dir is drillable —
prep happens before a screen is booked, not after.

## Step 1 — read the gap report

```bash
ls -la interview/gap_report.md 2>/dev/null || echo "no gap report — first session"
```

If it exists, read it. Then **reconcile every row against the profile** before using it:

- Row keyed on a `B-` id no longer present in `profile/bullets.md` → delete the row
  silently. The bullet is gone; the finding is moot.
- Row whose `claim` snippet is no longer a substring of that bullet's `canonical` →
  set verdict `stale` and clear `history`. The old verdict is about text that no longer
  exists, so it means nothing.
- `X-` rows are never reconciled this way; they have no bullet by definition.

Report the carry-over in at most two lines: how many open rows, and the two or three
you intend to target. If the file is absent, say "first session" and instantiate it in
Step 5 rather than erroring.

## Step 2 — build the target list

Open gaps outrank fresh targets, always. Fill the rest of the session from the mode.

**Role mode.** Read three files from the role's output dir:

- `trace.md` — the primary target list, and it is already sorted by risk for you. Each
  resume line carries a `source=` and a `transformation=`. Read all four classes:

  - `source=B-<CTX>-NN transformation=unchanged` — a canonical claim that landed
    verbatim. A normal target. The user should be able to defend these.
  - `source=B-<CTX>-NN transformation=rephrase` — a `drift` target. The `before:` and
    `after:` lines show exactly which words the resume swapped in under
    REPHRASE-LICENSE, and `synonyms:` names the license used. The user has to hold the
    conversation in the *resume's* vocabulary, not the canonical's. Ask in the resume's
    words and see whether the answer keeps up.
  - **`source=UNATTRIBUTED`** — the highest-priority targets in the file. These are
    lines on the resume this company received with **no bullet behind them at all**.
    Many carry an explicit `DRIFT:` block naming the exact phrases that "appear in NO
    bullet and NO synonym", or a `note:` explaining what was verified against something
    other than `bullets.md`. Those named phrases are the question. An interviewer
    reading this resume will ask about them, and there is no canonical claim to fall
    back on. Each becomes an `X-` `no-anchor` row keyed to the phrase, not the line
    number — line numbers move when the resume is re-rendered.
  - `source=summary` and `source=SKILLS` — composite lines assembled from several
    bullets. Target the underlying bullets, not the line.

  A resume can carry many `UNATTRIBUTED` lines. Do not try to cover them all in one
  6-8 question session; take the ones the JD actually cares about, and leave the rest
  in the gap report for next time.
- `keywords_to_mirror.md` — the JD's real demands, and the highest-value input for
  this command. **Read it whole; do not pattern-match it.** Per keyword it records
  what supports the claim, and where support is thin it says so in prose whose wording
  varies from run to run — "NOT claimed in any bullet", "surfaces only in Skills",
  "the retrieval internals have no source in `profile/`", "that line is unattributed".
  There is no guaranteed field and no stable phrase to grep, so judge each entry:

  - A keyword whose only support is a **Skills line** with no `B-` id behind it is an
    `X-` `no-anchor` target. The resume asserts it; no claim attests it.
  - A keyword the file explicitly says is unattested, weakened, or has internals with
    no source is an `X-` `no-anchor` target **named at the level of the gap** — the
    unanchored thing is the retrieval internals, not the pipeline the bullet does
    attest. Ask about the internals.
  - A keyword with a real `B-` id behind it is a normal target, not `no-anchor`.

  These are the most likely place a real loop goes wrong: an interviewer reads the same
  JD, asks the obvious question, and the user has nothing under it. If the file records
  no thin support at all, that is a valid outcome — take the session's targets from
  `trace.md` instead and say so rather than manufacturing an `X-` row.
- `jd_snapshot.md` — the posting itself, for the seniority framing and the domain.

**Vertical mode.** Read `profile/bullets.md` and `profile/skills_master.md`:

- Bullets whose `tags` fit the lane.
- Plus bullets named in the `evidence:` of any `SKILL-*` whose `vertical_lean` includes
  the lane. Bullets carry no `vertical_lean` of their own — only `tags`; the lean field
  lives on `skills_master.md`.
- Bias hard toward ids absent from the coverage ledger. A never-drilled bullet outranks
  a bullet that already held once.

Read the lane's `rubric.md` in both modes for what that vertical weights and its target
seniority band — it tells you the altitude to pitch at. A lane targeting analyst level
gets asked how, not how-many-reports.

Do not tell the user the target list. They should not know which claim you are walking
toward.

## Step 3 — run the ladders

Open a thread on one target. Push deeper each turn: the mechanism behind the claim, the
decision the user owned, the alternative they rejected, the thing that went wrong.

Pivot the moment an answer thins out. Thin means: a generality where a specific belongs
("it was generally pretty slow"), a passive construction hiding who did it, restating
the bullet back at you, or a number they cannot source. Note the **pivot trigger** —
the exact shape of the failure — and open a new thread on an unrelated target. Do not
comment on the pivot.

Classify each closed thread with exactly one verdict:

| verdict | means |
|---|---|
| `undefended` | Could not back the claim with specifics. The claim itself is at risk. |
| `delivery` | Claim is sound and anchored; the answer buried it. Wording, not substance. |
| `drift` | Resume rephrased this bullet under license and the user cannot hold the conversation in the rephrased vocabulary. |
| `no-anchor` | What the user said to defend it is not in `bullets.md` at all. Feeds the unlocks file. |
| `held` | Defended cleanly. One `held` is luck; two closes the row. |

`stale` is set by reconciliation in Step 1, never by a thread.

## Step 4 — debrief

Only now does the user get feedback. Per thread: the verdict and one line on why.

Then, **for weak threads only** (`undefended`, `delivery`, `drift`), write the answer
they should have given. This is the deliverable, so bind it hard:

- Every content word must trace to the cited bullet's `canonical` text plus that
  bullet's `allowable_synonyms`. That is REPHRASE-LICENSE, unchanged.
- Cite the bullet ids the answer draws on, under the answer.
- No metric, tool, scope or date the profile does not already attest. If the strong
  answer would need a number that is not in a bullet, the answer is "I would need to
  check the exact figure" — say that instead of inventing one.
- A `no-anchor` thread gets no model answer. It gets an honest scope-limiting line:
  what the user *can* claim, and where the boundary is. Never a workaround that implies
  the unanchored thing.
- A thread that `held` gets no model answer. There is nothing to fix.

No numeric scores anywhere, in the debrief or the artifacts.

## Step 5 — write the artifacts

Three files under `interview/`. The directory is gitignored; `Write` creates it.

```
interview/gap_report.md                          # rewritten in place every session
interview/sessions/<date>_<vertical>[_<job_id>].md   # append-only, never rewritten
interview/unlocks_<date>.md                      # only when non-empty
```

Same-day collisions: a session file gets a `-2`, `-3` suffix. `unlocks_<date>.md` is
**appended** to under a new `## Session <n>` heading rather than spawning a second
file, so one day's review stays one file.

`gap_report.md` is the only artifact a later session reads. Sessions and unlocks are
human-facing archive — this command must run correctly if they are deleted.

### gap_report.md

```markdown
# Interview gap report

_Working memory for `/interview`. Judgment calls only — anything inferable from
`profile/bullets.md` or `profile/skills_master.md` belongs there, and the bullet is the
authority when the two disagree._

last_session: <session filename stem>
sessions_run: <n>
lap: <n>

**history** — verdicts BEFORE the current one, oldest first, max 3, single letters
(`F`=undefended `D`=delivery `R`=drift `N`=no-anchor `H`=held). Older is discarded.

## Drill next

Worst-first: `stale`, then `no-anchor` on a JD headline demand, then `undefended`,
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

### sessions/<date>_<vertical>[_<job_id>].md

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

### unlocks_<date>.md

Write it only when the session actually surfaced undocumented work. This is the
memory-unlock half of the command, and it follows `/suggest-synonyms`' discipline: you
never write to `profile/bullets.md` or `profile/skills_master.md` — the user does,
after reviewing.

Deliberately a separate file from `/suggest-synonyms`' own draft. That one is driven by
JD keyword frequency and asks "does this vocabulary map to a claim you already have".
This one asks "you said this out loud under pressure; is it real?" — different
provenance, different review question.

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

## Step 6 — report

Print the three paths, the carry-over count, and anything evicted under the overflow
rule.

Then, if the role's state should move: **tell the user to run
`/track <job_id> screen`** (or `interview`) themselves. Do not attempt it. `/track` is
the sole writer of state transitions (R10), and a direct write to
`pipeline/<job_id>/state.yaml` is blocked by a hook — a session that tries it dies at
the very end, after the interview is already spent.
