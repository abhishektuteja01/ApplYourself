---
description: Research a company and draft company_answers.md (why_company/why_role/what_interests_you_about_product) into a job_id's existing /tailor output dir. Extracted from /cover-letter Step 2b so /apply can call it directly for roles that need no full letter.
model: sonnet
effort: medium
allowed-tools:
  - Bash
  - Read
  - Write
  - WebSearch
  - WebFetch
argument-hint: <job_id>
---

# /company-answers — research + draft company_answers.md, no letter

Arguments: `$1` is the 8-hex `job_id`.

**Before anything else, read `.claude/shared/no_fab.md`.** This command
cites NO-FAB and REPHRASE-LICENSE by name.

## Step 1 — prerequisites + tailor dir (one block, fail loud)

```bash
cd "$(git rev-parse --show-toplevel)" || { echo "ERROR: not inside the repo."; exit 1; }
JOB_ID="$1"

test -n "$JOB_ID" || { echo "ERROR: /company-answers requires a job_id argument."; exit 1; }
test -f "pipeline/$1/state.yaml" || { echo "ERROR: pipeline/$1/state.yaml missing. Run /track $1 saved first."; exit 1; }
test -f profile/bullets.md || { echo "ERROR: profile/bullets.md missing."; exit 1; }
test -f profile/de_ai_rules.yaml || { echo "ERROR: profile/de_ai_rules.yaml missing."; exit 1; }

LATEST_DIR=$(uv run python -c "
import yaml
data = yaml.safe_load(open('pipeline/$1/state.yaml')) or {}
dirs = data.get('tailored_dirs') or []
print(dirs[-1] if dirs else '')
")
if [ -z "$LATEST_DIR" ]; then
  echo "ERROR: pipeline/$1/state.yaml has no tailored_dirs[] entries. Run /tailor $1 first."
  exit 1
fi
OUT_DIR="applications/${LATEST_DIR}"
test -d "$OUT_DIR" || { echo "ERROR: ${OUT_DIR} referenced by state.yaml does not exist on disk."; exit 1; }
test -f "${OUT_DIR}/jd_snapshot.md" || { echo "ERROR: ${OUT_DIR}/jd_snapshot.md missing."; exit 1; }

TODAY=$(date "+%B %-d, %Y")
ENV_FILE="/tmp/company_answers_$1_env.sh"
{
  printf 'OUT_DIR=%q\n' "$OUT_DIR"
  printf 'TODAY=%q\n'   "$TODAY"
} > "$ENV_FILE"
cat "$ENV_FILE"
echo "reusing tailor dir: ${OUT_DIR}"
```

If ANY check fails, exit immediately. **No partial work.**

Every later Bash block re-sources this file first:

```bash
cd "$(git rev-parse --show-toplevel)" && . /tmp/company_answers_$1_env.sh
```

## Step 2 — load context

`Read` in full:
- `${OUT_DIR}/jd_snapshot.md` — the frozen JD
- `profile/bullets.md` — canonical bullets + `allowable_synonyms` (only
  source of factual claims)

Skip re-reading `profile/bullets.md` if already read in full this session
with no change signal. Always read `${OUT_DIR}/jd_snapshot.md` — it's
per-job.

## Step 3 — company research (always attempt, never blocking)

Find the company's actual focus, not its marketing tagline — specific
enough to show you looked.

Search using the company name **plus a disambiguating detail from
`jd_snapshot.md`** (industry/domain phrase, or HQ/location if stated) —
many portfolio-company and common-word names collide with unrelated
companies on a bare-name search. If the ATS slug (job URL /
`boards.greenhouse.io/<slug>`) suggests an obvious domain, prefer fetching
that company's own About/Mission page directly over a generic web search.

Pull at most 1-2 concrete themes — what they build, who they serve, a
stated focus tied to this JD's work — never the homepage tagline verbatim.
If the search returns nothing specific, the wrong company, or only
marketing copy, drop this step and write `INSUFFICIENT_RESEARCH` sections
below. Never blocks.

Write `${OUT_DIR}/company_answers.md` (fabrication discipline: any sentence
touching your own experience traces to a `profile/bullets.md` bullet, and
REPHRASE-LICENSE binds; the plain-language escape covers only a sentence
making no claim about you — which every section below normally is):

```
# company_answers.md
job_id: $1
company: <company name>
researched_at: <TODAY, YYYY-MM-DD>

## why_company
<2-4 sentences, grounded in the research above — INSUFFICIENT_RESEARCH if it
found nothing specific, the wrong company, or only marketing copy>

## why_role
<2-4 sentences, grounded in jd_snapshot.md's actual work — this one almost
never has to be INSUFFICIENT_RESEARCH, since the JD itself is the source>

## what_interests_you_about_product
<2-4 sentences, or INSUFFICIENT_RESEARCH under the same rule as why_company>
```

`INSUFFICIENT_RESEARCH` is the literal token, not a sentence — a correct
outcome, not a failure. `/apply` parks a role on that section rather than
submitting a fabricated "why us."

## Step 4 — lint loop

```bash
cd "$(git rev-parse --show-toplevel)" && . /tmp/company_answers_$1_env.sh
uv run python <<PYEOF
import re
from pathlib import Path
from src.lint import fix_mechanical, find_phrase_violations, load_de_ai_rules

company_path = Path('${OUT_DIR}/company_answers.md')
rules = load_de_ai_rules()
parts = re.split(r'^## (.+)$', company_path.read_text(), flags=re.MULTILINE)
preamble = parts[0]
it = iter(parts[1:])
sections = {h.strip(): b.strip() for h, b in zip(it, it)}

all_subs, all_violations = [], []
for section, text in sections.items():
    if text == 'INSUFFICIENT_RESEARCH':
        continue
    fixed, subs = fix_mechanical(text, rules)
    sections[section] = fixed
    all_subs.extend({**s, 'field': section} for s in subs)
    for v in find_phrase_violations(fixed, context='resume', exempt_lines=None, rules=rules):
        all_violations.append({**v, 'field': section})

rebuilt = preamble.rstrip('\n') + '\n\n' + '\n\n'.join(
    f'## {section}\n{text}' for section, text in sections.items()
) + '\n'
company_path.write_text(rebuilt)

import json
print(json.dumps({
    'mechanical_subs': len(all_subs),
    'violations': all_violations,
}, indent=2, default=str))
PYEOF
```

If `violations` is non-empty: rewrite the offending section using ONLY
words already in the relevant bullet's canonical text + its
`allowable_synonyms`, or (for a sentence tracing to no bullet at all) the
plain-language escape. Update `${OUT_DIR}/company_answers.md` directly,
re-run the block above, following `.claude/shared/lint_loop.md`'s attempt
cap and hard-refuse rule.

When `violations` is empty, proceed to Step 5.

## Step 5 — runtime assertions before reporting done

- [ ] `${OUT_DIR}/company_answers.md` exists, has all three `##` sections,
      each either 2-4 sentences or the literal `INSUFFICIENT_RESEARCH`
- [ ] Final lint pass (re-run Step 4) returns zero violations

If any check fails, do NOT report success — diagnose and fix.

## Step 6 — report

Tell the user:
```
${OUT_DIR}/company_answers.md written:
  why_company: <filled|INSUFFICIENT_RESEARCH>
  why_role: <filled|INSUFFICIENT_RESEARCH>
  what_interests_you_about_product: <filled|INSUFFICIENT_RESEARCH>

/apply's Tier C2 and /cover-letter's Step 2b will both read this file
instead of re-researching.
```
