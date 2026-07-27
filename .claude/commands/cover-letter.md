---
description: Generate a one-page, lint-clean cover letter for a specific job_id, reusing the latest /tailor output dir (already vertical-prefixed, e.g. risk_ai/2026-... or sap/2026-...). Maps profile/bullets.md experience onto the JD's keywords_to_mirror with the same no-fabrication discipline as /tailor (R2/R3). Writes Abhishek_Tuteja_Cover_Letter.docx + Abhishek_Tuteja_Cover_Letter.pdf into the existing applications/<vertical>/<dir>/, then appends the same vertical-prefixed dir to pipeline/<job_id>/state.yaml.cover_letters[] (the side-list mutation /cover-letter is allowed, same pattern as /tailor's tailored_dirs[]).
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

You are writing a real cover letter for a specific role, reusing the most
recent `/tailor` output for that `job_id`. **This slice exists to enforce
R2 (no fabrication) and R5 (de-AI'd writing) for fresh-generated prose**.

Arguments: `$1` is the 8-hex `job_id`. Parse `$ARGUMENTS` for an optional
`--to "Name"` flag (same convention as `/outreach`'s `--to`/`--via`): if
present, the salutation becomes `Dear Name,` instead of the default
`Dear Hiring Manager,`.

---

## Step 1 — prerequisites + tailor dir + template placeholders (one block, fail loud)

`LATEST_DIR` already carries the vertical prefix /tailor wrote into
`tailored_dirs[]` (e.g. `risk_ai/2026-06-17_acme_..._a1b2c3d4`)
— no separate vertical lookup needed here, `OUT_DIR` resolves correctly
as-is. Run everything below as ONE bash block:

```bash
JOB_ID="$1"

test -n "$JOB_ID" || { echo "ERROR: /cover-letter requires a job_id argument."; exit 1; }
test -f "pipeline/${JOB_ID}/state.yaml" || { echo "ERROR: pipeline/${JOB_ID}/state.yaml missing. Run /track ${JOB_ID} saved first."; exit 1; }
test -f profile/bullets.md || { echo "ERROR: profile/bullets.md missing."; exit 1; }
test -f profile/de_ai_rules.yaml || { echo "ERROR: profile/de_ai_rules.yaml missing."; exit 1; }
test -f profile/cover_letter_template.docx || { echo "ERROR: profile/cover_letter_template.docx missing. Save your cover letter design there with placeholder paragraphs: {{SALUTATION}} and {{BODY}} required, {{DATE}}/{{CLOSING}}/{{SIGNOFF_NAME}} optional."; exit 1; }

LATEST_DIR=$(uv run python -c "
import yaml
data = yaml.safe_load(open('pipeline/${JOB_ID}/state.yaml')) or {}
dirs = data.get('tailored_dirs') or []
print(dirs[-1] if dirs else '')
")
if [ -z "$LATEST_DIR" ]; then
  echo "ERROR: pipeline/${JOB_ID}/state.yaml has no tailored_dirs[] entries. Run /tailor ${JOB_ID} first -- /cover-letter reuses its jd_snapshot.md and keywords_to_mirror.md rather than re-parsing the JD."
  exit 1
fi
OUT_DIR="applications/${LATEST_DIR}"
test -d "$OUT_DIR" || { echo "ERROR: ${OUT_DIR} referenced by state.yaml does not exist on disk."; exit 1; }
test -f "${OUT_DIR}/jd_snapshot.md" || { echo "ERROR: ${OUT_DIR}/jd_snapshot.md missing."; exit 1; }
test -f "${OUT_DIR}/keywords_to_mirror.md" || { echo "ERROR: ${OUT_DIR}/keywords_to_mirror.md missing."; exit 1; }

TODAY=$(date "+%B %-d, %Y")
echo "today: ${TODAY}"
uv run python -c "
from pathlib import Path
from src.docx_render import list_cover_letter_placeholders
print('placeholders:', sorted(list_cover_letter_placeholders(Path('profile/cover_letter_template.docx'))))
"
echo "reusing tailor dir: ${OUT_DIR}"
```

If ANY check fails, exit immediately. **No partial work.**

The placeholder check uses the shared helper, NOT a hand-rolled
`paragraph.text` scan — Word's built-in templates wrap placeholders in
content controls that plain `paragraph.text` silently misses. If
`{{SALUTATION}}` or `{{BODY}}` is missing from the printed list,
hard-refuse and tell the user to add it to
`profile/cover_letter_template.docx`.

## Step 2 — load context

`Read` these files in full:
- `${OUT_DIR}/jd_snapshot.md` — the frozen JD this letter must speak to
- `${OUT_DIR}/keywords_to_mirror.md` — the 2-3 keywords the resume already mirrors; mirror the same ones here for consistency
- `profile/bullets.md` — canonical bullets + `allowable_synonyms` per bullet (the ONLY source of factual claims)

Skip-reread rule: if `profile/bullets.md` was already read in full earlier
THIS session (e.g. by the `/tailor` run this command reuses) and you have
no signal it changed (no edit this session, no system reminder saying it
was modified), do not re-read it. The two `${OUT_DIR}` files are per-job;
always read them. When in doubt whether a file changed, re-read —
correctness beats the token saving.

## Step 2b — company mission research (always attempt, never blocking)

The company's real mission/focus (not marketing tagline) is a legitimate
thing to speak to in a cover letter — but only if it's specific enough to
prove you actually looked, not generic enough to paste into any letter.

Search for it using the company name **plus a disambiguating detail
pulled from `jd_snapshot.md`** (industry/domain phrase from the JD body,
or HQ/location if the JD states one) — many portfolio-company and
common-word names (e.g. "Distyl", "The Agentic Loop", "Glean") collide
with unrelated companies on a bare-name search. If the ATS slug in
`companies.yaml`/the job URL suggests an obvious domain (e.g.
`boards.greenhouse.io/<slug>` → try `<slug>.com`), prefer fetching that
company's own About/Mission page directly over a generic web search.

Pull at most 1-2 concrete themes (what they actually build, who they
serve, a stated focus tied to the JD's actual work) — not their homepage
tagline verbatim. If the search returns nothing specific, returns an
unrelated company, or only turns up generic marketing copy, drop this
entirely and draft without it. This step never hard-refuses or blocks
Step 3 — it's enrichment, not a prerequisite.

## Step 3 — draft the letter content

**Fabrication discipline (R2/R3, identical to /tailor):** every sentence
mapping your experience to the JD must trace to a specific bullet's
canonical text or that bullet's `allowable_synonyms`. No tools, metrics,
scopes, or dates beyond what `profile/bullets.md` attests. "Analogy is not
equivalence" applies here too — do not relabel the ACM commodity lifecycle
as generic SD Order-to-Cash to chase JD vocabulary.

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

Don't hand-police structural AI-tells here (tricolons, "not only X but
also Y", uniform rhythm, formulaic "I am writing to express my
interest..." / "I look forward to hearing from you" openers and closers)
— Step 4's no_ai_slop pass owns that cleanup. Just draft in plain, varied
prose and move on.

One cover-letter-specific call stays yours at draft time: if a Step 2b
mission theme didn't come back specific enough to prove real research,
leave it out entirely rather than reaching for a generic "I'm inspired by
your mission to..." line — that's a bigger AI tell than not mentioning it.

Write a JSON file `/tmp/cover_letter_${JOB_ID}_draft.json` with this shape:

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
  "signoff_name": "Abhishek Tuteja"
}
```

- `salutation`: `"Dear Hiring Manager,"` unless `--to "Name"` was passed in `$ARGUMENTS`, then `"Dear Name,"`.
- `date`: the `TODAY` value Step 1 printed. Only needed if the template has a `{{DATE}}` placeholder — Step 1 already printed the template's actual placeholder list; use it. Omit keys from the JSON for placeholders the template doesn't have (the `{{SALUTATION}}`/`{{BODY}}` hard-refuse already happened in Step 1). If a placeholder the template DOES have (e.g. `{{CLOSING}}`) has no natural generated value, still include the key with an empty string -- the renderer blanks unfilled-but-present placeholders rather than leaving the raw `{{TOKEN}}` text in the letter, but it's cleaner for the JSON to be explicit.
- 2-3 `body` entries is typical; aim for ~250-400 words total across them so the letter fits one page in the template's own layout.

## Step 4 — no_ai_slop editing pass (before lint)

Run the `no_ai_slop` skill in **edit** mode over the drafted prose — the
`salutation` plus every `body[]` entry from
`/tmp/cover_letter_${JOB_ID}_draft.json`. This is the deep pass for the
structural AI-tells the banned-phrase linter can't catch (binary
contrasts, colon reveals, importance puffery, summary-recap endings,
robotic rhythm, fake-profound kickers).

This is a voice/structure edit, NOT a rewrite of substance. The edit must
not add any claim, tool, metric, scope, or date beyond what
`profile/bullets.md` attests (R2) — no_ai_slop already forbids inventing
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
they're not generated prose).

```bash
uv run python <<PYEOF
import json
from pathlib import Path
from src.lint import fix_mechanical, find_phrase_violations, load_de_ai_rules

draft_path = Path('/tmp/cover_letter_${JOB_ID}_draft.json')
content = json.loads(draft_path.read_text())
rules = load_de_ai_rules()

lintable_fields = ['salutation'] + [f'body[{i}]' for i in range(len(content.get('body') or []))]
texts = [content['salutation']] + list(content.get('body') or [])

all_subs, all_violations = [], []
fixed_texts = []
for field, text in zip(lintable_fields, texts):
    fixed, subs = fix_mechanical(text, rules)
    fixed_texts.append(fixed)
    all_subs.extend({**s, 'field': field} for s in subs)
    for v in find_phrase_violations(fixed, context='resume', exempt_lines=None, rules=rules):
        all_violations.append({**v, 'field': field})

content['salutation'] = fixed_texts[0]
content['body'] = fixed_texts[1:]
draft_path.write_text(json.dumps(content, indent=2))

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
`allowable_synonyms` (or, if it's not tied to a specific bullet, just
rewrite in plain language avoiding the flagged phrase/category), update
the JSON file, and re-run the lint above. Loop up to 5 attempts. If still
failing after 5 attempts, hard-refuse: do not write any output files, tell
the user which phrase/category kept failing.

When `violations` is empty, proceed to Step 5.

## Step 6 — render the docx

```bash
uv run python -c "
import json
from pathlib import Path
from src.docx_render import render_cover_letter
content = json.loads(Path('/tmp/cover_letter_${JOB_ID}_draft.json').read_text())
render_cover_letter(content, Path('profile/cover_letter_template.docx'), Path('${OUT_DIR}/Abhishek_Tuteja_Cover_Letter.docx'))
print('rendered:', '${OUT_DIR}/Abhishek_Tuteja_Cover_Letter.docx')
"
```

If render raises `TemplateMissingError` or `TemplateError`, surface the
message verbatim and stop — `TemplateError` here means the template is
missing `{{SALUTATION}}` or `{{BODY}}`; tell the user to add the missing
placeholder paragraph(s) to `profile/cover_letter_template.docx`.

Then convert to PDF via Microsoft Word, routed through the same fixed staging
dir as `/tailor` Step 6 (Word's sandbox grant only reliably persists for one
unchanging path, not the new `${OUT_DIR}` each job gets):

```bash
STAGING="$(pwd)/.pdf_staging"
mkdir -p "$STAGING"
cp "${OUT_DIR}/Abhishek_Tuteja_Cover_Letter.docx" "${STAGING}/Abhishek_Tuteja_Cover_Letter.docx"
DOCX_ABS="${STAGING}/Abhishek_Tuteja_Cover_Letter.docx"
PDF_ABS="${STAGING}/Abhishek_Tuteja_Cover_Letter.pdf"
osascript <<ASEOF
tell application "Microsoft Word"
    open POSIX file "${DOCX_ABS}"
    set theDoc to active document
    save as theDoc file format format PDF file name "${PDF_ABS}"
    close active document saving no
end tell
ASEOF
if [ -s "${PDF_ABS}" ]; then
    cp "${PDF_ABS}" "${OUT_DIR}/Abhishek_Tuteja_Cover_Letter.pdf"
    rm -f "${DOCX_ABS}" "${PDF_ABS}"
    echo "pdf rendered: ${OUT_DIR}/Abhishek_Tuteja_Cover_Letter.pdf"
else
    echo "WARNING: PDF conversion via Word failed — docx is primary, PDF supplementary."
fi
```

If `osascript` fails (non-zero exit), surface the error and continue — the
docx is the primary artifact; the PDF is supplementary.

## Step 7 — append the dir to state.yaml.cover_letters[]

This is the side-list mutation `/cover-letter` is allowed
(same pattern as `/tailor`'s `tailored_dirs[]`). State transitions remain
`/track`'s sole job (R10).

```bash
uv run python -c "
from pathlib import Path
from src.state_io import state_path_for, append_cover_letter
p = state_path_for(Path('pipeline'), '$JOB_ID')
data = append_cover_letter(p, '$LATEST_DIR')
print(f'cover_letters[] now has {len(data[\"cover_letters\"])} entry/entries')
"
```

## Step 8 — runtime assertions before reporting done

Before reporting success, verify on disk:
- [ ] `${OUT_DIR}/Abhishek_Tuteja_Cover_Letter.docx` exists and is non-empty
- [ ] `${OUT_DIR}/Abhishek_Tuteja_Cover_Letter.pdf` exists and is non-empty (warn but don't fail if osascript errored)
- [ ] One final lint pass (re-run Step 5's Python against the file at `/tmp/cover_letter_${JOB_ID}_draft.json`) returns zero violations
- [ ] `pipeline/${JOB_ID}/state.yaml.cover_letters[]` contains `${LATEST_DIR}`

If any check fails, do NOT report success — diagnose and fix.

## Step 9 — report

Tell the user:
```
Cover letter written into: ${OUT_DIR}/
  - Abhishek_Tuteja_Cover_Letter.docx ({size} bytes)
  - Abhishek_Tuteja_Cover_Letter.pdf ({size} bytes)

state.yaml.cover_letters[] now references this dir.

Next:
  1. Open Abhishek_Tuteja_Cover_Letter.docx -- confirm no fabricated claims and no
     ACM-as-O2C-style drift against profile/bullets.md.
  2. Submit manually alongside Abhishek_Tuteja_Resume.docx.
```
