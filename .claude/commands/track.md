---
description: Sole writer of state transitions: move a role along the allowed moves, or flip a sent outreach. Other moves need --force --note.
model: sonnet
effort: low
disable-model-invocation: true
allowed-tools:
  - Bash(uv run track *)
argument-hint: <job_id> <state> [--note "..."] [--force]  |  outreach-sent <job_id> --channel <c> --to "<n>"
---

Run and print the output verbatim, nothing added:

```bash
uv run track $ARGUMENTS
```

`outreach-draft` belongs to `uv run app build outreach`; never run it by hand.

If it exits nonzero, the stderr line already says what's wrong -- stop, don't retry or work around it.
