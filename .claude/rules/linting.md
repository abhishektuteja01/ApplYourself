---
paths:
  - "src/lint.py"
  - "src/app_cli.py"
  - "profile/de_ai_rules.yaml"
---

# Linting (`src/lint.py`)

Two tiers: Tier 1 mechanical fixes (dashes, smart quotes, ellipsis, odd spaces)
auto-applied, with every replacement value read from `profile/de_ai_rules.yaml`;
Tier 2 banned-phrase violations are flagged only, and the command session
rewrites until the linter returns empty (the fix loop in
`.claude/shared/no_fab.md`).

- Tier 2 is the `rules:` table (`schema_version: 2`): each entry's `match`
  (`word | sentence_start | regex`), `scope` (`resume`, `outreach`), `level`
  (`block` fails a build, `warn` is reported under `warnings`) and optional
  `unless_before: number` decide behaviour. A `label` only names the rule.

- One dash rule for en and em dashes: a dash joining dates, numbers or day names
  is a range; anything else is an aside.
- Link targets, URLs, emails, month names used as dates and the caller's `keep`
  terms (company, recipient, skill names and aliases, context titles and orgs)
  are never scanned.
- `sentence_start` rules are checked at every sentence start.
- Sources (`bullets.md`, `skills_master.md`, `resume.yaml`, lane summaries) are
  linted once by `verticals-check`; builds lint only generated text.
