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

This is `/score` with prior judgments discarded and its own dump (no
`prepare`). Read `.claude/commands/score.md` in full — steps 2-5 (judge
fan-out, completeness check, merge, render) apply unchanged.

All deterministic pre-screens (title-out-of-lane, plus every configured
vertical's title/JD disqualifier from `profile/verticals.yaml`) still
apply, against every row. `vertical` is read from `clean.parquet` as-is,
never reclassified.

## Step 1 — prerequisites, leftover batches, dump, split

```bash
test -f jobs/clean.parquet || { echo "ERROR: jobs/clean.parquet missing — run discovery first"; exit 1; }
test -f profile/sponsorship_rules.yaml || { echo "ERROR: profile/sponsorship_rules.yaml missing"; exit 1; }
uv run python -m src.verticals || { echo "ERROR: verticals config invalid or per-vertical rubric files missing"; exit 1; }
mkdir -p jobs/scored.staging shortlist
```

Leftover staged batches (`ls jobs/scored.staging/batch_*.json`): do NOT
merge them here.
- Continuing an interrupted `/rescore`? Run plain `/score` instead.
- Otherwise: proceed — the dump below clears them.

`--check` stops after the dump below with counts. `--vertical <name>`
scopes the re-judge to one vertical.

**Full rescore (no `--vertical`):**

```bash
rm -f jobs/scored.parquet
uv run python -m src.score_cli dump --force-all
uv run python -m src.score_cli split
```

**Vertical-scoped rescore (`--vertical <name>`):** do NOT delete
`scored.parquet` — `merge_scores` overwrites by `job_id` on collision,
leaves every other row untouched.

```bash
uv run python -m src.score_cli dump --force-all --vertical <name>
uv run python -m src.score_cli split
```

**If `--check`:** stop here, report the printed counts.

## Step 2 — fan out judges

```bash
uv run python -m src.score_cli ranges
```

Each `range <vertical> <A>-<B>` line is one judge to spawn. Spawn one
Agent per line, single message, parallel, model sonnet, prompt exactly
`/score-judge --range <A>-<B> --vertical <name>`. No other content in the
prompt. Never chunk the counts by hand — `ranges` is the same printer
`prepare` uses for `/score`.

## Steps 3-5 — identical to /score

Completeness check, merge, render — same as `/score` steps 3-4.

In the final report, note "full rescore" (or "<vertical> rescore — other
vertical's scores untouched").

---

## /rescore triggers (explicit-only, never auto-run)

- `profile/bullets.md`, `profile/verticals.yaml` (incl. `skill_weights`),
  `profile/verticals/*/rubric.md`, `profile/preferences.md`,
  `profile/sponsorship_rules.yaml`, or `profile/scoring_rubric.md` edited.
- Model upgrade, re-judging everything under the new model: spawn Step
  2's judges with the new model AND pass it to merge
  (`uv run python -m src.score_cli merge --model <model-id>`).
