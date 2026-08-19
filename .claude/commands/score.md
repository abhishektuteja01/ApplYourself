---
description: Incrementally score new jobs and regenerate today's shortlist. Spawns judge agents per vertical range.
model: sonnet
effort: medium
allowed-tools:
  - Bash
  - Read
  - Edit
  - Agent
---

# /score

No arguments. (Range/vertical scoping is judge-mode only, invoked
internally; for a full vertical re-judge use `/rescore --vertical`.)

1. `uv run python -m src.score_cli prepare`
   - Exit 1 → report the printed error, stop.
   - `rows_to_score=0` → skip to step 4.
   - Each `range <vertical> <A>-<B>` line is one judge to spawn.

2. Spawn one Agent per `range` line, single message, parallel, with
   `subagent_type: score-judge` and prompt exactly:
   `--range <A>-<B> --vertical <name>`
   No other content in the prompt. The agent definition
   (`.claude/agents/score-judge.md`) fixes its model and tools.

3. `uv run python -m src.score_cli check-coverage`
   - Exit 0 → merge (3a).
   - Missing job_ids → find their line numbers without printing row
     content: `grep -n '<job_id>' "$(git rev-parse --show-toplevel)"/jobs/scored.staging/unscored_<vertical>.jsonl | cut -d: -f1`
     (repo-anchored: `jobs/` is resolved from the repo root, not the CWD).
     Re-spawn only the incomplete ranges (same prompt format), re-check
     once. Still incomplete → stop, report failed ranges. Never judge them
     yourself. Never merge partial coverage.
   - Unexpected/duplicate job_ids → delete those batch files, re-spawn
     those ranges, same one-retry rule.
   - `unreadable: [...]` → a judge wrote a corrupt batch. Repair the JSON
     in place if the rows are recoverable; only if they are not, delete the
     file and re-spawn that range. Never merge with a batch unreadable.

   **Deleting batches:** allowed before merge (here), because the range can
   just be re-judged. Never once merge has started (3a) — it banks rows as it
   goes, so a delete discards judged work with no trace. If unsure, do not.
   - Row-level problems (out-of-range axis, bad label) are not a re-spawn
     trigger — they surface at merge.

   3a. `uv run python -m src.score_cli merge`
   - `ValueError: Cannot merge N invalid score(s)` → fix only the named
     row(s) in the named batch file in place, re-run merge. Never
     re-spawn a judge for a row fix. Never suppress an invalid row.
     Each error is prefixed `batch_<name>.json[<i>]` — the file and the
     0-indexed position within its array.
   - Exit 1 with `unreadable batch file(s), staging kept` → rows already
     merged are banked and staging is intact. Repair the named file, re-run
     merge (safe: rows overwrite by job_id). Never delete a batch to make
     merge pass — that discards ~100 judged rows.

4. `uv run python -m src.score_cli render`
   - Writes `shortlist/<date>.md`, prints the report line.
   - AssertionError here is a bug: fix root cause, re-run. Don't hand-edit
     the file.

5. Report the line step 4 printed, plus recovered/re-spawn/in-place-fix
   counts if any occurred.
