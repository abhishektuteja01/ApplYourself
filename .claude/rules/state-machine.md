---
paths:
  - "src/state_io.py"
  - "src/track_cli.py"
  - "pipeline/**"
---

# The state machine (R10)

9 states: `saved, skip, tailored, applied, screen, interview, offer, rejected,
withdrawn`. `state_io.ALLOWED` lists the moves:

- `saved` -> `tailored, applied, skip, withdrawn`
- `tailored` -> `applied, saved, skip, withdrawn`
- `applied` -> `screen, interview, offer, rejected, withdrawn`
- `screen` -> `interview, offer, rejected, withdrawn`
- `interview` -> `offer, rejected, withdrawn`
- `skip` -> `saved`
- `offer, rejected, withdrawn`: terminal.

Any other move, same-state included, needs `--force --note "<why>"`; the
history entry carries the note and `forced: true`. Creating a role's file is
not a move.

`ghosted` is never stored. `state_io.shown_state` shows an `applied` role as
`ghosted` once its last `state_history` entry is older than
`ghosted_after_days` (`profile/automation.yaml`, default 30): in `track
states`, `/standup`, the status sheet and the night summary.

History is `pipeline/events.jsonl`, one append-only line per change (`v, ts,
job_id, kind, from, to, note, forced, data`; kinds `created`, `transition`,
`tailored_dir`, `outreach_drafted`, `outreach_sent`, `cleaned`, and
`imported` from the migration). Each `state.yaml` is a cache of its role's
fold (`state_io.apply_event`), rewritten under the role's lock right after
the event is appended with fsync; readers keep reading `state.yaml`. A write
to a role whose `state.yaml` is gone rebuilds it from the log first, and
`uv run track rebuild-states --dry-run|--apply` remakes any that drifted.
Until `uv run track migrate-events --apply` creates the log, writes update
`state.yaml` alone. The migration turns every `state_history` into events and
restores the submit ledger's sent roles that have no `state.yaml`; `--dry-run`
exits 1 if the fold differs from any file.

R10: `src/track_cli.py` is the only module that appends state events
(`created`, `transition`, through `state_io.transition` or `ensure_state`);
`/track`, `app_cli`, `apply_cli` and `apply/triage` call `track_cli.main`, and
`tests/test_state_io.py` fails on any other caller. `/tailor` (through
`uv run app build resume`) and `/outreach` only append side-list events
(`tailored_dirs[]`, `outreach[]`). Whether a role has a cover letter or company
answers is whatever files its `tailored_dirs[-1]` folder holds. `/standup` is
read-only and is the sole regenerator of `pipeline.md`.

`uv run track cleanup [--apply]` (`src/cleanup.py`, the night's cleanup stage)
deletes the folders of `skip` and never-applied `withdrawn` roles, and of
`rejected`, applied-then-`withdrawn` and `ghosted` roles after writing one
`applications/_archive/<lane>/<folder>.md`: the resume text, then a `## Job
description` section from `jd_snapshot.md` when the folder has one. It records `cleaned:
{date, archived}` through `state_io.mark_cleaned` and never changes `state:`. A
cleaned role's missing folder makes `app_cli.built_folder` None (so `app dir`
gives an empty `dir`) and the sheet shows it `Cleaned`, so neither night stage
rebuilds it; a fresh `/tailor` makes a new folder.

`.claude/hooks/state_yaml_guard.sh` blocks the Edit/Write tools from targeting a
`state.yaml` or `pipeline/events.jsonl` at all, since every legitimate write
goes through Bash.

## The three `saved -> tailored` firers

All route through `track_cli` and are guarded to fire only from `saved`:

- `/cover-letter` fires it after `uv run app build letter` succeeds, with
  `uv run track <id> tailored --if saved`.
- `/apply` fires it itself, on a `saved` role, once its plan shows no
  cover-letter park (the board has no letter upload, or the letter now exists).
- `apply triage --fill` fires it before filling a ready role.

A cover letter is a per-board prerequisite, decided by that plan-check.

## The two entry bars differ

- `apply prepare` — the per-role path `/apply` uses — accepts state `saved` or
  `tailored` with non-empty `tailored_dirs[]`. This is what lets the
  self-promotion fire.
- `apply run`'s `eligible_queue()` takes `tailored` only and leaves out every
  role whose latest submit-ledger line (`applications/apply_runs/submitted.jsonl`)
  is anything but `refused`;
  `--job-id` is checked against that same queue.

A board that DOES need a cover letter blocks inside the plan-check either way.
