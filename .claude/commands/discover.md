---
description: Run the scrape, then a 10-line health digest of the run against its 7-day history. Writes nothing outside jobs/.
model: sonnet
effort: medium
disable-model-invocation: true
allowed-tools:
  - Bash(cd *)
  - Bash(git rev-parse *)
  - Bash(uv run discovery-check*)
  - Bash(uv run discover)
  - Bash(uv run discover --*)
  - Bash(uv run discover-digest*)
  - Read
---

# /discover — scrape, then digest the run report

Usage: `/discover [--deadline-hours H] [--source NAME] [--max-terms N]`
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
it. A killed run loses at most a few minutes: the next run finds the unchecked
boards still due.

## Step 3 — digest

```bash
cd "$(git rev-parse --show-toplevel)" || exit 1
uv run discover-digest --json
```

`--run-id <id>` digests an older run instead of the newest; `--window N`
changes how many prior runs the medians cover (default 7).

Render at most 10 lines from that JSON. Every number in it is already
computed — `median_rows`, `ratio`, `error_delta`, `zero_streak`,
`below_median`, `silent_zero`, `http_429`. Print those values; do not
recompute anything, and do not open `jobs/runs/*` to check them.

Line budget, in priority order — drop from the bottom when you run out:

1. `run_id`, `wall_time_s`, `final_rows`, `new_job_ids`.
2. One line per name in `crashed`.
3. One line per name in `silent_zeros` — zero rows, no error, and this lane
   normally finds rows. Give `zero_streak` when it is above 1.
4. One line per name in `below_median` — give `rows`, `median_rows`, `ratio`.
5. One line for any trend whose `error_delta` is at least 5 or whose
   `http_429` / `http_403` is above zero.
6. One line naming `truncated` lanes and `not_started` (boards left due).
7. One line from `boards`: per ATS, `dead` and `never_found` counts.

Say "clean run" in one line when 2-6 are all empty. No preamble, no table.
