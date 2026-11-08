---
description: Mock interviewer that drills you on your own resume claims, for a job_id or a lane. Keeps a gap report; never writes profile/ or state.yaml.
model: opus
effort: high
disable-model-invocation: true
allowed-tools:
  - Bash(cd *)
  - Bash(git rev-parse *)
  - Bash(uv run config verticals*)
  - Bash(uv run app dir *)
  - Bash(sed -n *)
  - Bash(ls *)
  - Read
  - Write
  - Glob
  - Grep
  - AskUserQuestion
argument-hint: "<job_id | vertical>"
---

# /interview — drill the user on their own claims

Find the claims the user cannot defend out loud before an interviewer does.

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
uv run config verticals
```

**Vertical mode:** if the name is not a configured lane, print the lane list and stop.

**Role mode:**

```bash
cd "$(git rev-parse --show-toplevel)" && uv run app dir <job_id>
sed -n '1,40p' "pipeline/<job_id>/state.yaml"
```

`dir` is the role's output dir and `vertical` its lane. From the state file take
`company` and `title`.

If `state.yaml` is missing, or `dir` is empty, **stop**. The role was never
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

- Row keyed on a `B-` id no longer present in `profile/bullets.md`, or an `S-` id no
  longer present in `profile/stories.md` → delete the row
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

**Role mode.** Read two files from the role's output dir:

- `trace.md` — written by code, one entry per resume line:

  - `source=B-<CTX>-NN transformation=unchanged` — a canonical claim that landed
    verbatim. A normal target. The user should be able to defend these.
  - `source=B-<CTX>-NN transformation=rephrase` — a `drift` target. The `before:` and
    `after:` lines show exactly which words the resume swapped in under
    REPHRASE-LICENSE. The user has to hold the conversation in the *resume's*
    vocabulary, not the canonical's. Ask in the resume's words and see whether the
    answer keeps up.
  - `source=summary transformation=written` — a summary written for this JD. Its
    `uses:` names the bullets its facts come from: target those claims in the
    summary's words.
  - `source=SKILL-A,SKILL-B transformation=skills` — a Skills line. Every skill on it
    is a claim the interviewer can pick up. A skill the JD names is a target, and
    its `evidence:` in `profile/skills_master.md` names the bullets that must hold it.
  - `## Keywords` — every `keywords_to_mirror` entry, with the lines it landed on (in any spelling) or
    `not landed`. **A keyword the JD demands that did not land is the highest-value
    target**: an interviewer reads the same JD, asks the obvious question, and the
    resume has nothing under it. Each becomes an `X-` `no-anchor` row keyed to the
    keyword, not a line number. A keyword that landed only on a Skills line is
    `no-anchor` too unless a bullet behind that skill holds it.

  Folders tailored before the builder carry a hand-written trace and a separate
  `keywords_to_mirror.md` (`source=UNATTRIBUTED`, `DRIFT:` blocks). Read those the
  same way: an unattributed line or a named drift phrase is a `no-anchor` target.

  Do not try to cover every target in one 6-8 question session; take the ones the JD
  actually cares about, and leave the rest in the gap report for next time.
- `jd_snapshot.md` — the posting itself, for the seniority framing and the domain.

**Vertical mode.** Read `profile/bullets.md`, `profile/skills_master.md` and
`profile/stories.md` if it exists (optional file):

- Bullets whose `tags` fit the lane.
- Plus bullets named in the `evidence:` of any `SKILL-*` whose `show` includes the
  lane, and bullets whose `default_for` includes it.
- Plus `S-` entries from `stories.md` whose `anchors:` name any bullet already in the
  pool. A story is drilled as a claim in its own right: press on the mechanism in its
  `action`, the number in its `outcome`, and whether the `retrospect` survives being
  asked what it would cost. A story whose anchor bullet held but whose own mechanism
  does not is exactly the gap this file exists to catch.
- Bias hard toward ids absent from the coverage ledger. A never-drilled bullet or story
  outranks one that already held once.

Read the lane's `rubric.md` in both modes for what that vertical grades highest and
which titles it reads as senior — it tells you the altitude to pitch at. A lane targeting analyst level
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
| `no-anchor` | What the user said to defend it is not in `bullets.md` or `stories.md` at all. Feeds the unlocks file. |
| `story-thin` | An `S-` row only. The story's situation and outcome hold, but the mechanism in `action` or the cost in `retrospect` does not. The story is at risk, not the bullet it anchors to. |
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

Read `.claude/shared/interview_templates.md` now: it holds each file's template, the
gap report's key and compaction rules, what not to persist, and the unlocks table
rules. Follow it exactly.

## Step 6 — report

Print the three paths, the carry-over count, and anything evicted under the overflow
rule.

Then, if the role's state should move: **tell the user to run
`/track <job_id> screen`** (or `interview`) themselves. Do not attempt it. `/track` is
the sole writer of state transitions (R10), and a direct write to
`pipeline/<job_id>/state.yaml` is blocked by a hook — a session that tries it dies at
the very end, after the interview is already spent.
