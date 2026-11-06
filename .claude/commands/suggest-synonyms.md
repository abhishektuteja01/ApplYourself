---
description: Three-track synonym/skills audit. Track 1 — gap-fill allowable_synonyms from shortlist JD keywords (profile/bullets.md). Track 2 — memory unlock prompts for adjacent tools/concepts the user likely touched but hasn't documented, across every context in bullets.md (each employment and each project). Track 3 — same gap-fill + memory-unlock pattern applied to profile/skills_master.md. Never writes to bullets.md or skills_master.md itself: `uv run synonyms apply` does. `--auto` is the unattended nightly form.
argument-hint: "[--auto]"
model: sonnet
effort: medium
allowed-tools:
  - Bash(cd *)
  - Bash(git rev-parse *)
  - Bash(uv run apply sheet *)
  - Bash(uv run synonyms *)
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
each context in `profile/resume.yaml` `contexts:` (an employment, a teaching
role, a personal project); a bullet belongs to the context its id names (`DEL`
in `B-DEL-01`). These are surfaced as
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
  field is the outer bound of what it may be restated as, and NO-DRIFT rules out
  relabeling a specialized process as its generic cousin.
- Memory-unlock suggestions are hypotheses only — never assert the user did something;
  always frame as a question ("Did you use X? If yes, this would be addable.")
- If a JD keyword has no honest anchor, write `UNMAPPABLE` — do not invent.
- Track 3 skill suggestions must trace to evidence already in `bullets.md` or to a
  confirmed Track 2/3 memory-unlock answer — never invent a skill with no anchor.

Output is a draft file for the user to review (`--auto`: a proposals file that
`uv run synonyms apply` checks and writes). You never write to `profile/bullets.md`
or `profile/skills_master.md` yourself.

## `--auto` (nightly, nobody to ask)

Runs Steps 1, 2, the Track 3 gap-fill and the memory-unlock reasoning, then writes
only `jobs/synonyms_proposals.yaml` and runs `uv run synonyms apply`. Skip Step 3's
draft file, the tables and Step 4. Print apply's output and stop.

```yaml
ready:                      # Track 1 rephrasings + Track 3 aliases
  - {target: B-XXX-NN | SKILL-XXX, phrase: "2-6 words", keyword: "the JD keyword"}
declined:                   # keywords no bullet or skill covers
  - {keyword: "...", reason: "one short clause"}
unlock:                     # at most 5, `likely` only
  - {kind: bullet | skill, thing: "the tool or step", context: "the job or project", via: "where you probably used it"}
```

Lenient: propose a phrase whenever the bullet or skill plausibly covers the keyword
in the same tool or claim; decline only when nothing does. A phrase never claims more
than the bullet's `canonical` and `evidence` (NO-DRIFT). Code drops duplicates and
unknown targets, so do not check them yourself.

---

**Before anything else, read `.claude/shared/no_fab.md`.** This command
cites NO-FAB and REPHRASE-LICENSE by name; their definitions live there,
not in this file.

## Step 1 — the roles with no resume, and their keywords

```bash
cd "$(git rev-parse --show-toplevel)" || { echo "ERROR: not inside the repo."; exit 1; }
uv run apply sheet --add >/dev/null
uv run synonyms keywords > jobs/synonyms_keywords.json
```

`keywords` are the top 40 `keywords_to_mirror` of the roles with no resume yet,
already without the keywords declined before. No roles or no keywords: say so and stop.

Then `Read` these files:
- `jobs/synonyms_keywords.json`
- `profile/bullets.md` — canonical bullets + existing `allowable_synonyms` per bullet

## Step 2 — build the gap map

For each entry of `keywords` in `jobs/synonyms_keywords.json`, do the following analysis in
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

Read `.claude/shared/synonyms_templates.md` now: it holds the draft file's
sections for every track and the rules for each. Write the Track 1 sections here.

## Step 3b — Track 2: memory unlock audit

Independently of the JD keyword list, reason about the user's specific role context
and generate a list of tools/interfaces/processes they **probably encountered** but
haven't documented in any bullet's canonical text or `allowable_synonyms`.

**Build the inference base from `profile/bullets.md`, not from memory.** Group
the bullets by context (the id prefix; its header is in `profile/resume.yaml`
`contexts:`). Per context, read
across its bullets' `canonical`, `tags` and `evidence` for the domain, stack,
scale, seniority and period.

Then ask, per context: what tools, interfaces, commands and adjacent processes
does someone in this *exact* environment almost certainly encounter, even if no
bullet names them? Reason from the specifics — a platform implies its tooling, an
ops cadence implies ticketing, a rollout implies methodology artifacts.

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

Rate each candidate `likely` (surprising if unused in this context) or
`possible`. Output `likely` only — include a `possible` just when it appears in
the shortlist keywords, flagged as lower-confidence.

Apply the same `likely`/`possible` discipline to every context, employment and
project alike — a project's dependency choices are as unlogged as an employer's
internal tooling.

Also check the shortlist keywords for any terms that fall into any context's
"memory unlock" bucket — JD terms the user likely knows from practice but hasn't
documented.

Add the Track 2 section from the templates file.

## Step 3c — Track 3: skills_master gap-fill + memory unlock

`Read` `profile/skills_master.md` (every `SKILL-*` block: `name`, `evidence`,
`allowable_synonyms`, `show`).

**Gap-fill half:** reuse `jobs/synonyms_keywords.json` from Step 1. For each
keyword that names a tool/technology/library (skip process-y keywords already
handled by Track 1 — those belong to bullets, not skills), check:

1. **Already covered?** Does the keyword (or a close variant) already match a
   `SKILL-*` entry's `name` or `allowable_synonyms`? If yes → no action.
2. **Honest anchor in an existing skill?** If the keyword is just a different
   display name for a skill already listed (e.g. JD says "LLM orchestration"
   and `SKILL-<ID>` already covers this underlying capability), propose
   it as a new `allowable_synonyms` entry on that skill.
3. **No anchor at all?** Do NOT propose a brand-new `SKILL-*` entry from a JD
   keyword alone — a skill entry asserts the user has used something; the
   gap-fill track only ever proposes *aliases* on existing entries, same
   no-fabrication discipline as bullets' Track 1. Route anything with no
   existing skill anchor to the memory-unlock half below instead, since that's
   the part of this command that explicitly asks the user to confirm.

**Memory-unlock half:** reason about specific tools/libraries the user likely
used but hasn't entered as a `SKILL-*` block, anchored to evidence already
established in `profile/bullets.md` (the same per-context groups you built
in Step 3b above) — e.g. if a Track 2 memory-unlock item gets
confirmed `yes` by the user in a prior run, or is independently inferable as
`likely` from the bullet text already on record, propose it here too. Frame
every row as a yes/no question, never an assertion.

Add the two Track 3 sections from the templates file.

## Step 4 — report

Tell the user:
- Path to the draft file
- Track 1: N ready-to-add synonyms across M bullets, K unmappable keywords
- Track 1: top 3 highest-frequency unmappable keywords (market wants this; profile can't claim it)
- Track 2: X memory-unlock prompts (Y flagged as "also in shortlist — high value if confirmable"),
  broken out by context
- Track 3: P ready-to-add skill aliases across Q skills, R memory-unlock prompts
  for skills_master.md
- Reminder: "Answer the yes/no questions in the Memory unlocks tables first — that's
  the fastest path to new claimable vocabulary and new skill entries. Then review
  the Ready-to-add synonyms/aliases. Only add what you can stand behind on a
  recruiter call. New skills_master.md entries are written by you, not this command."

## Summary session

When the user answers in the morning session: an unlock question, `uv run synonyms
answer <id> yes|no`; a declined keyword they agree with, `uv run synonyms approve
<keyword>`. A `yes` still means the user writes the bullet or skill block, or asks you
to draft it for them to paste.
