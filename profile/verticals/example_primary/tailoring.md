# example_primary tailoring defaults (TEMPLATE)

Read by `/tailor` for rows whose `vertical` is this one. Every rule below is
a DEFAULT: the vertical sets it, the JD's wording may fine-tune within it,
never the reverse. Rewrite each section for your own profile.

**Bullet budget (page-budget hard floor):** 4 employer + 1 secondary-role +
5 project bullets = 10 non-frozen bullets minimum. Production employer
experience is this vertical's primary evidence.

**Project ordering default:** Project A > Project B > Project C, 2/2/1
bullets. JD fine-tune: a JD leaning hard on Project B's domain may lead
with it.

**Summary framing:** lead with the production employer experience; personal
projects are the secondary strength.

**Skills layout (4 lines):** render these category lines in this order. For
each line, `/tailor` selects + orders the listed SKILLs per the ranking in
tailor.md (`vertical_lean` + `keywords_to_mirror`), rendering each entry's
`name` or an `allowable_synonyms` alias. A SKILL not listed on any line never
appears for this vertical. JD content fine-tunes ordering WITHIN a line and
never adds/drops a line or moves a SKILL to another line. Replace these
category names and `SKILL-<ID>`s with your own from `profile/skills_master.md`.

1. **Domain & Enterprise:** SKILL-WIDGET-ASSEMBLY, SKILL-WIDGET-CONFIG, SKILL-GIZMO-LEDGER
2. **Programming:** SKILL-PYTHON, SKILL-SQL
3. **AI & Machine Learning:** SKILL-LLM-APPS, SKILL-RAG
4. **Databases & Tools:** SKILL-POSTGRES, SKILL-DOCKER, SKILL-GIT

**Section order:** SUMMARY → WORK EXPERIENCE → PROJECTS → EDUCATION →
TECHNICAL SKILLS.
