---
description: Run the nightly scrape and read the run report back — preflight, scrape, then a ≤10-line health digest of tonight's run against its own 7-day history. READ-ONLY on every report; writes nothing outside jobs/.
model: sonnet
effort: medium
allowed-tools:
  - Bash
  - Read
---

# /discover — scrape, then digest the run report

`uv run discover` writes `jobs/runs/<run_id>.md` and nobody reads it back.
This command runs the scrape and then renders what the report says.

Usage: `/discover [--resume ID] [--deadline-hours H] [--source NAME] [--max-terms N]`
Pass every flag the user gave straight through to Step 2. `--source` is
repeatable. With no flags, a full run.

`/discover digest` skips Steps 1-2 and renders the newest report already on disk.

## Step 1 — preflight

```bash
cd "$(git rev-parse --show-toplevel)" || { echo "ERROR: not inside the repo."; exit 1; }
uv run discovery-check
```

Nonzero exit means the config does not resolve. Print the error and stop —
do not start a scrape against a broken config.

## Step 2 — scrape

```bash
cd "$(git rev-parse --show-toplevel)" || exit 1
uv run discover <the user's flags, verbatim>
```

A full run takes hours. Tell the user the run id as soon as `discover` prints
it. If the command exits nonzero, still go to Step 3: the report and the
cleaning half are written from a `finally`, so a failed run still has one.

## Step 3 — digest

```bash
cd "$(git rev-parse --show-toplevel)" || exit 1
uv run discover-digest --json
```

`--run-id <id>` digests an older run instead of the newest; `--window N`
changes how many prior runs the medians cover (default 7).

Render at most 10 lines from that JSON. Every number in it is already
computed — `median_rows`, `ratio`, `error_delta`, `zero_streak`,
`below_median`, `silent_zero`. Print those values; do not recompute, re-median
or re-ratio anything, and do not open `jobs/runs/*.md` to check them.

Line budget, in priority order — drop from the bottom when you run out:

1. `run_id`, `wall_time_s`, `final_rows`.
2. One line per name in `crashed` — a lane that wrote no shard.
3. One line per name in `silent_zeros` — zero rows, no error, and this lane
   normally returns rows. Give `zero_streak` when it is above 1; that is the
   "three nights running" case.
4. One line per name in `below_median` — give `rows`, `median_rows`, `ratio`.
5. One line for any trend whose `error_delta` is at least 5.
6. One line naming `truncated` and `not_started` lanes, if any.
7. One line naming `skipped` lanes. A skip is a cadence decision, not a
   failure: never report it as one.
8. `parse_notes`, if any — the report itself was unreadable in part.

Say "clean run" in one line when 2-6 are all empty. No preamble, no table.

## Step 4 — read-only assertion

Before reporting done, confirm this command wrote nothing outside what
`uv run discover` writes itself (`jobs/raw/`, `jobs/clean.parquet`,
`jobs/clean.preview.jsonl`, `jobs/runs/<run_id>.md`, the ledgers under
`jobs/`). Step 3 is a reader. No `state.yaml` write (R10), no `profile/` write,
no edit to any run report.
