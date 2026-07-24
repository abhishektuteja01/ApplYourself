---
description: Sole writer of state transitions. Transitions a role through the 11-state machine, OR flips a sent outreach. Creates pipeline/<job_id>/state.yaml if missing; rejects illegal transitions out of terminal states.
model: sonnet
effort: low
allowed-tools:
  - Bash
argument-hint: <job_id> <state> [--note "..."]  |  outreach-sent <job_id> --channel <c> --to "<n>"
---

Run and print the output verbatim, nothing added:

```bash
uv run python -m src.track_cli $ARGUMENTS
```

If it exits nonzero, the stderr line already says what's wrong -- stop, don't retry or work around it.
