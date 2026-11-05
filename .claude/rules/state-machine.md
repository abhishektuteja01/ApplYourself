---
paths:
  - "src/state_io.py"
  - "src/track_cli.py"
  - "pipeline/**"
---

# The state machine (R10)

11 states: `saved, skip, tailored, applied, recruiter_contact, screen, interview,
offer, rejected, withdrawn, ghosted`. Terminal (`offer, rejected, withdrawn,
ghosted`) reject all out-transitions.

R10: `src/track_cli.py` is the only module that calls `state_io.transition` or
`ensure_state`; `/track`, `app_cli`, `apply_cli` and `apply/triage` call
`track_cli.main`, and `tests/test_state_io.py` fails on any other caller. `/tailor` (through
`uv run app build resume`) and `/outreach` only append to side lists
(`tailored_dirs[]`, `outreach[]`). Whether a role has a cover letter or company
answers is whatever files its `tailored_dirs[-1]` folder holds. `/standup` is
read-only and is the sole regenerator of `pipeline.md`.

`.claude/hooks/state_yaml_guard.sh` blocks the Edit/Write tools from targeting a
`state.yaml` at all, since every legitimate write goes through Bash.

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
