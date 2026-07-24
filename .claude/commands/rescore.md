---
description: Full re-judge from scratch — discards prior scored.parquet and judges every row in the current 14-day window. Use after editing profile/* or to refresh stale judgments.
model: sonnet
effort: medium
argument-hint: "[--vertical <name>] [--check]"
allowed-tools:
  - Bash
  - Read
  - Write
  - Agent
---

# /rescore — full re-judge

This is `/score` with prior judgments discarded. **Read
`.claude/commands/score.md` in full** — orchestrator flow, judge fan-out,
completeness check, merge, and shortlist render all apply unchanged (judges
read the rubric from `profile/scoring_rubric.md`). This file only documents
what differs: the dump.

All deterministic pre-screens (title-out-of-lane, plus every configured
vertical's title/JD disqualifier from `profile/verticals.yaml`) still apply — they
just run against every row since prior scores are discarded. `vertical` is
read from `clean.parquet` as-is, never reclassified.

> Safe even when `pipeline/<job_id>/state.yaml` exists — canonical state
> lives there, not in `scored.parquet`. Throwing `scored.parquet` away never
> loses workflow state.

## Step 1 — prerequisites + leftover batches

Run /score's Step O1 prereq block. Then check for leftover staged batches
(`ls jobs/scored.staging/batch_*.json`). If any exist, **do NOT run /score's
recovery merge here — it's wasted work**: this command's wipe + `--force-all`
dump re-judges every row regardless, so merged leftovers would be discarded
immediately. Instead:
- **Continuing an interrupted /rescore?** Don't re-run `/rescore` — run plain
  `/score`: its Step O2 merges the staged batches and its incremental dump
  judges only what's still unscored, completing the rescore without
  re-judging finished rows.
- **Intentionally starting over** (or the leftovers are from an interrupted
  `/score` whose rows you're about to re-judge anyway)? Proceed — Step 2's
  dump clears them; nothing is permanently lost since unmerged rows stay
  unscored and re-dump next run.

`--check` and `--vertical` are /rescore-only flags (/score takes none):
`--check` stops after the Step 2 dump with counts — a preview before
committing to a full re-judge; `--vertical` scopes the re-judge to one
vertical, per Step 2 below.

## Step 2 — DISCARD prior scores, then dump ALL clean rows

**Full rescore (no `--vertical`):**

```bash
rm -f jobs/scored.parquet
uv run python -m src.score_cli dump --force-all
```

**Vertical-scoped rescore (`--vertical <name>`, any vertical configured in
`profile/verticals.yaml`):** do NOT delete `scored.parquet` — that would
discard the other verticals' scores. A `--force-all` vertical-scoped dump +
merge is sufficient: `merge_scores` overwrites by `job_id` on collision and
leaves every other row untouched.

```bash
uv run python -m src.score_cli dump --force-all --vertical <name>
```

**If `--check`:** stop here, report the printed counts.

## Steps 3+ — identical to /score Steps O3 (split) through O10

Split, fan out judges (`/score --range <A>-<B> --vertical <v>` spawn
prompts, `model: sonnet`), completeness check, merge, shortlist, assertions.
The only difference is that every dumped row is fresh (no carry-forward);
`prune_scored` is a no-op since every scored `job_id` is by construction in
`clean.parquet`.

In the Step O10 report, note "full rescore" (or "<vertical> rescore — other
vertical's scores untouched") so the user knows what was discarded.

---

## When to use /rescore vs /score

- **/score** (incremental, default): daily run; only new postings are judged.
- **/rescore** (full re-judge): after editing `profile/bullets.md`,
  `profile/verticals.yaml` (incl. `skill_weights`),
  `profile/verticals/*/rubric.md`, `profile/preferences.md`,
  `profile/sponsorship_rules.yaml`, or `profile/scoring_rubric.md` —
  anything the judging is calibrated against. Also after a model upgrade if
  you want every row re-judged under the newer model — in that case spawn
  the judges with the new model in the Step O4 fan-out AND pass it to the
  merge (`uv run python -m src.score_cli merge --model <model-id>`) so the
  `scored_by_model` stamp stays honest. This is
  explicit-only — no auto-trigger from profile file changes.
