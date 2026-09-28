---
description: Write a one-page cover letter for a job_id into its built folder. The session writes cover_letter.yaml from the bullets on that resume; `uv run app build letter` checks and renders it into the user's template. Moves the role saved -> tailored.
model: opus
effort: medium
allowed-tools:
  - Bash
  - Read
  - Write
  - Skill
  - WebSearch
  - WebFetch
argument-hint: <job_id> [--to "Hiring Manager Name"]
---

# /cover-letter — one role, one letter

`$1` = the 8-hex `job_id`. With `--to "Name"` in `$ARGUMENTS`, the salutation is
`Dear Name,`; otherwise it is `Dear Hiring Manager,`. Read
`.claude/shared/no_fab.md` first.

## 1. Find the folder

```bash
cd "$(git rev-parse --show-toplevel)" && uv run app dir $1
```

An empty `dir` means no resume is built yet: stop and tell the user to run
`/tailor $1`.

## 2. Company answers

If `has_answers` is false, invoke the `company-answers` skill for `$1`. Either
way, read `<dir>/company_answers.yaml`. A `null` answer means no research: then
ground "why this role" in the JD's work, not in the company.

## 3. Read

- `<dir>/jd_snapshot.md` — the JD and its `keywords_to_mirror`
- `<dir>/trace.md` — which bullets are on this resume. The letter draws on
  these, so the letter and the resume tell one story.
- `profile/bullets.md` — those bullets' canonical text and synonyms

## 4. Write `<dir>/cover_letter.yaml`

```yaml
salutation: "Dear Hiring Manager,"
uses: [B-XXX-01, B-YYY-02]        # every bullet a sentence about you draws on
body:
  - "First paragraph."
  - "Second paragraph."
```

Write fresh prose about this role, not a mail-merge. Across the letter, cover:
- the role and a genuine reason for wanting it, with a theme from
  `company_answers.yaml` only if one is specific
- 2-3 achievements from `uses`, mapped onto the JD's keywords, honest and
  specific, with no invented scope
- a close

Start without a filler opener. Aim for 230-300 words in 2-3 paragraphs, so it
fits one page. The build measures the page itself.

## 5. De-slop

Run the `no_ai_slop` skill in edit mode over the salutation and body. Its edit
may not add a claim, tool, number or scope. Write the edited text back into
`cover_letter.yaml`.

## 6. Build

```bash
cd "$(git rev-parse --show-toplevel)" && uv run app build letter $1
```

On exit 2, follow the fix loop in `no_fab.md`. A `pages` problem means cut the
body, or merge two paragraphs, and build again.

## 7. Move the role on

```bash
cd "$(git rev-parse --show-toplevel)" && uv run track $1 tailored --if saved --note "resume + cover letter on file"
```

This fires only from `saved`, so a re-run never drags an applied role back.

## 8. Report

```
Cover letter: <dir>/<Name>_Cover_Letter.docx, pdf: <status, pages>, <words> words
Company answers: <answered keys> / null: <keys>
Next: /apply $1
```
