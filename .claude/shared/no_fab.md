# Shared: the writing rules and the fix loop

Read by `/tailor`, `/cover-letter`, `/company-answers`, `/apply`, `/outreach`,
`/interview`, `/suggest-synonyms`, `/new-vertical`, `/tune-vertical` and
`/onboarding`.

## The rules

- **NO-FAB** — never introduce a tool, metric, scope, date or claim that no
  source attests. The sources are `profile/bullets.md` (claims),
  `profile/skills_master.md` (skills) and `profile/resume.yaml` (the fixed
  lines). A sentence about the company or the role may use the JD and your
  research. A sentence about you may only use your bullets.
- **NO-DRIFT** — analogy is not equivalence. A specialized module's workflow is
  not the generic workflow it resembles, and integration-level exposure is not
  ownership. Relabeling that asserts equivalence is fabrication, even when it
  reads naturally.
- **REPHRASE-LICENSE** — a reworded bullet may use only the words in that
  bullet's canonical text, its `allowable_synonyms`, and the stopwords in
  `profile/de_ai_rules.yaml`.
- **SKILLS-SOURCE** — a Skills line lists only `skills_master.md` entries whose
  `show` names that line. A skill is shown by its `name` or one of its
  `allowable_synonyms`.

## What code checks

`uv run app build ...` checks all of this and reports each break as a problem
with a `code`. `uv run app schema resume|letter|answers` prints the keys each
file may hold.

| code | means |
|---|---|
| `unknown_id` | a bullet or skill id that does not exist |
| `context`, `budget`, `layout` | the plan breaks the lane's shape: a context not on this lane, a bullet count outside `{min, max}`, a skill on the wrong line, or a missing or extra key |
| `license` | a reworded bullet uses a word outside REPHRASE-LICENSE |
| `number` | a number with no source: on a resume, the cited bullets, a context header or the baseline summary; in a letter or answers, the JD, a context header or a `uses` bullet |
| `skill` | a summary names a skill no cited bullet backs, or an alias that is not an allowable synonym |
| `lint` | a banned phrase from a `level: block` rule in `profile/de_ai_rules.yaml`; a `level: warn` hit is listed under `warnings` and never fails the build. Company names, the recipient's name, skill names, context titles and orgs, URLs and dates are never flagged |
| `pages` | the pdf runs past one page, or "page count unknown": no pdf was made. That one is not a plan problem: do not edit, stop and report it (`--no-pdf` passes without a page count, only when the user asks) |

Mechanical fixes (dashes, smart quotes, ellipses, odd spaces) are applied
silently and never count as problems. Source files are checked once, by
`uv run verticals-check`, so a build only checks the text you wrote.

## The fix loop

1. Fix exactly what each problem names, under the rules above. A fix that
   adds a new claim is worse than the phrase it removed.
2. Re-run the same build.
3. Stop after 5 builds. A resume then runs once with `--final`. For prose,
   delete each sentence that still fails, then build once more. If it still
   fails, stop and report the problem, the phrase and where it is.

Never ship a flagged problem, and never delete a file you did not write this
run. A failed build writes nothing but its report.
