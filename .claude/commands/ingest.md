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

`$1` = posting URL. `$2` = configured vertical name. Every whitespace-separated
token *after* `$2` must be exactly `resume` or `cover-letter` — either, both, or
neither, in any order; anything else → stop. Match whole tokens only, never a
substring of the URL. `cover-letter` implies `resume`: it reuses `/tailor`'s
`jd_snapshot.md` + `keywords_to_mirror.md`. Neither → stop after step 3 and
print the two commands.

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
# Step 2 writes into jobs/raw/ and rebuilds clean.parquet + the seen ledger, so
# a discovery run must not be mid-flight. The bracketed letters keep the pattern
# from matching this command line itself.
pgrep -f 'discovery[.]orchestrato[r]|/discove[r]$' >/dev/null && { echo "ERROR: a discovery run is in flight. Re-run /ingest after it finishes."; exit 1; }
```

If `resume` was requested (or implied), also — `tailor-prep`'s prereqs, which
would otherwise fail only after a fetch, a judge and a `scored.parquet` write:

```bash
cd "$(git rev-parse --show-toplevel)" || exit 1
for f in profile/bullets.md profile/de_ai_rules.yaml profile/skills_master.md profile/resume_template.docx; do
  test -f "$f" || { echo "ERROR: $f missing -- /tailor needs it."; exit 1; }
done
```

If `cover-letter` was requested, also:

```bash
cd "$(git rev-parse --show-toplevel)" || exit 1
test -f profile/cover_letter_template.docx || { echo "ERROR: profile/cover_letter_template.docx missing -- /cover-letter needs it. Add it or drop the cover-letter token."; exit 1; }
```

## Step 2 — ingest the URL

```bash
uv run ingest-url "$1" --vertical "$2"
```

On success it prints `job_id: <8-hex>` and `Ingested: <company> — <title>
[<vertical>]`. **That printed vertical, not `$2`, is the row's lane for every
later step**: a duplicate already in `clean.parquet` can win dedupe on a longer
`jd_text` and keep its own vertical, url and source. If it differs from `$2`,
use the printed one and say so in step 6.

On exit 1, match the message:

| Message | Action |
|---|---|
| `not a recognized ATS posting` | `uv run ingest-url "$1" --dry-run` (writes nothing), read company and title off the printed text, re-run the step 2 command with `--company "..." --title "..."` added (keep `--vertical "$2"`). **One attempt.** They define the `job_id` hash; a wrong pair mints a second id for the same role and cannot be deduped against the board's spelling. |
| `below the ... cleaning floor` | `WebFetch` the URL; if the text is still short, stop — say to paste the JD into `inbox/` and run `/score`. |
| `did not survive cleaning` + a listed `job_id` | A near-duplicate won dedupe. Continue with that `job_id` — through step 3, not past it; it is in `clean.parquet` but not necessarily scored. Its vertical is whatever that row already carries, which may not be `$2`; read it with `uv run python -c "import pandas as pd; print(pd.read_parquet('jobs/clean.parquet').set_index('job_id').loc['<job_id>','vertical'])"`. |
| `did not survive cleaning`, no hint | Stop. Report the raw file (below) and that the row was dropped by a cleaning filter no hint covers; the run report `jobs/runs/<run_id>.md` names the stage. |
| `location_allowlist` | Stop. Report the location and that `profile/discovery.yaml` must allow it. |
| `not configured` / `did not classify` | Stop. Report the configured vertical list. |

The three `did not survive cleaning` / `location_allowlist` rows fail *after*
the raw row is archived, so they leave it in `jobs/raw/<run_id>.parquet` and
every later cleaning run re-drops it — name that file when you stop. The other
three fail before any write, so retrying them costs nothing. Either way, retry
only as the table prescribes; guessed company/title variations each mint a
distinct `job_id`.

## Step 3 — score that one row

```bash
uv run python -m src.score_cli dump --job-id <job_id> --no-prescreen
```

`--no-prescreen` sends a deliberately chosen role to a judge instead of
auto-skipping it, so it gets real subscores and `keywords_to_mirror`. Read
`rows_to_score=` from the output:

- `0` → the row is either already scored or absent from `clean.parquet`; the
  dump cannot tell you which. Confirm before going on:

  ```bash
  uv run python -c "
  import pandas as pd, sys
  from pathlib import Path
  p = Path('jobs/scored.parquet')
  print(p.exists() and sys.argv[1] in set(pd.read_parquet(p)['job_id']))
  " <job_id>
  ```

  `True` → skip to step 4 (a re-tailor becomes `_v2`). Do **not** spawn a judge;
  the range would not exist. `False` → stop; the `job_id` is in neither file.
- `1` → continue:

```bash
uv run python -m src.score_cli split
```

`split` prints one `<vertical>=<n>` pair per lane. Exactly one must be `1`, and
it must be step 2's vertical — if some other lane holds the row, that lane is
the truth; judge it there. Spawn one Agent, model sonnet, prompt exactly — no
other content:
`/score-judge --range 1-1 --vertical <that vertical>`

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
`--dry-run`), vertical (flag it if it is not `$2`), the line above, and each
artifact path. State is `saved`.

Close by asking the user to say the word once they've applied, then run
`uv run track <job_id> applied`. Do not transition before they say so.
