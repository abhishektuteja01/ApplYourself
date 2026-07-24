---
description: Incrementally score new jobs and regenerate today's shortlist. Spawns judge agents per vertical range.
model: sonnet
effort: medium
allowed-tools:
  - Bash
  - Agent
---

# /score

No arguments. (Range/vertical scoping is judge-mode only, invoked
internally; for a full vertical re-judge use `/rescore --vertical`.)

1. `uv run python -m src.score_cli prepare`
   - Exit 1 → report the printed error, stop.
   - `rows_to_score=0` → skip to step 4.
   - Each `range <vertical> <A>-<B>` line is one judge to spawn.

2. Spawn one Agent per `range` line, single message, parallel, model
   sonnet, prompt exactly:
   `/score-judge --range <A>-<B> --vertical <name>`
   No other content in the prompt.

3. `uv run python -m src.score_cli check-coverage`
   - Exit 0 → merge (3a).
   - Missing job_ids → find their line numbers without printing row
     content: `grep -n '<job_id>' jobs/scored.staging/unscored_<vertical>.jsonl | cut -d: -f1`.
     Re-spawn only the incomplete ranges (same prompt format), re-check
     once. Still incomplete → stop, report failed ranges. Never judge them
     yourself. Never merge partial coverage.
   - Unexpected/duplicate job_ids → delete those batch files, re-spawn
     those ranges, same one-retry rule.
   - Row-level problems (bad subscores/label) are not a re-spawn trigger —
     they surface at merge.

   3a. `uv run python -m src.score_cli merge`
   - `ValueError: Cannot merge N invalid score(s)` → fix only the named
     row(s) in the named batch file in place, re-run merge. Never
     re-spawn a judge for a row fix. Never suppress an invalid row.

4. `uv run python -m src.score_cli render`
   - Writes `shortlist/<date>.md`, prints the report line.
   - AssertionError here is a bug: fix root cause, re-run. Don't hand-edit
     the file.

5. Report the line step 4 printed, plus recovered/re-spawn/in-place-fix
   counts if any occurred.
