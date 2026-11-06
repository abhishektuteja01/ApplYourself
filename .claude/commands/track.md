---
description: Sole writer of state transitions. Transitions a role along the allowed moves (state_io.ALLOWED), OR flips a sent outreach. Creates pipeline/<job_id>/state.yaml if missing; any other move needs --force --note.
model: sonnet
effort: low
allowed-tools:
  - Bash
argument-hint: <job_id> <state> [--note "..."] [--force]  |  outreach-sent <job_id> --channel <c> --to "<n>"
---

Run and print the output verbatim, nothing added:

```bash
uv run track $ARGUMENTS
```

If it exits nonzero, the stderr line already says what's wrong -- stop, don't retry or work around it.
