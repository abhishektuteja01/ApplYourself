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

`/track` is the **sole writer of state transitions** (R10): every transition goes
through it, and no other command writes `state:` itself. `/tailor` (through
`uv run app build resume`) and `/outreach` only append to side lists
(`tailored_dirs[]`, `outreach[]`). Whether a role has a cover letter or company
answers is whatever files its `tailored_dirs[-1]` folder holds. `/standup` is
read-only and is the sole regenerator of `pipeline.md`.

`.claude/hooks/state_yaml_guard.sh` blocks the Edit/Write tools from targeting a
`state.yaml` at all, since every legitimate write goes through Bash.

## The two `saved -> tailored` firers

Both route through `/track` and both are guarded to fire only from `saved`:

- `/cover-letter` fires it after `uv run app build letter` succeeds, with
  `uv run track <id> tailored --if saved`.
- `/apply` fires it itself, on a `saved` role, the moment its own plan-check
  confirms the board genuinely needs no cover letter (no required cover-letter
  upload, no unresolved company-specific question).

A cover letter is a per-board prerequisite, decided by that plan-check.

## The two entry bars differ

- `apply prepare` — the per-role path `/apply` uses — accepts state `saved` or
  `tailored` with non-empty `tailored_dirs[]`. This is what lets the
  self-promotion fire.
- `apply run`'s `eligible_queue()` takes `tailored` only, and `--job-id` is checked
  against that same queue, so a `saved` role is rejected by name there.

A board that DOES need a cover letter blocks inside the plan-check either way.
