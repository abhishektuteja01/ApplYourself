---
paths:
  - "src/lint.py"
  - "profile/de_ai_rules.yaml"
---

# Linting (`src/lint.py`)

Two tiers: Tier 1 mechanical fixes (dashes, smart quotes, ellipsis, NBSP,
zero-width) auto-applied to everything; Tier 2 banned-phrase violations
(`profile/de_ai_rules.yaml`) are flagged only — the command session loops the LLM
to rewrite until the linter returns empty. Verbatim canonical `bullets.md` text is
exempt from Tier 2 only when `bullets_diction_pass_completed: true`; outreach is
never exempt.
