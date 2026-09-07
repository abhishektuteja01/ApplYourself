# example_tertiary tailoring defaults (TEMPLATE)

See `../example_primary/tailoring.md` for the full template notes. This file
shows the shape a lane takes when employer experience and project evidence
carry roughly equal weight.

**Bullet budget (page-budget hard floor):** 5 employer + 6 project bullets =
11 non-frozen bullets minimum.

**Project ordering default:** Project A (1) > Project B (2) > Project C (3).
JD fine-tune allowed within the default.

**C1 story priority:** Project A > Project B > Project C. Applies to Tier C1
application-form answers only, not resume bullets. A story in
profile/stories.md whose `kind` matches the question beats a
higher-priority project whose `kind` does not; remaining ties break on
`keywords_to_mirror` overlap.

**Summary framing:** lead with the stack the JD names, backed by the employer
scope that proves it.

**Skills layout (4 lines):** render these category lines in this order. For
each line, `/tailor` selects + orders the listed SKILLs per the ranking in
tailor.md (`vertical_lean` + `keywords_to_mirror`), rendering each entry's
`name` or an `allowable_synonyms` alias. A SKILL not listed on any line never
appears for this vertical. JD content fine-tunes ordering WITHIN a line and
never adds/drops a line or moves a SKILL to another line. Replace these
category names and `SKILL-<ID>`s with your own from `profile/skills_master.md`.

1. **Programming:** SKILL-PYTHON, SKILL-TYPESCRIPT, SKILL-SQL
2. **AI & Machine Learning:** SKILL-COG-TRAINING, SKILL-LLM-APPS, SKILL-RAG
3. **Databases & Tools:** SKILL-POSTGRES, SKILL-DOCKER, SKILL-GIT
4. **Domain & Enterprise:** SKILL-COG-PLATFORM

**Section order:** SUMMARY → WORK EXPERIENCE → PROJECTS → EDUCATION →
TECHNICAL SKILLS.
