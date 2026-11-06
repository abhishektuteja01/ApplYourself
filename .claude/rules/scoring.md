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
   rules (no lane, `profile/scoring.yaml` `ineligible` plus its
   `no_sponsorship` phrases when the home need is `now`, the lane's
   `drop_phrases`, the lane's `years` table). A drop is never stored: it is
   checked again every run, and a stored score of a row it now drops is
   deleted. The rest is staged in `jobs/scored.staging/` as one packet per
   lane and slices of at most 10 roles, each row with its `judge_input_hash`
   (`input_hash` of the lane packet and the trimmed JD).
2. `score-judge` agents (the only model step): four subscores, the facts
   (years, country, the JD's sponsorship statements with literal JD quotes),
   keywords, a short reason, and an optional action that may only lower it.
3. `score finish` (Python): checks each answer against its slice, derives the
   sponsorship label, caps, tags, total and action, merges (with the raw
   answer in `judge_answer`), prunes, and writes the shortlist.

Every reader of scored.parquet goes through `score_store.view()`: it derives
each judged row again from `judge_answer` under today's rules, keeps an older
row with no raw answer as `basis: legacy` at its stored score, and leaves out
`filter:*` rows. Never `pd.read_parquet` it for a score.

Do not move a mechanical rule back into a rubric, or judgment into Python. A
new hard rule goes in `score_filter.py` plus a `profile/` key; a new derived
field goes in `score_store.derive`.

`/score --all [--vertical V]` re-judges instead of scoring only new rows,
except roles whose state is past `shortlist.pipeline_states`; there is no
separate rescore command. `--stale-only` takes only judged rows whose hash
changed and prints the count without staging; `--yes` stages them. A row with
no hash (legacy) is never stale, and a plain `score start` re-judges nothing.
`--job-id` (for `/ingest`) skips every drop except no-lane.
`scripts/drop_filter_rows.py --dry-run|--apply` removes the old `filter:*`
rows once; the next `/score` then re-checks those roles.

Prune keeps every row still in `clean.parquet` or in an active
`state.yaml`, and discovery's view always includes active roles, so a
tracked role never loses its score.

Note: `shortlist.py` imports `apply/detect.py`, which pulls the ATS parsers
into the scoring path at import time.
