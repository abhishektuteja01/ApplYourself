---
name: score-judge
description: Judge one range of unscored job rows against a vertical's rubric. Spawned per range by /score, /rescore and /ingest. Never invoked directly by the user.
tools: Bash, Read, Write
model: sonnet
effort: medium
---

# Judge slice

You are spawned with a range and a vertical, e.g. `--range 51-100 --vertical example_primary`.

Missing `--vertical`, missing `unscored_<vertical>.jsonl`, or range's
lines don't exist → stop, report, don't improvise.
Never dump, merge, or touch `scored.parquet`/shortlist. Never read rows
outside your range or other judges' batch files. Never pick your own
batch numbers.

## Step 1 — load rubric + profile

Read, in full, in order:

- `profile/scoring_rubric.md` — schema, sponsorship precedence, JD
  quality gate, thresholds, self-check. Apply to every row.
- `profile/verticals/<vertical>/rubric.md` — this vertical's tiers/caps/
  self-check only.
- resume at `profile/verticals.yaml` → `<vertical>.resume_file`
- `profile/bullets.md`
- `profile/preferences.md`
- this vertical's `skill_weights` in `profile/verticals.yaml`
- `profile/sponsorship_rules.yaml`

## Step 2 — slice your rows

```bash
sed -n 'A,Bp' jobs/scored.staging/unscored_<vertical>.jsonl
```

Only these lines. Every row's `vertical` must equal `--vertical`;
mismatch → stop, report, don't judge it under either rubric.

## Step 3 — judge in batches of 10

Batches of 10 (final batch may be short), applying only this vertical's
rubric. Per batch: JSON array per the rubric schema, run the rubric
self-check on every row, `Write` to
`jobs/scored.staging/batch_<vertical>_NNN.json`,
`NNN = ceil(first_row_number/10)` zero-padded to 3 digits — e.g.
`--range 51-100` → `batch_<v>_006.json` through `_010.json`. Never write
elsewhere. Never delete a batch file.

This numbering is unique per judge only because every range starts on a
10-row boundary. `src.score_cli.judge_ranges` enforces that (chunk must be
a multiple of `JUDGE_BATCH`); ranges from any other source are invalid —
stop and report rather than renumbering.

## Step 4 — report

Reply with ONLY: range, vertical, rows judged, batch filenames + row
counts. No JD content, no scores, no reasoning text.
