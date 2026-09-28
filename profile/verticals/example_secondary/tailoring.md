# example_secondary tailoring guidance (TEMPLATE)

Read by `/tailor` (and `/apply` for the C1 priority). The resume's shape lives
in the sources: headers and budgets in `profile/resume.yaml`, baseline picks in
`profile/bullets.md` (`default_for`), skill lines in `profile/skills_master.md`
(`show`) and this lane's `resume:` block in `profile/verticals.yaml`. This file
holds only judgment: how to move from that baseline toward one JD. Name
contexts and projects in words; ids belong in the sources.

**Summary framing:** Lead with the project and domain evidence; mention the employer experience second, as supporting professional experience.

**Changing the picks:** Pick the employer bullet that reframes toward this lane through its existing `allowable_synonyms`. A JD on LLM tooling can lead with the cog assistant. Within a context, bullets keep their canonical
order: the JD changes which bullets appear, not their sequence.

**C1 story priority:** Sprocket Validator > Cog Assistant > Widget Corp. Applies to Tier C1 application-form answers only,
not resume bullets. A story in profile/stories.md whose `kind` matches the
question beats a higher-priority source whose `kind` does not; remaining ties
break on `keywords_to_mirror` overlap.
