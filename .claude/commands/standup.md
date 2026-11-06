---
description: Regenerate pipeline.md from pipeline/*/state.yaml — `uv run track standup` renders the Active/Closed tables, the session adds a short summary at the top. SOLE regenerator of pipeline.md. READ-ONLY on every state.yaml.
model: sonnet
effort: medium
allowed-tools:
  - Bash
  - Read
  - Write
---

# /standup — regenerate pipeline.md (read-only on state.yaml)

`uv run track standup` writes `pipeline.md` (title, Active and Closed tables)
from every `pipeline/<id>/state.yaml`, joined to `score_store.view()`. The
session writes only the short note at the top. Nothing here writes a
state.yaml.

## Step 1 — read the digest

```bash
# Every path below is repo-relative, and src/ resolves its own paths from
# the repo root — so anchor the shell there too.
cd "$(git rev-parse --show-toplevel)" || { echo "ERROR: not inside the repo."; exit 1; }
uv run track standup --digest
```

It writes nothing and prints the active / closed / ghosted counts,
`ghosted_after_days`, the roles touched since the last `pipeline.md`, and the
active roles untouched for `ghosted_after_days`+ days.

## Step 2 — write the ≤4-line note

From the digest only, write 4 lines or fewer covering:
- **what changed since last standup**: the touched roles
- **what's going stale**: the untouched active roles, and the ghosted count
- **suggested next actions**: 1-2 concrete prompts, e.g. "3 roles show
  `ghosted`: `/track <job_id> withdrawn` or `/outreach <job_id> referral
  --to '...'` on the strongest pending fit"

No bullet padding, no "Here's the summary:" preamble. With zero roles, skip
the note.

Run `mktemp`, Write the note to the path it prints, then:

```bash
uv run track standup --note-file <that path>
```

## Step 3 — report

Relay the `OK:` line ("pipeline.md regenerated; <N> active, <M> closed") and
name any roles flagged going stale so the user can act.
