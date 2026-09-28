---
description: Score new jobs and write today's shortlist. --all re-judges every role not past saved/tailored; --vertical V limits to one lane.
model: sonnet
effort: low
argument-hint: "[--all] [--vertical <name>]"
allowed-tools:
  - Bash
  - Agent
---

# /score

1. `uv run score start $ARGUMENTS`. Exit 1: report the error, stop.
   A `done` line: report it, stop.
2. For each `judge ...` line, spawn one `score-judge` agent whose prompt is
   the rest of that line. All in one message, in parallel.
3. `uv run score finish`. Exit 0: report its first line, stop.
   Exit 1: spawn its `judge ...` lines once the same way, run `finish` again.
   Still exit 1: report its `bad` lines and stop.

Never read, edit or delete files in `jobs/scored.staging/`.
