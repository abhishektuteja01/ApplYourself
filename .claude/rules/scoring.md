---
paths:
  - "src/score_cli.py"
  - "src/scoring_io.py"
  - "src/shortlist.py"
  - "src/prescreen.py"
---

# Scoring architecture (`/score`)

`/score` takes no arguments. It runs the deterministic `src.score_cli`
subcommands in order — `prepare` → judges → `check-coverage` → `merge` →
`render` — and fans out parallel Sonnet judge agents over per-vertical row
ranges. It never judges a row itself, and its context stays counts-only — the one
exception is the recovery path, where it reads and repairs the specific batch rows
a corrupt-JSON or merge-validation error names.

Judging is a **separate command file**, `score-judge.md`, spawned per range
(`--range A-B --vertical V`). A judge reads lines A–B of
`jobs/scored.staging/unscored_<vertical>.jsonl`, writes batch files, and never
merges.

A judge only picks rows from its assigned range — gaps/collisions are impossible by
construction. Deterministic pre-screens (out-of-lane titles, per-vertical
disqualifiers, `hard_ineligible` sponsorship phrases) auto-skip rows *before* any
judge sees them. `/rescore` discards `scored.parquet` and re-judges the whole
14-day window from scratch.

`DEFAULT_MODEL` in `score_cli.py` is a bare string printed for the judge subagent
to use. No client is constructed — see R7.

Note: `shortlist.py` imports `apply/detect.py`, which transitively pulls the
playwright bootstrap and the ATS HTTP client into the scoring path at import time.
`browser.py` defers the real playwright import into a function, so a clone without
`--group apply` still works, but the import edge is there.
