---
description: Generate a one-page, lint-clean cover letter for a job_id into that role's existing /tailor output dir. Same no-fabrication discipline as /tailor.
model: sonnet
effort: medium
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Skill
  - WebSearch
  - WebFetch
argument-hint: <job_id> [--to "Hiring Manager Name"]
---

# /cover-letter — generate a tailored, audited cover letter

Write a cover letter for one role, reusing the most recent `/tailor` output for
that `job_id`.

Arguments: `$1` is the 8-hex `job_id`. Parse `$ARGUMENTS` for an optional
`--to "Name"` flag (same convention as `/outreach`'s `--to`/`--via`): if
present, the salutation becomes `Dear Name,` instead of the default
`Dear Hiring Manager,`.

---

**Before anything else, read `.claude/shared/no_fab.md`.** This command
cites NO-FAB and REPHRASE-LICENSE by name; their definitions live there,
not in this file.

## Step 1 — prerequisites + tailor dir + template placeholders (one block, fail loud)

`tailored_dirs[]` entries are vertical-prefixed, so `OUT_DIR=applications/${LATEST_DIR}`
resolves as-is. Run everything below as ONE bash block:

```bash
# Every path below is repo-relative, and src/ resolves its own paths from
# the repo root — so anchor the shell there too.
cd "$(git rev-parse --show-toplevel)" || { echo "ERROR: not inside the repo."; exit 1; }
JOB_ID="$1"

test -n "$JOB_ID" || { echo "ERROR: /cover-letter requires a job_id argument."; exit 1; }
test -f "pipeline/$1/state.yaml" || { echo "ERROR: pipeline/$1/state.yaml missing. Run /track $1 saved first."; exit 1; }
test -f profile/bullets.md || { echo "ERROR: profile/bullets.md missing."; exit 1; }
test -f profile/de_ai_rules.yaml || { echo "ERROR: profile/de_ai_rules.yaml missing."; exit 1; }
test -f profile/cover_letter_template.docx || { echo "ERROR: profile/cover_letter_template.docx missing. Save your cover letter design there with placeholder paragraphs: {{SALUTATION}} and {{BODY}} required, {{DATE}}/{{CLOSING}}/{{SIGNOFF_NAME}} optional."; exit 1; }

LATEST_DIR=$(uv run python -c "
import yaml
data = yaml.safe_load(open('pipeline/$1/state.yaml')) or {}
dirs = data.get('tailored_dirs') or []
print(dirs[-1] if dirs else '')
")
if [ -z "$LATEST_DIR" ]; then
  echo "ERROR: pipeline/$1/state.yaml has no tailored_dirs[] entries. Run /tailor $1 first -- /cover-letter reuses its jd_snapshot.md and keywords_to_mirror.md rather than re-parsing the JD."
  exit 1
fi
OUT_DIR="applications/${LATEST_DIR}"
test -d "$OUT_DIR" || { echo "ERROR: ${OUT_DIR} referenced by state.yaml does not exist on disk."; exit 1; }
test -f "${OUT_DIR}/jd_snapshot.md" || { echo "ERROR: ${OUT_DIR}/jd_snapshot.md missing."; exit 1; }
test -f "${OUT_DIR}/keywords_to_mirror.md" || { echo "ERROR: ${OUT_DIR}/keywords_to_mirror.md missing."; exit 1; }

# tailored_dirs[] entries are vertical-prefixed, so the lane is the first path
# segment. APPLICANT_NAME/FILE_SLUG come from that vertical's resume_file (its
# first bold line) -- no name is hardcoded in this file.
VERTICAL="${LATEST_DIR%%/*}"
eval "$(uv run tailor-prep identity "$VERTICAL")" || exit 1
test -n "$FILE_SLUG" || { echo "ERROR: no FILE_SLUG for vertical ${VERTICAL} -- its resume_file needs a bold name line."; exit 1; }

TODAY=$(date "+%B %-d, %Y")

# Persist the resolved values: shell state does NOT survive between Bash calls,
# and `eval` above consumed prep's stdout so nothing printed APPLICANT_NAME or
# FILE_SLUG for later steps to substitute.
ENV_FILE="/tmp/cover_letter_$1_env.sh"
{
  printf 'VERTICAL=%q\n'        "$VERTICAL"
  printf 'OUT_DIR=%q\n'         "$OUT_DIR"
  printf 'APPLICANT_NAME=%q\n'  "$APPLICANT_NAME"
  printf 'FILE_SLUG=%q\n'       "$FILE_SLUG"
  printf 'TODAY=%q\n'           "$TODAY"
} > "$ENV_FILE"
cat "$ENV_FILE"
uv run python -c "
from pathlib import Path
from src.docx_cover_letter import list_cover_letter_placeholders
print('placeholders:', sorted(list_cover_letter_placeholders(Path('profile/cover_letter_template.docx'))))
"
echo "reusing tailor dir: ${OUT_DIR}"
```

If ANY check fails, exit immediately. **No partial work.**

**Every later Bash block starts by re-sourcing the printed values.** Bash
variables do not persist between Bash calls:

```bash
cd "$(git rev-parse --show-toplevel)" && . /tmp/cover_letter_$1_env.sh
```

Use the helper above, never a hand-rolled `paragraph.text` scan: Word wraps
placeholders in content controls that `paragraph.text` silently misses. If
`{{SALUTATION}}` or `{{BODY}}` is absent from the printed list, hard-refuse and
tell the user to add it to `profile/cover_letter_template.docx`.

## Step 2 — load context

`Read` these files in full:
- `${OUT_DIR}/jd_snapshot.md` — the frozen JD this letter must speak to
- `${OUT_DIR}/keywords_to_mirror.md` — the 2-3 keywords the resume already mirrors; mirror the same ones here for consistency
- `profile/bullets.md` — canonical bullets + `allowable_synonyms` per bullet (the ONLY source of factual claims)

Skip re-reading `profile/bullets.md` if it was already read in full this session
with no change signal. The two `${OUT_DIR}` files are per-job — always read them.
When in doubt, re-read.

## Step 2b — company mission research (always attempt, never blocking)

Find the company's actual focus, not its marketing tagline — specific enough to
show you looked.

Search for it using the company name **plus a disambiguating detail
pulled from `jd_snapshot.md`** (industry/domain phrase from the JD body,
or HQ/location if the JD states one) — many portfolio-company and
common-word names (e.g. "Distyl", "The Agentic Loop", "Glean") collide
with unrelated companies on a bare-name search. If the ATS slug in
`companies.yaml`/the job URL suggests an obvious domain (e.g.
`boards.greenhouse.io/<slug>` → try `<slug>.com`), prefer fetching that
company's own About/Mission page directly over a generic web search.

Pull at most 1-2 concrete themes — what they build, who they serve, a stated
focus tied to this JD's work — never the homepage tagline verbatim. If the search
returns nothing specific, the wrong company, or only marketing copy, drop this
step and draft without it. It never blocks Step 3.

**Second output: `${OUT_DIR}/company_answers.md`.** `/apply`'s Tier C2 resolves
the recurring company-specific application questions ("why us", "what excites
you about our product") from this file instead of searching at apply time — the
research above is otherwise used for one clause in the letter and then
discarded. Draft it now, right after the research above, using the same
fabrication discipline as the letter (Step 3's NO-FAB paragraph applies here
too: any sentence touching your own experience traces to a `bullets.md` bullet
and REPHRASE-LICENSE binds; the plain-language escape covers only a sentence
that makes no claim about you, which every section below normally is). Write:

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

`INSUFFICIENT_RESEARCH` is the literal token, not a sentence — `/apply` parks
the role on that section rather than submitting a fabricated "why us." Writing
it is a successful outcome here, not a failure; Step 2b staying non-blocking
does not change.

## Step 3 — draft the letter content

**Fabrication discipline (/tailor's NO-FAB, identical here):** every sentence
mapping your experience to the JD must trace to a specific bullet's
canonical text or that bullet's `allowable_synonyms`. No tools, metrics,
scopes, or dates beyond what `profile/bullets.md` attests. "Analogy is not
equivalence" applies here too — do not relabel a specialized process as its
generic industry cousin, or integration-level exposure as ownership, to chase JD
vocabulary. The vertical's `tailoring.md` names any relabel banned outright.

Unlike `/tailor`, the output here is NOT markdown rendered into freshly
created paragraphs — `profile/cover_letter_template.docx` is the user's
own design (letterhead, fonts, colors, spacing) and is preserved
byte-for-byte. You are only generating the TEXT that fills its
`{{SALUTATION}}` / `{{BODY}}` / optional `{{DATE}}` / `{{CLOSING}}` /
`{{SIGNOFF_NAME}}` placeholder paragraphs. Do not invent a
header/contact block — the template already has one.

This is fresh prose about a specific role, not a mail-merge — do not treat
the following as a fill-in-the-blanks template. There's no fixed
paragraph count or per-paragraph assignment; let the shape follow the
content. Across the letter, cover:
- the role and company, and a genuine reason for this role (fold in a
  Step 2b theme here ONLY if you found something specific — if not,
  ground the "why this role" in the JD's actual work, not the company)
- 2-3 `bullets.md` achievements mapped onto the JD's `keywords_to_mirror`,
  honest and specific, no invented scope
- a close

Just start — no filler opener announcing that this is an application.

Don't hand-police structural AI-tells here (tricolons, "not only X but
also Y", uniform rhythm, formulaic "I am writing to express my
interest..." / "I look forward to hearing from you" openers and closers)
— Step 4's no_ai_slop pass owns that cleanup. Just draft in plain, varied
prose and move on.

Write a JSON file `/tmp/cover_letter_$1_draft.json` with this shape:

```json
{
  "date": "June 16, 2026",
  "salutation": "Dear Hiring Manager,",
  "body": [
    "First paragraph text.",
    "Second paragraph text.",
    "Optional third paragraph text."
  ],
  "closing": "Sincerely,",
  "signoff_name": "<$APPLICANT_NAME from Step 1>"
}
```

- `salutation`: `"Dear Hiring Manager,"` unless `--to "Name"` was passed in `$ARGUMENTS`, then `"Dear Name,"`.
- `date`: the `TODAY` value Step 1 printed. Only needed if the template has a `{{DATE}}` placeholder — Step 1 already printed the template's actual placeholder list; use it. Omit keys from the JSON for placeholders the template doesn't have (the `{{SALUTATION}}`/`{{BODY}}` hard-refuse already happened in Step 1). If a placeholder the template DOES have (e.g. `{{CLOSING}}`) has no natural generated value, still include the key with an empty string -- the renderer blanks unfilled-but-present placeholders rather than leaving the raw `{{TOKEN}}` text in the letter, but it's cleaner for the JSON to be explicit.
- 2-3 `body` entries is typical; aim for 230-300 words total across them so the letter fits one page in the template's own layout. Measured ceilings in this template: 3 paragraphs hold ~330 words, 4 hold ~300. Dropping from 4 paragraphs to 3 is the lever when a letter needs the room.

## Step 4 — no_ai_slop editing pass (before lint)

Run the `no_ai_slop` skill in **edit** mode over the drafted prose — the
`salutation` plus every `body[]` entry from
`/tmp/cover_letter_$1_draft.json`. This is the deep pass for the
structural AI-tells the banned-phrase linter can't catch (binary
contrasts, colon reveals, importance puffery, summary-recap endings,
robotic rhythm, fake-profound kickers).

This is a voice/structure edit, NOT a rewrite of substance. The edit must
not add any claim, tool, metric, scope, or date beyond what
`profile/bullets.md` attests — no_ai_slop already forbids inventing
facts; hold that line here. Keep the letter's mapping to the
`keywords_to_mirror` intact.

Feed the skill the salutation + body paragraphs, take its edited prose,
and write the edited text back into the same JSON file (`salutation` +
`body[]`), preserving the JSON shape and leaving any
`date`/`closing`/`signoff_name` keys untouched. Then proceed to the lint
loop, which remains the final gate before render.

## Step 5 — lint loop (fully linted, no bullets.md exemption)

Cover letters are fresh-generated prose, not verbatim bullet text — there
is **no exemption** here, same as outreach. Lint
`salutation` + every `body` entry (skip `date`/`closing`/`signoff_name` --
they're not generated prose) **and** every non-`INSUFFICIENT_RESEARCH`
section of `company_answers.md` (§7b) — one loop covers both files, since a
violation in either blocks the same way.

```bash
cd "$(git rev-parse --show-toplevel)" && . /tmp/cover_letter_$1_env.sh
uv run python <<PYEOF
import json
import re
from pathlib import Path
from src.lint import fix_mechanical, find_phrase_violations, load_de_ai_rules

draft_path = Path('/tmp/cover_letter_$1_draft.json')
content = json.loads(draft_path.read_text())
rules = load_de_ai_rules()

lintable_fields = ['salutation'] + [f'body[{i}]' for i in range(len(content.get('body') or []))]
texts = [content['salutation']] + list(content.get('body') or [])

company_path = Path('${OUT_DIR}/company_answers.md')
company_preamble, company_sections = '', {}
if company_path.exists():
    parts = re.split(r'^## (.+)$', company_path.read_text(), flags=re.MULTILINE)
    company_preamble = parts[0]
    it = iter(parts[1:])
    company_sections = {h.strip(): b.strip() for h, b in zip(it, it)}

all_subs, all_violations = [], []
fixed_texts = []
for field, text in zip(lintable_fields, texts):
    fixed, subs = fix_mechanical(text, rules)
    fixed_texts.append(fixed)
    all_subs.extend({**s, 'field': field} for s in subs)
    for v in find_phrase_violations(fixed, context='resume', exempt_lines=None, rules=rules):
        all_violations.append({**v, 'field': field})

for section, text in company_sections.items():
    if text == 'INSUFFICIENT_RESEARCH':
        continue
    fixed, subs = fix_mechanical(text, rules)
    company_sections[section] = fixed
    all_subs.extend({**s, 'field': f'company_answers.{section}'} for s in subs)
    for v in find_phrase_violations(fixed, context='resume', exempt_lines=None, rules=rules):
        all_violations.append({**v, 'field': f'company_answers.{section}'})

content['salutation'] = fixed_texts[0]
content['body'] = fixed_texts[1:]
draft_path.write_text(json.dumps(content, indent=2))

if company_path.exists():
    rebuilt = company_preamble.rstrip('\n') + '\n\n' + '\n\n'.join(
        f'## {section}\n{text}' for section, text in company_sections.items()
    ) + '\n'
    company_path.write_text(rebuilt)

print(json.dumps({
    'mechanical_subs': len(all_subs),
    'mechanical_sample': all_subs[:5],
    'violations': all_violations,
}, indent=2, default=str))
PYEOF
```

(`context='resume'` with no `exempt_lines` passed is equivalent to "fully
linted, no exemption" here — it just also skips the outreach-only opener
list, which doesn't apply to cover letters.)

If `violations` is non-empty: rewrite the offending field's text using
ONLY words already in the relevant bullet's canonical text + its
`allowable_synonyms`. The plain-language escape applies to exactly one case —
prose that traces to NO bullet at all, such as the salutation, the closing, a
company-mission sentence from Step 2b, or a `company_answers.md` section. If
the sentence carries any claim about your experience, it traces to a bullet and
REPHRASE-LICENSE binds; the escape is not available. Update the relevant file
(`/tmp/cover_letter_$1_draft.json` or `${OUT_DIR}/company_answers.md`,
matching the `field` the violation named), and re-run the lint above, following
`.claude/shared/lint_loop.md` — read it; it holds the attempt cap and the
hard-refuse rule.

When `violations` is empty, proceed to Step 6.

## Step 6 — render the docx

```bash
cd "$(git rev-parse --show-toplevel)" && . /tmp/cover_letter_$1_env.sh
uv run python -c "
import json
from pathlib import Path
from src.docx_cover_letter import render_cover_letter
content = json.loads(Path('/tmp/cover_letter_$1_draft.json').read_text())
render_cover_letter(content, Path('profile/cover_letter_template.docx'), Path('${OUT_DIR}/${FILE_SLUG}_Cover_Letter.docx'))
print('rendered:', '${OUT_DIR}/${FILE_SLUG}_Cover_Letter.docx')
"
```

If render raises `TemplateMissingError` or `TemplateError`, surface the
message verbatim and stop — `TemplateError` here means the template is
missing `{{SALUTATION}}` or `{{BODY}}`; tell the user to add the missing
placeholder paragraph(s) to `profile/cover_letter_template.docx`.

Then convert to PDF: set the variables and follow
`.claude/shared/render_pdf.md` verbatim.

```bash
cd "$(git rev-parse --show-toplevel)" && . /tmp/cover_letter_$1_env.sh
BASENAME=Cover_Letter
```

Read that file now and run its block. Do not reconstruct the AppleScript from
memory — the fixed staging dir it uses is what keeps Word's sandbox grant valid.

## Step 7 — append the dir to state.yaml.cover_letters[]

This is the side-list mutation `/cover-letter` is allowed
(same pattern as `/tailor`'s `tailored_dirs[]`). The transition itself happens in
Step 7b, and goes through `/track` (R10).

```bash
cd "$(git rev-parse --show-toplevel)" && . /tmp/cover_letter_$1_env.sh
uv run python -c "
from pathlib import Path
from src.state_io import state_path_for, append_cover_letter
p = state_path_for(Path('pipeline'), '$1')
data = append_cover_letter(p, '$LATEST_DIR')
print(f'cover_letters[] now has {len(data[\"cover_letters\"])} entry/entries')
"
```

## Step 7b — transition `saved` -> `tailored`

Both artifacts now exist, which is exactly `/apply`'s eligibility bar, so this is
the point where the role becomes submittable. Runs `uv run track`, so `/track`
stays the only writer (R10).

**Only fires from `saved`.** `transition()` permits same-state and backwards
moves, so an unguarded call would drag an `applied` or `screen` role back to
`tailored` on a re-run. Any state other than `saved` is left untouched.

```bash
cd "$(git rev-parse --show-toplevel)"
CURRENT=$(uv run python -c "
from pathlib import Path
from src.state_io import state_path_for, load_state
print((load_state(state_path_for(Path('pipeline'), '$1')) or {}).get('state', ''))
")
if [ "$CURRENT" = "saved" ]; then
    uv run track "$1" tailored --note "resume + cover letter on file"
else
    echo "state is '${CURRENT}', not 'saved' — leaving it alone (no transition)."
fi
```

## Step 8 — runtime assertions before reporting done

Before reporting success, verify on disk:
- [ ] `${OUT_DIR}/${FILE_SLUG}_Cover_Letter.docx` exists and is non-empty
- [ ] `${OUT_DIR}/${FILE_SLUG}_Cover_Letter.pdf` exists and is non-empty (warn but don't fail if osascript errored)
- [ ] `${OUT_DIR}/company_answers.md` exists and has all three `##` sections
      (§7b) — each is either 2-4 sentences or the literal `INSUFFICIENT_RESEARCH`
- [ ] One final lint pass returns zero violations. Step 5's Python reads
      `/tmp/cover_letter_$1_draft.json` and `${OUT_DIR}/company_answers.md`, so
      re-running it verbatim re-checks both drafts, not the rendered docx —
      which is the intent here, since the docx is generated FROM the JSON.
- [ ] Body word count is within 230-300 (count the `body` entries' words; /outreach
      asserts its channel limit, and this letter has to fit one page the same way)
- [ ] The rendered PDF is exactly one page: `pdfinfo "${OUT_DIR}/${FILE_SLUG}_Cover_Letter.pdf" | awk '/^Pages:/{print $2}'`
      returns `1`. Word count is a guard rail, not the test — paragraph count and
      ragged line breaks move the real boundary, so assert the page count itself.
      If it returns 2, cut the body (or merge two paragraphs into one) and re-render.
      Trust `pdfinfo`, not `mdls` — Spotlight metadata lags behind the file.
- [ ] `pipeline/$1/state.yaml.cover_letters[]` contains `${LATEST_DIR}`

If any check fails, do NOT report success — diagnose and fix.

## Step 9 — report

Tell the user:
```
Cover letter written into: ${OUT_DIR}/
  - ${FILE_SLUG}_Cover_Letter.docx ({size} bytes)
  - ${FILE_SLUG}_Cover_Letter.pdf ({size} bytes)
  - company_answers.md ({why_company/why_role/what_interests_you_about_product
    status: filled or INSUFFICIENT_RESEARCH})

state.yaml.cover_letters[] now references this dir. /apply's Tier C2 will read
company_answers.md instead of parking on the recurring "why us" questions.

Next:
  1. Open ${FILE_SLUG}_Cover_Letter.docx -- confirm no fabricated claims and no
     scope-widening drift against profile/bullets.md.
  2. Submit manually alongside ${FILE_SLUG}_Resume.docx, or run /apply.
```
