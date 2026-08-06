# Shared: the lint loop contract

Read by `/tailor`, `/cover-letter` and `/outreach`.

Tier 1 (mechanical: dashes, smart quotes, ellipsis, NBSP, zero-width) is applied
automatically and is never a violation you resolve — `src/lint.py` fixes it and
reports what it changed.

Tier 2 (banned phrases from `profile/de_ai_rules.yaml`) is flagged, never
auto-fixed. The loop:

1. Rewrite the flagged text under the rules in `.claude/shared/no_fab.md` — a
   rewrite that introduces a new claim is worse than the phrase it removed.
2. Re-run the same lint command on the same file.
3. Repeat, **at most 5 attempts total**.

If violations remain after 5 attempts, **hard-refuse**: write no output files
(delete any partial output dir), and tell the user which phrase and which
category kept failing, plus the line it is on.

**Never ship a flagged phrase.** Not with a warning, not with a note in the
report, not "resolved in spirit". The linter returning empty is the only exit.

Outreach text is **never** exempt from Tier 2, whatever
`bullets_diction_pass_completed` says.
