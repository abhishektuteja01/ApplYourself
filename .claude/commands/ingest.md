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
test ! -f jobs/scored.staging/run.json || { echo "ERROR: a /score run is open. Finish it with /score, then re-run /ingest."; exit 1; }
# Step 2 writes the jobs store and rebuilds clean.parquet. It waits on the
# store's lock, but a nightly run would rebuild clean.parquet again at its end,
# so wait for it. The bracketed letters keep the pattern from matching itself.
pgrep -f 'discovery[.]ru[n]|/discove[r]$' >/dev/null && { echo "ERROR: a discovery run is in flight. Re-run /ingest after it finishes."; exit 1; }
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
later step**: when the posting joins a job the store already had, it prints
`note: this posting joined a job ...`, and that job keeps its own id, vertical,
url and source. If the vertical differs from `$2`, use the printed one and say
so in step 6.

On exit 1, match the message:

| Message | Action |
|---|---|
| `not a recognized ATS posting` | `uv run ingest-url "$1" --dry-run` (writes nothing), read company and title off the printed text, re-run the step 2 command with `--company "..." --title "..."` added (keep `--vertical "$2"`). **One attempt.** They define the `job_id` hash. A wrong company spelling still merges against the board's own spelling if the JD matches, and the merge keeps the tracked id — but a wrong *title* does not, and mints a second id for the same role. |
| `below the ... floor` | `WebFetch` the URL; if the text is still short, stop — say to paste the JD into `inbox/` and run `/score`. |
| `does not exist yet` | Stop. The jobs store has not been built; report the printed command. |
| `did not survive cleaning` | Stop. The job was saved but the view dropped it (its retention window has passed); report the `job_id`. |
| `location_allowlist` | Stop. Report the location and that `profile/discovery.yaml` must allow it. |
| `not configured` / `did not classify` | Stop. Report the configured vertical list. |

Every row but `did not survive cleaning` fails before any write, so retrying
costs nothing. Retry only as the table prescribes; a guessed title mints a
distinct `job_id`.

## Step 3 — score that one row

Already scored? Then skip to step 4 (a re-tailor becomes `_v2`):

```bash
uv run python -c "
import pandas as pd, sys
from pathlib import Path
p = Path('jobs/scored.parquet')
print(p.exists() and sys.argv[1] in set(pd.read_parquet(p)['job_id']))
" <job_id>
```

`False` → `uv run score start --job-id <job_id>`. `--job-id` skips the drops,
so a deliberately chosen role gets real subscores and `keywords_to_mirror`.
- Exit 1 → report the error, stop.
- `judge=0` → the `job_id` is not in `clean.parquet`; stop.
- One `judge ...` line → spawn one `score-judge` agent whose prompt is the
  rest of that line, then `uv run score finish`. Exit 1 → spawn its `judge`
  line once, `finish` again; still exit 1 → stop before tailoring.

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
