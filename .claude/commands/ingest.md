---
description: One command from a job URL to finished application material — ingest, clean, score, register, tailor, cover letter. Applying stays manual.
model: sonnet
effort: medium
allowed-tools:
  - Bash
  - Read
  - WebFetch
  - Agent
argument-hint: <url> <vertical> [resume] [cover-letter]
---

# /ingest — one URL to application material

`$1` = posting URL. `$2` = configured vertical name. Parse `$ARGUMENTS` for the
artifact tokens `resume` and `cover-letter` — either, both, or neither, in any
order. Unknown token → stop. `cover-letter` implies `resume`: it reuses
`/tailor`'s `jd_snapshot.md` + `keywords_to_mirror.md`. Neither → stop after
step 3 and print the two commands.

Never judge a row yourself — step 3 spawns a judge. Never write a state
transition; `/track` is the sole writer (R10).

## Step 1 — pre-flight (one block, fail loud)

Everything that can fail late is checked here, before a fetch or a judge.

```bash
cd "$(git rev-parse --show-toplevel)" || { echo "ERROR: not inside the repo."; exit 1; }
test -n "$2" || { echo "ERROR: /ingest requires <url> <vertical>."; exit 1; }
uv run verticals-check || exit 1
test -f profile/scoring_rubric.md || { echo "ERROR: profile/scoring_rubric.md missing -- score-judge reads it."; exit 1; }
# score_cli dump clears staging unconditionally, so a concurrent /score would
# lose its judged batches.
test -z "$(ls -A jobs/scored.staging 2>/dev/null)" || { echo "ERROR: jobs/scored.staging is non-empty -- a /score is in flight. Re-run /ingest after it finishes."; exit 1; }
```

If `cover-letter` was requested, also:

```bash
test -f profile/cover_letter_template.docx || { echo "ERROR: profile/cover_letter_template.docx missing -- /cover-letter needs it. Add it or drop the cover-letter token."; exit 1; }
```

## Step 2 — ingest the URL

```bash
uv run ingest-url "$1" --vertical "$2"
```

Prints `job_id: <8-hex>`. On exit 1, match the message:

| Message | Action |
|---|---|
| `not a recognized ATS posting` | `uv run ingest-url "$1" --dry-run` (writes nothing), read company and title off the printed text, re-run with `--company "..." --title "..."`. **One attempt.** They define the `job_id` hash; a wrong pair mints a second id for the same role and cannot be deduped against the board's spelling. |
| `below the ... cleaning floor` | `WebFetch` the URL; if the text is still short, stop — say to paste the JD into `inbox/` and run `/score`. |
| `did not survive cleaning` + a listed `job_id` | A near-duplicate won dedupe. Continue with that `job_id`; skip to step 4. |
| `location_allowlist` | Stop. Report the location and that `profile/discovery.yaml` must allow it. |
| `not configured` / `did not classify` | Stop. Report the configured vertical list. |

A failed ingest leaves its raw row in `jobs/raw/<run_id>.parquet`; every later
cleaning run re-drops it. Name that file when you stop, and do not retry with
guessed variations — each attempt adds another orphan row.

## Step 3 — score that one row

```bash
uv run python -m src.score_cli dump --job-id <job_id> --no-prescreen
```

`--no-prescreen` sends a deliberately chosen role to a judge instead of
auto-skipping it, so it gets real subscores and `keywords_to_mirror`. Read
`rows_to_score=` from the output:

- `0` → already in `scored.parquet`. Skip to step 4 (a re-tailor becomes `_v2`).
  Do **not** spawn a judge; the range would not exist.
- `1` → continue:

```bash
uv run python -m src.score_cli split
```

Spawn one Agent, model sonnet, prompt exactly — no other content:
`/score-judge --range 1-1 --vertical $2`

```bash
uv run python -m src.score_cli check-coverage
uv run python -m src.score_cli merge --no-prune
```

- `check-coverage` exit 1 → delete the batch, re-spawn once, re-check. Still
  failing → stop before tailoring.
- `merge` `ValueError: Cannot merge` → fix the named row in the named batch
  file in place, re-run merge. Never re-spawn for a row fix.
- `--no-prune` is required: step 2 rebuilt `clean.parquet` over the 14-day raw
  window, so pruning here would delete every aged-out row from
  `scored.parquet`.

## Step 4 — resume

Requested (or implied)? Spawn one Agent, prompt exactly: `/tailor <job_id>`

`/tailor` registers `pipeline/<job_id>/state.yaml` at `saved` itself via
`track_cli ensure`. If it reports a hard-refuse, stop — do not run step 5.

## Step 5 — cover letter

Only after step 4 reports success. Spawn one Agent, prompt exactly:
`/cover-letter <job_id>`

Strictly sequential: its prereqs are written by `/tailor` steps 7 and 8.

## Step 6 — report

```bash
uv run python -c "
import pandas as pd, sys
r = pd.read_parquet('jobs/scored.parquet').set_index('job_id').loc[sys.argv[1]]
print(f'fit_score={r.fit_score:.0f} action={r.suggested_action} sponsorship={r.sponsorship_label}')
print(f'reasoning: {r.reasoning}')
" <job_id>
```

Report, concisely: company and title (flag them as derived if step 2 needed
`--dry-run`), vertical, the line above, and each artifact path. State is
`saved`.

Close by asking the user to say the word once they've applied, then run
`uv run track <job_id> applied`. Do not transition before they say so.
