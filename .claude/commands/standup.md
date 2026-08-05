---
description: Regenerate pipeline.md from pipeline/*/state.yaml — Claude-rendered rollup with a short summary at the top, Active/Closed tables below. SOLE regenerator of pipeline.md. READ-ONLY on every state.yaml.
model: sonnet
effort: medium
allowed-tools:
  - Bash
  - Read
  - Write
---

# /standup — regenerate pipeline.md (read-only on state.yaml)

`/standup` reads every `pipeline/<id>/state.yaml`,
writes a single `pipeline.md` rollup, and MUST NOT mutate any state.yaml.

## Step 1 — load all states

```bash
uv run python <<'PYEOF'
import json
from pathlib import Path
from src.state_io import load_all_states
from src.state_io import ACTIVE_STATES, CLOSED_STATES
states = load_all_states(Path("pipeline"))
def jsonable(d):
    return {k: (str(v) if not isinstance(v, (str, int, float, bool, list, dict, type(None))) else v)
            for k, v in d.items()}
out = {
    "states":         [jsonable(s) for s in states],
    "active_states":  sorted(ACTIVE_STATES),
    "closed_states":  sorted(CLOSED_STATES),
}
Path("/tmp/standup_states.json").write_text(json.dumps(out, indent=2, default=str))
print(f"loaded {len(states)} state.yaml file(s)")
PYEOF
```

Read `/tmp/standup_states.json`. If there are zero states, write a
`pipeline.md` that simply says "No roles tracked yet. Run `/track
<job_id> saved` after reviewing today's shortlist." and stop.

## Step 2 — compute the ≤4-line summary

Based on the loaded states, write a short prose summary (4 lines or
fewer) covering:
- **what changed since last standup**: roles whose `last_touch` is within
  the last ~3 days
- **what's going stale**: roles in a non-terminal state with
  `last_touch` more than 14 days ago
- **suggested next actions**: 1-2 concrete prompts, e.g. "`/track
  ${jid} ghosted` on 3 roles `applied` >14 days ago with no response;
  `/outreach ${jid} referral --to '...'` on the strongest pending fit"

Keep it tight. No bullet padding, no "Here's the summary:" preamble --
the reader knows what they're reading.

## Step 3 — split into Active and Closed tables

- **Active**: state in {`saved`, `tailored`, `applied`,
  `recruiter_contact`, `screen`, `interview`}
- **Closed**: state in {`offer`, `rejected`, `withdrawn`, `ghosted`,
  `skip`} (skip is closed-for-view but NOT terminal)

For each row, compute the columns:
- `state`
- `company`
- `title`           (truncate to ~60 chars if longer)
- `vertical`        (a vertical name configured in `profile/verticals.yaml`,
                     or `?` if empty/missing -- legacy rows predating the
                     vertical column)
- `fit`             (`fit_score` int, or `?` if null)
- `spons`           (sponsorship_label)
- `last_touch`      (date only -- `YYYY-MM-DD` slice of the ISO timestamp)
- `dir_link`        (**last** entry of `tailored_dirs[]`, or empty string --
                     the newest version, matching /cover-letter and /outreach;
                     a `_v2` re-tailor supersedes the earlier dir)
- `note`            (the latest `state_history[].note`, or empty)

Sort Active by `last_touch DESC` (most recent activity first). Sort
Closed the same way.

## Step 4 — render pipeline.md

Write to `pipeline.md` at repo root:

```markdown
# Pipeline — <today YYYY-MM-DD>

<the ≤4-line summary>

## Active (N)

| state | company | title | vertical | fit | spons | last_touch | dir | note |
|---|---|---|---|---|---|---|---|---|
| <state> | <company> | <title> | <vertical> | <fit> | <spons> | <YYYY-MM-DD> | `<dir>` | <note> |
...

## Closed (M)

| state | company | title | vertical | fit | spons | last_touch | dir | note |
|---|---|---|---|---|---|---|---|---|
...
```

Omit a table if its section is empty (e.g. on day one when only `saved`
rows exist, the Closed table doesn't render).

Pipe characters inside any cell value must be escaped as `\|` so the
table doesn't break.

## Step 5 — read-only assertion

Before reporting done, confirm you did NOT call any Bash command that
writes to a `pipeline/<id>/state.yaml`. R10 forbids state mutation here.
The only writes from this command are to `pipeline.md` and the
`/tmp/standup_states.json` scratch file.

## Step 6 — report

Tell the user: "pipeline.md regenerated; <N> active, <M> closed". If any
roles are flagged "going stale" in the summary, name them so the user
can act.
