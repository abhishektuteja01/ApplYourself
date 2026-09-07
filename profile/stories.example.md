# TEMPLATE — copy to profile/stories.md and replace every entry with your own.
#
# Behavioral-answer material. Read by /apply for Tier C1 questions ("the hardest
# thing you worked on", "a time you were wrong") and by /interview as drill
# targets. bullets.md answers what you built; this file answers how you decided.
#
# THE RULE, same as bullets.md: an entry's prose is what you will stand behind
# on a call. A drafted answer may use only words already in this entry's
# situation/action/outcome/retrospect text or an entry in its
# allowable_synonyms. No new numbers, no invented tools.
#
# Entry format (all nine keys, one blank line between entries):
#   ## S-<CTX>-<SLUG>    <CTX> matches the bullets.md context tag for the same
#                        project; <SLUG> is a short name you invent.
#   source:              where the work happened, with dates. Same string as
#                        the bullets.md entries for that project.
#   kind:                the routing key. /apply maps a question label to a
#                        kind, never straight to a project. One of:
#                        bug_caught | judgment_call | reversal |
#                        negative_result | hardest | conflict | failure
#   anchors:             the B- ids in bullets.md this story defends. Must be
#                        real ids. /interview drills the story against them.
#   situation:           the setup, 1-2 sentences. What made it hard or unclear.
#   action:              what you did. Specific, first person, no hedging.
#   outcome:             what happened, with the number where you have one.
#   retrospect:          what you would do differently, or why the call still
#                        stands. This is the field that makes "what would you
#                        change" answerable at all.
#   tags:                snake_case list, your own grouping vocabulary.
#   allowable_synonyms:  pre-approved re-packagings of the SAME story. As with
#                        bullets.md, a synonym that widens the claim is a
#                        fabrication with extra steps.
#
# Unlike bullets.md, story prose gets NO diction-lint exemption. It is fresh
# generation into a form field, same as outreach, so it must survive the banned
# phrase list in profile/de_ai_rules.yaml on its own.
#
# Which story wins when several match a question is set per vertical, by
# `C1 story priority` in profile/verticals/<name>/tailoring.md.
#
# The entries below are fictional, and match the widget/sprocket/cog world of
# the committed profile/verticals/example_* lanes.

## S-SPR-BANDS
source: Sprocket compliance side project (Jul 2025 - Oct 2025)
kind: bug_caught
anchors: [B-SPR-01]
situation: The validation harness scored every historical measurement against the published tolerance bands, and the pass rate came out higher than the vendor's own quality report.
action: I pinned the record count the harness expected and found it was reading 12,400 rows against the 12,000 in the dataset. Four hundred rows from an earlier export had never been cleared. I added a count assertion that runs before scoring and fails loudly.
outcome: The duplicate rows had been inflating the pass rate. Every batch number changed after the fix, and the count assertion has caught two similar mistakes since.
retrospect: The mismatch was in the log for a week before I read it. I now pin an expected row count next to any dataset I score more than once.
tags: [data_integrity, validation, debugging]
allowable_synonyms: ["duplicate record contamination", "row-count assertion", "data integrity check", "stale export", "silent data defect"]

## S-COG-SCOPE
source: Cog training assistant (personal project, Nov 2025 - Feb 2026)
kind: judgment_call
anchors: [B-COG-01, B-COG-02]
situation: The plan included a second retrieval mode that would have needed its own index alongside the one already built.
action: I sized it before building it. The second index would have been about forty times the storage of the first, for an advantage that only shows up on documents longer than anything in the corpus.
outcome: I dropped the mode and wrote the sizing calculation into the project notes as the reason, so the decision is checkable rather than remembered.
retrospect: The call still stands, and the reason matters more than the call: the arithmetic depends on the corpus, so a longer-document corpus would argue the other way.
tags: [architecture_decisions, capacity_planning, scoping]
allowable_synonyms: ["scoped out on measured grounds", "sized before building", "storage estimate", "documented the decision"]

## S-WID-HANDOFF
source: Widget Corp, Widget Operations Analyst (Exampletown, Jan 2023 - Jun 2025)
kind: conflict
anchors: [B-WID-02]
situation: Two teams disagreed on which of them owned a recurring reconciliation break, and the break stayed open through three reporting cycles while they argued.
action: I stopped arguing about ownership and reproduced the break end to end in the test environment, then wrote up the exact step where the handoff dropped the record. The write-up made the owner obvious without anyone having to concede a position.
outcome: The break closed in one cycle, and the write-up became the template the team used for the next two disputed defects.
retrospect: I spent two cycles trying to settle it in meetings first. A reproduction is a faster argument than an opinion.
tags: [stakeholder_management, defect_investigation, communication]
allowable_synonyms: ["cross-team disagreement", "ownership dispute", "reproduced the defect", "written root cause", "resolved without escalation"]
