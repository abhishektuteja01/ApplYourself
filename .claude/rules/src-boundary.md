---
paths:
  - "src/**"
---

# The determinism boundary (R7)

**No module under `src/` ever calls an LLM.** `src/` is deterministic plumbing:
parquet I/O, config loading, cleaning, linting, docx rendering, state transitions.
All *judgment* (scoring a job, tailoring a resume, writing a cover letter/outreach)
happens inside a slash-command session in `.claude/commands/*.md`, which calls the
`src/` helpers via Bash for the deterministic parts. When editing, keep judging out
of `src/` and keep parquet/state mutation out of the command prose.

`.claude/hooks/r7_no_llm_in_src.sh` enforces this after every edit to `src/`. It
matches import lines and LLM API endpoints, so the `"claude-sonnet-5"` model string
`score_cli` prints for the judge subagent and the `B0-LLM` answer-tier labels do not
trip it.

## `src/` is vertical-agnostic and company-agnostic

Never hardcode a vertical name, search term, or company. Those come only from
`profile/*.yaml` and `data/universe/*.csv`. ATS vendor names and job-board names
are structurally required and do not count.

## Config access

Call `verticals.get_config()` **inside function bodies**, never at module level, so
test injection via `set_config()` always wins. The one instance-capture to watch is
`discovery/orchestrator.py`'s `Context.__init__`, which is compliant but holds the
config for the run's lifetime.

`src/paths.py` is re-exported, not imported through: modules bind their own
module-level names (`CLEAN = paths.CLEAN`) because tests patch those local names.
A global path change touches one file; a test override needs the right alias.
