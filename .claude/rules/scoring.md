---
paths:
  - "src/score_cli.py"
  - "src/score_filter.py"
  - "src/score_store.py"
  - "src/shortlist.py"
---

# Scoring architecture (`/score`)

Three steps, one owner per rule:

1. `score start` (Python): `score_filter.drop_reason` drops rows by the hard
   rules (no lane, `profile/scoring.yaml` `ineligible`, the lane's
   `drop_phrases`, the lane's `years` table) straight into `scored.parquet`
   as `scored_by_model="filter:<code>"`. The rest is staged in
   `jobs/scored.staging/` as one packet per lane and slices of at most 10 roles.
2. `score-judge` agents (the only model step): four subscores, the
   sponsorship label with a literal JD quote, keywords, a short reason, and an
   optional action that may only lower it.
3. `score finish` (Python): checks each answer against its slice, derives the
   caps, tags, total and action, merges, prunes, and writes the shortlist.

Do not move a mechanical rule back into a rubric, or judgment into Python. A
new hard rule goes in `score_filter.py` plus a `profile/` key; a new derived
field goes in `score_store.derive`.

`/score --all [--vertical V]` re-judges instead of scoring only new rows; there
is no separate rescore command. `--job-id` (for `/ingest`) skips the drops.

Prune keeps every row still in `clean.parquet` or in an active
`state.yaml`, and discovery's view always includes active roles, so a
tracked role never loses its score.

Note: `shortlist.py` imports `apply/detect.py`, which pulls the ATS parsers
into the scoring path at import time.
