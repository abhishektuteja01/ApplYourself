---
description: Generate a one-page ATS-clean, vertical-aware tailored resume for a job_id — vertical defaults from profile/verticals/<vertical>/tailoring.md, canonical bullets only, rephrase within allowable_synonyms, Skills from skills_master.md. Writes docx/pdf + audit artifacts to applications/<vertical>/<dir>/ and appends the dir to state.yaml.tailored_dirs[].
model: sonnet
effort: medium
allowed-tools:
  - Bash
  - Read
  - Write
argument-hint: <job_id>
---

# /tailor — generate a tailored, audited resume

Tailor the real-bullets-only resume for one role. `$1` = the 8-hex `job_id`.

## Invariants (govern every step; stated once here)

- **VERT-DEFAULT** — the vertical's `tailoring.md` sets the default for bullet
  budget, project/section ordering, summary framing, and skills layout. JD text
  fine-tunes *within* that default; it never adds or removes a section or line.
- **NO-FAB**, **NO-DRIFT**, **REPHRASE-LICENSE**, **SKILLS-SOURCE** — defined in
  `.claude/shared/no_fab.md`. **Read that file now**; the rest of this command
  cites all four by name and Step 5's hard-refuse table depends on them.

---

## Step 1 — prerequisites + row load + output dir (one block, fail loud)

`uv run tailor-prep <job_id>` (the bash block below) runs every prereq check,
merges the clean+scored row to `/tmp/tailor_<job_id>_row.json`, resolves the
vertical, and creates the versioned output dir — exiting nonzero at the FIRST bad
check. If it exits nonzero, **stop — no partial work.**

- `vertical` comes from the row, never re-derived from JD text.
- Versioning is `max(existing _vN) + 1` across the role's lifetime; the leading
  date is always TODAY's. Numbers are never reused.
- `track_cli ensure` registers the role if it has no `state.yaml` and no-ops if
  it does. It is **not** a transition, so a `_v2` on an `applied` or terminal
  role is allowed. Transitions stay `/track`'s alone (R10).

```bash
JOB_ID="$1"
test -n "$JOB_ID" || { echo "ERROR: /tailor requires a job_id argument."; exit 1; }
# Validate the verticals config + per-vertical prose/resume files (owns its own
# actionable message); prep then consumes the loaded config.
uv run verticals-check || { echo "ERROR: verticals config invalid or per-vertical prose files missing — see message above."; exit 1; }
uv run python -m src.track_cli ensure "$JOB_ID" || exit 1
# prep prints VERTICAL / DIRNAME / OUT_DIR / DICTION_PASS / ROW_JSON /
# APPLICANT_NAME / FILE_SLUG on stdout
# (the full row JSON + status go to stderr); eval brings them into the shell.
PREP="$(uv run tailor-prep "$JOB_ID")" || exit 1
eval "$PREP"
test -n "$OUT_DIR" || { echo "ERROR: tailor-prep produced no OUT_DIR."; exit 1; }
test -n "$FILE_SLUG" || { echo "ERROR: tailor-prep produced no FILE_SLUG -- the vertical's resume_file needs a bold name line."; exit 1; }
```

After this block `$VERTICAL`, `$DIRNAME`, `$OUT_DIR`, `$DICTION_PASS`,
`$ROW_JSON` (= `/tmp/tailor_${JOB_ID}_row.json`), `$APPLICANT_NAME` and
`$FILE_SLUG` are set for every later step. The last two come from the first bold
line of the vertical's `resume_file` — no name is hardcoded here.

## Step 2 — read the JD row + profile

Read each file below in full. Skip one already read in full this session with no
change signal; when in doubt, re-read. Step 1 already printed the row JSON, so
re-reading that file is optional.

- `/tmp/tailor_${JOB_ID}_row.json` — the JD + score row
- the vertical's résumé — read the block's `resume_file` in
  `profile/verticals.yaml` **verbatim; never construct the path** (the filename
  does not track the vertical name). Education/contact/dates come verbatim from
  here. Its Skills block is baseline-only — do NOT use it (SKILLS-SOURCE).
- `profile/bullets.md` — canonical bullets + per-bullet `allowable_synonyms`
- `profile/skills_master.md` — master skills inventory + per-skill
  `allowable_synonyms`
- `profile/verticals/${VERTICAL}/tailoring.md` — this vertical's defaults (see
  VERT-DEFAULT)

## Step 3 — plan the resume

**3a — anchor to JD keywords first.** Print `keywords_to_mirror` from
`scored.parquet` for this job_id and build a keyword→bullet map: which bullet
surfaces each keyword, and whether a rephrase is needed. Drive every selection
below from it — zero keywords is a drop candidate, several is a priority keep.

**3b — decide each bullet.** For each bullet in `bullets.md`, choose:
- **`unchanged`** — include verbatim canonical text
- **`rephrase`** — restate under REPHRASE-LICENSE, mirroring JD vocabulary only
  where a synonym genuinely re-packages the same claim
- **`drop`** — not relevant to this JD; omit

**3c — budget & projects (VERT-DEFAULT).** The vertical's `tailoring.md` sets the
bullet mix and section order; JD content fills the floor, never lowers it.
- **Precedence, in order:** the vertical's `tailoring.md` total-bullet floor
  wins. If it states no floor, or states one below 10, use 10. There is never a
  case where both numbers bind at once. Below the governing floor, expand
  1-bullet projects to 2 before proceeding. Where the vertical also states a
  ceiling, do not exceed it.
- Keep EVERY project named in the vertical's `tailoring.md`
  "Project ordering" visible; pick 1–3 bullets each to hit budget. Drop a whole
  project ONLY when its `tailoring.md` says so **in the "Project ordering" line
  itself** — if that line does not name dropping as an option, dropping is not
  sanctioned. Default to "include with 1 bullet"; never infer permission.

**3d — summary (VERT-DEFAULT, NO-FAB).** Freely written per JD but bound by truth
from the vertical's résumé / `bullets.md` — no new facts. Frame per `tailoring.md`.

**3e — Skills section (SKILLS-SOURCE, VERT-DEFAULT).** The vertical's
`tailoring.md` "Skills layout" is an ordered list of category lines; each line
names the `skills_master.md` entries (by `SKILL-<ID>`) eligible to appear on it.
Render exactly those lines, in that order — line count and headers are the
vertical's. A skill absent from every line does not appear; the same skill may sit
under different headers in different verticals. Per line:
1. Take the line's eligible `SKILL-<ID>` set from the layout.
2. Rank: (a) `vertical_lean` includes `${VERTICAL}` AND matches a
   `keywords_to_mirror` entry; (b) remaining `vertical_lean` includes
   `${VERTICAL}`; (c) others last — enough that the line isn't near-empty.
3. Render each entry's `name` (or an `allowable_synonyms` alias if it better
   mirrors a JD keyword — relabeling only, same as bullets), comma-separated, in
   that order.

JD content may fine-tune order WITHIN a line only.

**Tailoring scope:**
- **Editable:** Summary; employer bullets; project bullets (order + selection +
  rephrase); Skills section (select + order per 3e)
- **Frozen:** Education, contact, all dates

**3f — commit the plan before drafting.** Write the full plan as a table — one row
per bullet, plus one for the Summary:

```
| source (B-ID/summary) | decision (unchanged/rephrase/drop) | keywords covered | synonyms to use |
```

`synonyms to use` = the exact `allowable_synonyms` entries the rephrase draws on
(empty for unchanged/drop). Step 4 executes this table; Step 7's trace.md is
written against it. If landed text diverges from the plan, say so in trace.md —
never silently absorb the difference.

## Step 4 — draft the resume markdown

Write the draft to `/tmp/tailor_${JOB_ID}_draft_resume.md` now — before lint. Use
this shape exactly so the docx renderer's parser handles it:

Every frozen line — the name, the contact line, each role/degree header with its
employer, location and dates — is **copied verbatim from the vertical's
`resume_file`**. Never retype or reformat one from memory. You generate only the
SUMMARY, the bullet selection, the project titles/order, and the SKILLS lines.

```markdown
**<name line, verbatim from resume_file>**

<contact line, verbatim from resume_file>

**SUMMARY**

<one-line summary tailored to JD, bound by truth from the vertical's résumé.
No tricolon ("X, Y, and Z") and no "not only X but also Y" — both are AI tells.>

**WORK EXPERIENCE**

**<Role>** - <Employer>	<Location> | <Start – End>        <- verbatim from resume_file

* <chosen canonical bullet or rephrase, by its bullets.md ID>
* <chosen canonical bullet or rephrase>
* ...

**<Role>** - <Employer>	<Location> | <Start – End>        <- verbatim from resume_file

* <chosen canonical bullet or rephrase>

**PROJECTS**

**<Project Title>**	<Date – Date>

* <chosen project bullet canonical or rephrase, by its bullets.md ID>
* ...

**EDUCATION**

**<Degree>**	<Start – End>                                 <- verbatim from resume_file

<Institution, Location> | GPA: <x.xx> | Coursework: ...       <- verbatim from resume_file

**TECHNICAL SKILLS**

<one line per category from the vertical's "Skills layout" in tailoring.md, e.g.>
**<Category>:** ...
```

**Renderer parser contract (hard rules):**

- Header lines use a literal **TAB (`\t`)** between LEFT and RIGHT content — the
  only signal the renderer recognises for right-alignment. Never substitute `|`
  or spaces.

  | Entry type | LEFT | RIGHT (after TAB) |
  |---|---|---|
  | Work | `**Role**` ` - ` `Company` | `Location \| Date – Date` |
  | Project | `**Project Name**` | `Date – Date` (no location) |
  | Education | `**Degree**` | `Date – Date` (school/GPA/coursework on a separate Resume Body line below) |

- **Section order:** per VERT-DEFAULT / Step 3c. The template shows one ordering;
  if the vertical defaults PROJECTS before WORK EXPERIENCE, swap those two sections
  (same internal rules). SUMMARY always first; EDUCATION/TECHNICAL SKILLS last.
- **Contact line** (right after the name block): no markup — the renderer
  centers the first body block after the name.
- **Skills lines** (`**Programming:** ...`): the trailing `:` on the bold prefix
  is the signal. No tab.
- **Frozen sections (education, contact):** VERBATIM from the vertical's résumé.
  Skills is NOT copied from the résumé (SKILLS-SOURCE) — build per Step 3e.

## Step 5 — lint loop (the enforcement chain)

Run mechanical fix + phrase scan, then loop per `.claude/shared/lint_loop.md`.
The Python reads the Step 4 draft and re-saves the mechanical-fixed version over
it.

```bash
uv run python <<PYEOF
import json
from pathlib import Path
from src.lint import (
    fix_mechanical, find_phrase_violations,
    load_de_ai_rules, bullets_diction_pass_completed,
    compute_exempt_lines, parse_bullets_md,
)

resume_md = Path('/tmp/tailor_${JOB_ID}_draft_resume.md').read_text(encoding='utf-8')
rules = load_de_ai_rules()
bullets = parse_bullets_md(Path('profile/bullets.md').read_text(encoding='utf-8'))
diction_done = bullets_diction_pass_completed(rules)

# Tier 1: mechanical (always applied; no exemption)
fixed_md, subs = fix_mechanical(resume_md, rules)
Path('/tmp/tailor_${JOB_ID}_draft_resume.md').write_text(fixed_md, encoding='utf-8')

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

**Branch on each violation:**

| Violation location | Action | File to name in refuse message |
|---|---|---|
| Canonical line (matches `bullets[id].canonical` verbatim after stripping markers) | **Hard-refuse** | the `bullet_id` |
| Frozen section (education/contact) | **Hard-refuse** | the vertical's résumé (`resume_file`) |
| Skills line (flagged text is a `skills_master.md` `name`/`allowable_synonyms`) | **Hard-refuse** | `profile/skills_master.md` + the offending `SKILL-<ID>` |
| Rephrased line | **Rewrite** under REPHRASE-LICENSE; re-run the lint Bash. Loop ≤5 attempts; if still failing, revert to `unchanged`. If the canonical text ITSELF then trips the same phrase, that is the hard-refuse row above, not another rewrite — use `drop` only when dropping still meets the bullet floor | — |

On any hard-refuse: delete `${OUT_DIR}` (`rmdir "${OUT_DIR}"`), surface the
location + phrase + category, and print:

```
Cannot tailor: <location> contains banned phrase "<phrase>" (category: <category>).

Re-run the diction pass, fix <file> until zero violations, confirm
bullets_diction_pass_completed: true in profile/de_ai_rules.yaml,
then re-run /tailor.

No applications/<vertical>/<dir>/ artifacts were written.
```

**Drift self-check (after lint passes, before render).** With `violations` empty,
re-read each rephrased line word by word: does any content word appear in NEITHER
the canonical text NOR its `allowable_synonyms`? If yes, it violates
REPHRASE-LICENSE regardless of how natural it reads — revert to `unchanged` (or
re-rephrase within license) and re-run the lint Bash. Proceed only when every
rephrase passes.

## Step 6 — render the docx

```bash
uv run python -c "
from pathlib import Path
from src.docx_render import render_resume
md = Path('/tmp/tailor_${JOB_ID}_draft_resume.md').read_text(encoding='utf-8')
render_resume(md, Path('profile/resume_template.docx'), Path('${OUT_DIR}/${FILE_SLUG}_Resume.docx'))
print('rendered:', '${OUT_DIR}/${FILE_SLUG}_Resume.docx')
"
```

On `TemplateMissingError`/`TemplateError`: surface the message verbatim and stop.

Then convert to PDF — set `BASENAME=Resume` (`OUT_DIR` and `FILE_SLUG` are
already set) and run `.claude/shared/render_pdf.md` verbatim. Read that file;
do not reconstruct the AppleScript from memory.

## Step 7 — write the audit artifacts

**`${OUT_DIR}/resume.md`** — copy the final lint-clean draft:

```bash
cp /tmp/tailor_${JOB_ID}_draft_resume.md "${OUT_DIR}/resume.md"
```

**`${OUT_DIR}/trace.md`** — per-line audit; for every NON-FROZEN line:

```
L<N> source=<B-ID|summary|frozen> transformation=<unchanged|reweight|rephrase>
```

For `transformation=rephrase`, append:
```
  before:   <canonical text from bullets.md>
  after:    <text that landed in resume.md>
  synonyms: ["<exact allowable_synonyms entries that license this rewrite>", ...]
```

A rephrase you cannot cite `synonyms:` for is illegal (REPHRASE-LICENSE) — catch
it here. Frozen lines (education/contact) → `transformation=frozen`. Skills lines
→ `transformation=reweight`, `source` = the `SKILL-<ID>` entry, not a `B-` id.

Header:
```markdown
# trace.md — per-line audit for ${OUT_DIR}

Tailored for job_id `${JOB_ID}` on $(date +%Y-%m-%d).
Every rephrase records before→after so NO-DRIFT is eyeball-catchable.
```

**`${OUT_DIR}/keywords_to_mirror.md`** — the 2–3 keywords from
`scored.parquet.keywords_to_mirror` and where each landed (which resume line, or
"not landed — kept verbatim canonical to preserve attestation").

**`${OUT_DIR}/jd_snapshot.md`** — the full JD body, written from the row.json
already on disk. Never retyped:

```bash
uv run tailor-prep snapshot "$JOB_ID" "$OUT_DIR"
```

**`${OUT_DIR}/lint_report.md`** — sections:

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

The only side-list mutation `/tailor` is allowed. State transitions remain
`/track`'s sole job (R10); `/tailor` only adds to the artifact-reference list.

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

Verify on disk; if any check fails, do NOT report success — diagnose and fix.

- [ ] `${OUT_DIR}/${FILE_SLUG}_Resume.docx` exists and is non-empty
- [ ] `${OUT_DIR}/${FILE_SLUG}_Resume.pdf` exists and is non-empty (warn but don't fail if osascript errored)
- [ ] `${OUT_DIR}/resume.md` exists
- [ ] `${OUT_DIR}/trace.md` exists with one entry per non-frozen line
- [ ] `${OUT_DIR}/jd_snapshot.md` contains the full JD body
- [ ] `${OUT_DIR}/lint_report.md` exists; "Phrase flags resolved" shows zero unresolved flags
- [ ] One final lint pass returns zero violations. Step 5's Python reads and
      writes `/tmp/tailor_${JOB_ID}_draft_resume.md`, so re-running it verbatim
      re-checks the draft, NOT the copy in `${OUT_DIR}`. Either run it before the
      `cp`, or change both paths to `${OUT_DIR}/resume.md` when re-running.
- [ ] `pipeline/${JOB_ID}/state.yaml.tailored_dirs[]` contains `${DIRNAME}`
- [ ] Every rendered Skills item traces to a `skills_master.md` `name`/`allowable_synonyms` (spot-check SKILLS-SOURCE)

## Step 10 — report and remind

```
Tailored: ${OUT_DIR}/
  - ${FILE_SLUG}_Resume.docx ({size} bytes)
  - ${FILE_SLUG}_Resume.pdf ({size} bytes)
  - trace.md ({N} entries, {M} rephrases — eyeball-scan for NO-DRIFT breaches)
  - lint_report.md ({K} mechanical fixes, 0 unresolved phrase flags)
  - keywords_to_mirror.md, jd_snapshot.md

state.yaml.tailored_dirs[] now references this dir.

Next:
  1. Open resume.docx and trace.md — confirm no analogy-as-equivalence drift.
  2. Submit manually on the company's site.
  3. Run /track ${JOB_ID} applied  (records the transition + sets applied_at).
  4. Verify the employer is E-Verify-enrolled (manual v1 step).
```
