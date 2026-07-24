---
description: Generate a one-page ATS-clean, vertical-aware tailored resume for a job_id — vertical defaults from profile/verticals/<vertical>/tailoring.md, canonical bullets only, rephrase within allowable_synonyms, Skills from skills_master.md. Writes docx/pdf + audit artifacts to applications/<vertical>/<dir>/ and appends the dir to state.yaml.tailored_dirs[].
model: sonnet
effort: medium
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
argument-hint: <job_id>
---

# /tailor — generate a tailored, audited resume

You are tailoring my real-bullets-only resume for a specific role. **The whole
slice exists to enforce R2 (no fabrication) and R5 (de-AI'd writing).**

Argument: `$1` is the 8-hex `job_id` to tailor for.

---

## Step 1 — prerequisites + row load + output dir (one block, fail loud)

One Bash block runs all the deterministic plumbing — prereq checks, row
load, diction gate, and output-dir computation. It exits at the FIRST
failed check with the same actionable messages as ever. **No partial
work:** if it exits nonzero, stop.

On dir naming: `vertical` is read from the row (precomputed at discovery
time) — never re-derived from JD text here. It is stable for a
given `job_id`, so prior-dir counting is scoped to that
vertical's subfolder. Versioning is count-based across the role's
lifetime, not date-scoped: the first re-tailor becomes `_v2` even on a
different day; the leading date is always TODAY's, so the dirname still
says when this attempt was made.

```bash
JOB_ID="$1"
test -n "$JOB_ID" || { echo "ERROR: /tailor requires a job_id argument."; exit 1; }
test -f jobs/clean.parquet || { echo "ERROR: jobs/clean.parquet missing — run discovery first."; exit 1; }
test -f jobs/scored.parquet || { echo "ERROR: jobs/scored.parquet missing — run /score first."; exit 1; }
test -f profile/bullets.md || { echo "ERROR: profile/bullets.md missing — author it."; exit 1; }
test -f profile/de_ai_rules.yaml || { echo "ERROR: profile/de_ai_rules.yaml missing — author it."; exit 1; }
test -f profile/skills_master.md || { echo "ERROR: profile/skills_master.md missing — author it."; exit 1; }
test -f profile/preferences.md || { echo "ERROR: profile/preferences.md missing. Author it (locations, comp floor, deal-breakers, must-haves)."; exit 1; }
test -f profile/resume_template.docx || { echo "ERROR: profile/resume_template.docx missing. Author it in Word with the ATS constraints (Calibri, single column, no tables, bold section headers, no images/icons)."; exit 1; }
uv run python -m src.verticals || { echo "ERROR: verticals config invalid or per-vertical prose files missing — see message above."; exit 1; }
test -f "pipeline/${JOB_ID}/state.yaml" || { echo "ERROR: pipeline/${JOB_ID}/state.yaml missing. Run /track ${JOB_ID} saved first to register the role (the state.yaml is the canonical role record)."; exit 1; }

mkdir -p applications
uv run python -c "
import json, pandas as pd
from pathlib import Path
JOB_ID = '$1'
clean = pd.read_parquet('jobs/clean.parquet').set_index('job_id')
scored = pd.read_parquet('jobs/scored.parquet').set_index('job_id')
if JOB_ID not in clean.index:
    raise SystemExit(f'ERROR: job_id {JOB_ID} not in clean.parquet')
if JOB_ID not in scored.index:
    raise SystemExit(f'ERROR: job_id {JOB_ID} not in scored.parquet -- run /score first')
row = {**clean.loc[JOB_ID].to_dict(), **scored.loc[JOB_ID].to_dict()}
print(json.dumps({k: (v.isoformat() if hasattr(v, 'isoformat') else v) for k, v in row.items()}, default=str, indent=2))
" > /tmp/tailor_${JOB_ID}_row.json || exit 1

# Diction-pass gate (one line; the linter reads the full file itself)
grep -q "bullets_diction_pass_completed: true" profile/de_ai_rules.yaml \
  && echo "diction_pass: true" || echo "diction_pass: false"

TODAY=$(date +%Y-%m-%d)
read -r VERTICAL COMPANY_SLUG TITLE_SLUG <<< "$(uv run python -c "
import json, re
row = json.load(open('/tmp/tailor_${JOB_ID}_row.json'))
v = row.get('vertical') or ''
slug = lambda s: re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')
from src.verticals import get_config
cfg = get_config()
print(v if v in cfg.verticals else cfg.default_vertical, slug(row['company']), slug(row['title'])[:60])
")"
test -n "$VERTICAL" || { echo "ERROR: vertical resolution failed — see message above."; exit 1; }
mkdir -p "applications/${VERTICAL}"
# Count prior tailor dirs for this job_id within its vertical folder
PRIOR_COUNT=$(ls -d applications/${VERTICAL}/*_${JOB_ID}* 2>/dev/null | wc -l | tr -d ' ')
if [ "$PRIOR_COUNT" = "0" ]; then
    DIRNAME="${VERTICAL}/${TODAY}_${COMPANY_SLUG}_${TITLE_SLUG}_${JOB_ID}"
else
    NEXT_V=$((PRIOR_COUNT + 1))
    DIRNAME="${VERTICAL}/${TODAY}_${COMPANY_SLUG}_${TITLE_SLUG}_${JOB_ID}_v${NEXT_V}"
fi
OUT_DIR="applications/${DIRNAME}"
mkdir -p "${OUT_DIR}"
echo "tailoring to: ${OUT_DIR}  (vertical=${VERTICAL})"
cat /tmp/tailor_${JOB_ID}_row.json
```

## Step 2 — read the JD row + profile

`Read` these files in full — EXCEPT: if a listed profile file was already
read in full earlier THIS session and you have no signal it changed (no
edit this session, no system reminder saying it was modified), do not
re-read it — the in-context copy is current, and re-reading it is the main
cost of back-to-back tailors. The row json is always new; always read it.
When in doubt whether a file changed, re-read — correctness beats the
token saving.

- `/tmp/tailor_${JOB_ID}_row.json` — the JD + score row (also printed by
  Step 1; reading the file is unnecessary if the full JSON is already in
  your context from Step 1's output)
- the vertical's résumé — its `resume_file` in `profile/verticals.yaml` (`profile/verticals/${VERTICAL}/resume_<vertical>.md`) — your attested resume for this lane (Education/contact/dates come verbatim from here; the Skills block here is baseline-only — do NOT use it for this tailoring run)
- `profile/bullets.md` — canonical bullets + `allowable_synonyms` per bullet
- `profile/skills_master.md` — master skills inventory + `allowable_synonyms` per skill; this, not the résumé's Skills block, is what the Skills section is built from
- `profile/verticals/${VERTICAL}/tailoring.md` (`${VERTICAL}` was resolved in Step 1) — this vertical's bullet budget, project ordering, summary framing, skills category order, and section order defaults

## Step 3 — plan the resume

**3a — anchor to JD keywords first.** Before selecting bullets, print the `keywords_to_mirror` list from `scored.parquet` for this job_id. For each keyword, identify which bullet will surface it and whether a rephrase is needed. Build a keyword→bullet map and use it to drive every selection decision below. A bullet covering zero keywords from the map is a drop candidate; one covering multiple is a higher-priority keep.

For each bullet in `bullets.md`, decide one of:
- **`unchanged`** — include verbatim canonical text
- **`rephrase`** — restate using ONLY words already in canonical + this bullet's `allowable_synonyms` list. Mirror JD vocabulary via synonyms where the synonym genuinely re-packages the same claim. **NEVER invent JD vocabulary.** Forbidden: tools/metrics/scopes/dates absent from canonical, fabricated module-config claims (R3), analogy-as-equivalence relabels (R2b: ACM commodity-contract settlement is NOT generic SD O2C).
- **`drop`** — bullet not relevant to this JD; omit

**Page budget — vertical-aware hard floor:** `profile/verticals/${VERTICAL}/tailoring.md` (read in Step 2, or earlier this session) sets the default bullet mix; JD content does not override the floor, only which bullets fill it.

If your plan has fewer than 10 total, expand 1-bullet projects to 2 bullets before moving on. Fewer than 10 bullets is a hard failure — a half-empty page is worse than a slightly-stretched project bullet.

**Project ordering policy:** Keep ALL three projects visible (PROVA, CapTrack, Options Pricing) for every vertical. The vertical's `tailoring.md` sets the default SECTION order; the JD's specific wording can still re-rank within that default — vertical sets the starting point, JD text fine-tunes, never the reverse. Then pick 1-3 bullets per project to hit the bullet budget above.

Drop an entire project ONLY when it's genuinely off-domain (e.g. ML options pricing on a pure SAP-config role). Default to "include with 1 bullet" over "drop entirely".

**Summary framing (vertical-aware):** the Summary line is freely written per JD (not frozen) but must stay bound by truth from the vertical's résumé (`resume_file`)/`bullets.md` — no new facts, same R2 discipline as bullets. Frame it per the vertical's `tailoring.md` (what leads, what supports).

**Skills section: select + order from `profile/skills_master.md`, never from the résumé's Skills block.** The Skills section layout is **per-vertical**: the vertical's `tailoring.md` "Skills layout" defines an ordered list of category lines, and for each line the set of `skills_master.md` entries (by `SKILL-<ID>`) eligible to appear on it. Render exactly those lines, in that order — the line count and headers are the vertical's, not a fixed number. A skill may sit under different headers in different verticals, and a skill not listed on any line of this vertical's layout does not appear. For each category line:
1. Take the line's eligible `SKILL-<ID>` set from the vertical's `tailoring.md` layout.
2. Rank within the line: (a) entries whose `vertical_lean` includes `${VERTICAL}` AND match a `keywords_to_mirror` entry first; (b) remaining entries whose `vertical_lean` includes `${VERTICAL}`; (c) any others last — include enough that the line isn't near-empty.
3. Render selected entries' `name` (or an `allowable_synonyms` alias if it better mirrors a JD keyword — same relabeling-only rule as bullets) in that priority order, comma-separated.
4. The lines and their order come from the vertical's `tailoring.md`. JD content can fine-tune ordering WITHIN a line, but never adds a line, drops a line, or moves a skill onto a line the layout didn't assign it to.

Every item that lands in the Skills section must trace to a `skills_master.md` entry AND be listed on that line in the vertical's `tailoring.md` layout — never invent one, never silently drop the master-file requirement and copy from the résumé's Skills block instead.

**Section order (vertical-aware):** the vertical's `tailoring.md` sets the default order of the WORK EXPERIENCE and PROJECTS sections (SUMMARY always first, EDUCATION/TECHNICAL SKILLS always last). JD content can still override within that default if it strongly justifies it (same "vertical sets default, JD fine-tunes" pattern as bullet/project ordering above) — but absent a strong JD-specific reason, stick to the vertical default.

Tailoring scope (option (b)):
- **Editable:** Summary, Deloitte bullets, project bullets (order + selection + rephrase-within-synonyms), Skills section (select + order from `skills_master.md` per the rule above)
- **Frozen:** Education, contact, all dates

**Commit the plan before drafting.** Close Step 3 by writing the full plan out
as a table — one row per bullet (plus one for the Summary):

```
| source (B-ID/summary) | decision (unchanged/rephrase/drop) | keywords covered | synonyms to use |
```

`synonyms to use` lists the exact `allowable_synonyms` entries the rephrase
will draw on (empty for unchanged/drop). Step 4 executes this table; Step 7's
trace.md is written against it. If the landed text diverges from the plan,
say so explicitly in trace.md — never silently absorb the difference.

## Step 4 — draft the resume markdown

Write the draft directly to `/tmp/tailor_${JOB_ID}_draft_resume.md` now — before lint.
Use this shape exactly so the docx renderer's small parser handles it:

```markdown
**Abhishek Tuteja**

San Francisco, CA • +555-0100 • user@example.com • [Portfolio](https://www.abhishektuteja.com/) • [LinkedIn](http://linkedin.com/in/abhishektuteja) • [GitHub](http://github.com/abhishektuteja01)

**SUMMARY**

<one-line summary tailored to JD; bound by truth from the vertical's résumé>

**WORK EXPERIENCE**

**Advisory Analyst** - Deloitte	Bengaluru, IN | May 2022 – Jul 2024

* <chosen B-DEL-XX canonical or rephrase>
* <chosen B-DEL-YY canonical or rephrase>
* ...

**Teaching Assistant** - Northeastern University	Oakland, CA | Jan 2025 – Apr 2026

* <B-TA-01 canonical>

**PROJECTS**

**<Project Title>**	<Date – Date>

* <chosen B-PROVA/CAPTRACK/OPT-XX canonical or rephrase>
* ...

**EDUCATION**

**Master of Science in Computer Science**	Sep 2024 – May 2026

Northeastern University, Oakland, CA | GPA: X.XX | Coursework: ...

**Bachelor of Technology in Mechanical Engineering (Minor: Data Science)**	Jul 2018 – Jul 2022

Manipal Institute of Technology, Manipal, KA

**TECHNICAL SKILLS**

**Programming:** ...
**AI & Machine Learning:** ...
**SAP & Enterprise:** ...
**Databases & Tools:** ...
```

CRITICAL formatting rules for the renderer's parser:

- **Job/project/education header lines use a literal TAB (`\t`)** as the
  delimiter between LEFT content and RIGHT-aligned content. The renderer
  emits the tab to the docx; the template's `Resume Job Header` style has
  a right tab stop at 19.05 cm so everything after the tab right-aligns
  at the right margin.
  - **Work entries:** LEFT = `**Role**` + ` - ` + `Company`; RIGHT =
    `Location | Date – Date` (location grouped with date, pipe-separated).
  - **Project entries:** LEFT = `**Project Name**`; RIGHT = `Date – Date`
    (no location).
  - **Education entries:** LEFT = `**Degree**`; RIGHT = `Date – Date`
    (school / GPA / coursework go on a separate Resume Body line below).
  - Do not substitute `|` or spaces for the tab; the tab is the only
    structural signal the renderer recognises.
- **Section order:** per Step 3's section-order rule (the vertical's
  `tailoring.md` sets it). The template above shows one ordering; if the
  vertical's default puts PROJECTS before WORK EXPERIENCE, swap those two
  sections — same internal formatting rules apply.
- **Contact line** (immediately after the name block) is rendered centered
  automatically — no markup needed, the renderer detects "first body block
  after name" and forces center alignment on that one paragraph.
- **Skills lines** (`**Programming:** ...`, `**AI & Machine Learning:** ...`,
  etc.) are detected by the trailing `:` on the bold prefix and route to
  `Resume Body` style with inline bold on the prefix. Do NOT use tab here;
  the colon is the structural signal.

Frozen sections (education, contact) come VERBATIM from the vertical's résumé (`resume_file`). Do not retype them creatively. The Skills section is NOT copied from the résumé — build it from `profile/skills_master.md` per Step 3's selection rule.

## Step 5 — lint loop (the enforcement chain)

Run mechanical fix and phrase scan. **`/tailor` MUST loop until phrase
violations are zero.** Per R5: "never silently let a banned phrase ship."

```bash
uv run python <<PYEOF
import json
from pathlib import Path
from src.lint import (
    fix_mechanical, find_phrase_violations,
    load_de_ai_rules, bullets_diction_pass_completed,
    compute_exempt_lines, parse_bullets_md,
)

resume_md = Path('/tmp/tailor_${JOB_ID}_draft_resume.md').read_text()
rules = load_de_ai_rules()
bullets = parse_bullets_md(Path('profile/bullets.md').read_text())
diction_done = bullets_diction_pass_completed(rules)

# Tier 1: mechanical (always applied; no exemption)
fixed_md, subs = fix_mechanical(resume_md, rules)
Path('/tmp/tailor_${JOB_ID}_draft_resume.md').write_text(fixed_md)

# Tier 2: phrase scan with conditional exemption
exempt = compute_exempt_lines(fixed_md, bullets, diction_done)
violations = find_phrase_violations(fixed_md, context='resume', exempt_lines=exempt, rules=rules)

out = {
    'mechanical_subs': len(subs),
    'mechanical_sample': subs[:5],
    'exempt_line_count': len(exempt),
    'diction_pass_done': diction_done,
    'violations': violations,
}
print(json.dumps(out, indent=2, default=str))
PYEOF
```

The Python above reads the draft you wrote in Step 4 and re-saves the mechanical-fixed version over it.

### Branch on what the violations look like

1. **Violation on a canonical line** (line text matches `bullets[id].canonical` verbatim after stripping bullet markers) — hard-refuse. Delete `${OUT_DIR}` (`rmdir "${OUT_DIR}"`), surface the bullet_id and phrase, and instruct the user:

   ```
   Cannot tailor: canonical bullet <bullet_id> contains banned phrase
   "<phrase>" (category: <category>).

   Re-run the diction pass, fix profile/bullets.md until zero violations,
   confirm bullets_diction_pass_completed: true in profile/de_ai_rules.yaml,
   then re-run /tailor.

   No applications/<vertical>/<dir>/ artifacts were written.
   ```

2. **Violation on a rephrased line** — rewrite using ONLY words in the source bullet's canonical text + its `allowable_synonyms`. Re-run the lint Bash above. Loop up to 5 attempts. If still failing after 5 attempts, revert that bullet to `unchanged` (safest) or `drop` it.

3. **Violation on a frozen section (education/contact)** — hard-refuse with the same message as case (1), naming the vertical's résumé (`resume_file`) as the file to fix instead of a bullet_id.

4. **Violation on a Skills section line** — the flagged text is a `skills_master.md` entry's `name` or `allowable_synonyms` value, not free prose. Hard-refuse with the same message as case (1), naming `profile/skills_master.md` and the offending entry's `SKILL-<ID>` as the file/entry to fix.

### Drift self-check (after lint passes, before render)

When `violations` is empty, run one adversarial pass over your own rephrases
before rendering: re-read each rephrased line word by word and ask — does any
content word appear in NEITHER that bullet's canonical text NOR its
`allowable_synonyms`? If yes, the rephrase is illegal regardless of how
natural it reads: revert that bullet to `unchanged` (or re-rephrase within
the licensed vocabulary) and re-run the lint Bash above. Only proceed to
Step 6 when every rephrase passes this check.

## Step 6 — render the docx

```bash
uv run python -c "
from pathlib import Path
from src.docx_render import render_resume
md = Path('/tmp/tailor_${JOB_ID}_draft_resume.md').read_text()
render_resume(md, Path('profile/resume_template.docx'), Path('${OUT_DIR}/Abhishek_Tuteja_Resume.docx'))
print('rendered:', '${OUT_DIR}/Abhishek_Tuteja_Resume.docx')
"
```

If render raises `TemplateMissingError` or `TemplateError`, the message is already actionable — surface it to the user verbatim and stop. (You should have caught this in Step 1's prereqs, but `_validate_template` enforces structural constraints (missing styles, tables, inline shapes) that Step 1 can't.)

Then convert the docx to PDF using Microsoft Word via AppleScript:

```bash
DOCX_ABS=$(cd "${OUT_DIR}" && pwd)/Abhishek_Tuteja_Resume.docx
PDF_ABS=$(cd "${OUT_DIR}" && pwd)/Abhishek_Tuteja_Resume.pdf
osascript <<ASEOF
tell application "Microsoft Word"
    open POSIX file "${DOCX_ABS}"
    set theDoc to active document
    save as theDoc file format format PDF file name "${PDF_ABS}"
    close active document saving no
end tell
ASEOF
echo "pdf rendered: ${OUT_DIR}/Abhishek_Tuteja_Resume.pdf"
```

If the `osascript` step fails (non-zero exit), surface the error and continue — the docx is the primary artifact; the PDF is supplementary.

## Step 7 — write the audit artifacts

`${OUT_DIR}/resume.md` — write the final lint-clean resume markdown (already on disk at `/tmp/tailor_${JOB_ID}_draft_resume.md`; just `cp`).

```bash
cp /tmp/tailor_${JOB_ID}_draft_resume.md "${OUT_DIR}/resume.md"
```

`${OUT_DIR}/trace.md` — per-line audit. For every NON-FROZEN line in resume.md, write:

```
L<N> source=<B-ID|summary|frozen> transformation=<unchanged|reweight|rephrase>
```

For `transformation=rephrase`, append:
```
  before:   <canonical text from bullets.md>
  after:    <text that landed in resume.md>
  synonyms: ["<exact allowable_synonyms entries that license this rewrite>", ...]
```

The `synonyms:` line is the authorization cite: every content word in `after`
that isn't in `before` must trace to one of the listed entries. A rephrase you
cannot cite synonyms for is illegal — catch it here, not in the user's review.

The `before:` / `after:` pair is the DRIFT CATCHER. The user will eyeball this
to catch ACM→O2C-style slippage (R2b). Frozen lines (education, contact) can
be marked `transformation=frozen`. Skills lines are `transformation=reweight`
(selected from `profile/skills_master.md`, not frozen) — `source` for these is
the `SKILL-<ID>` entry, not a `B-` bullet ID.

Header for trace.md:
```markdown
# trace.md — per-line audit for ${OUT_DIR}

Tailored for job_id `${JOB_ID}` on $(date +%Y-%m-%d).
Rule: every rephrase records before→after so analogy-as-equivalence drift
(ACM→O2C, MM/SD over-claims) is eyeball-catchable.
```

`${OUT_DIR}/keywords_to_mirror.md` — the 2-3 keywords from `scored.parquet.keywords_to_mirror` and where each landed (which resume line, or "not landed — kept verbatim canonical to preserve attestation").

`${OUT_DIR}/jd_snapshot.md` — the FULL JD body from `clean.parquet.jd_text` (frozen point-in-time snapshot). Written deterministically from the parquet — never retype the JD yourself:

```bash
uv run python <<PYEOF
import pandas as pd
from pathlib import Path
row = pd.read_parquet('jobs/clean.parquet').set_index('job_id').loc['${JOB_ID}']
snap = f"""---
job_id: ${JOB_ID}
company: {row['company']}
title: {row['title']}
url: {row['url']}
posted_date: {row['posted_date']}
snapshot_at: $(date +%Y-%m-%d)
---

{row['jd_text']}
"""
Path('${OUT_DIR}/jd_snapshot.md').write_text(snap)
print('jd_snapshot.md written:', len(row['jd_text']), 'chars of JD body')
PYEOF
```

`${OUT_DIR}/lint_report.md` — sections:

```markdown
# Lint Report — ${DIRNAME}

## Mechanical substitutions ({N} applied)
| line | col | original | replacement | category |
|---|---|---|---|---|
| ... |
(or "none" if N=0)

## Phrase flags resolved ({M} encountered)
For each: line, category, original phrase, resolution (rewritten to → "..." | reverted to canonical | bullet dropped).
(or "none" if M=0)

## Synonym status
- diction_pass_completed: <true|false>
- bullets with allowable_synonyms populated: X / <total bullets in bullets.md>
- bullets used `rephrase` transformation: Y
- bullets used `unchanged` transformation: Z
```

## Step 8 — append the dir to state.yaml.tailored_dirs[]

This is the side-list mutation `/tailor` is allowed. State
transitions (`tailored`, `applied`, etc.) remain `/track`'s sole job
(R10) -- `/tailor` only adds to the artifact-reference list.

```bash
uv run python -c "
from pathlib import Path
from src.state_io import state_path_for, append_tailored_dir
p = state_path_for(Path('pipeline'), '$JOB_ID')
data = append_tailored_dir(p, '$DIRNAME')
print(f'tailored_dirs[] now has {len(data[\"tailored_dirs\"])} entry/entries')
"
```

## Step 9 — runtime assertions before reporting done

Before reporting success, verify on disk:
- [ ] `${OUT_DIR}/Abhishek_Tuteja_Resume.docx` exists and is non-empty
- [ ] `${OUT_DIR}/Abhishek_Tuteja_Resume.pdf` exists and is non-empty (warn but don't fail if osascript errored)
- [ ] `${OUT_DIR}/resume.md` exists
- [ ] `${OUT_DIR}/trace.md` exists with one entry per non-frozen line
- [ ] `${OUT_DIR}/jd_snapshot.md` contains the full JD body
- [ ] `${OUT_DIR}/lint_report.md` exists; the "Phrase flags resolved" section shows zero unresolved flags
- [ ] One final lint pass returns zero violations (re-run the Python from Step 5 against `${OUT_DIR}/resume.md`)
- [ ] `pipeline/${JOB_ID}/state.yaml.tailored_dirs[]` contains `${DIRNAME}`
- [ ] Every item in the rendered Skills section traces to a `profile/skills_master.md` entry's `name` or `allowable_synonyms` (spot-check — no invented skill, none silently copied from the résumé's Skills block instead)

If any check fails, do NOT report success — diagnose and fix.

## Step 10 — report and remind

Tell the user:
```
Tailored: ${OUT_DIR}/
  - Abhishek_Tuteja_Resume.docx ({size} bytes)
  - Abhishek_Tuteja_Resume.pdf ({size} bytes)
  - trace.md ({N} entries, {M} rephrases — eyeball-scan for ACM→O2C drift)
  - lint_report.md ({K} mechanical fixes, 0 unresolved phrase flags)
  - keywords_to_mirror.md, jd_snapshot.md

state.yaml.tailored_dirs[] now references this dir.

Next:
  1. Open resume.docx and trace.md — confirm no analogy-as-equivalence drift.
  2. Submit manually on the company's site.
  3. Run /track ${JOB_ID} applied  (records the transition + sets applied_at).
  4. Verify the employer is E-Verify-enrolled (manual v1 step).
```
