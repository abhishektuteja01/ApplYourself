---
description: Write a one-page cover letter for a job_id from the bullets on its resume; `uv run ay app build letter` checks and renders it. Moves saved -> tailored.
model: opus
effort: medium
allowed-tools:
  - Bash(cd *)
  - Bash(git rev-parse *)
  - Bash(uv run app *)
  - Bash(uv run ay app *)
  - Bash(uv run track *)
  - Bash(uv run ay track *)
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
uv run ay app dir $1
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
- `profile/stories.md` — the stories whose `anchors` name a bullet in `trace.md`
- `profile/why.md` — your own reasons and opinions

## 4. Write `<dir>/cover_letter.yaml`

`uv run ay app schema letter` prints every key the file may hold; this is an example.

```yaml
salutation: "Dear Hiring Manager,"
uses: [S-XXX-01, B-XXX-01]        # every story and bullet a sentence about you draws on
body:
  - "First paragraph."
  - "Second paragraph."
```

Write it the way you would tell a person, not a mail-merge:
- 2 paragraphs, 150-220 words.
- Tell one story from `stories.md`, anchored to a bullet in `trace.md` and
  closest to the JD's work: what was going on, what you did, what you learned.
  At most one or two numbers. The resume carries the rest.
- A motive, preference or opinion comes only from `why.md`. None fits: state
  no motive.
- Name the company in one specific clause from `company_answers.yaml`. Never
  open with what the company does or what the role is.
- Do not end on a line mapping you onto the role, or on a thank-you. End on
  the last concrete sentence.

## 5. De-slop

Run the `no_ai_slop` skill in edit mode over the salutation and body. Its edit
may not add a claim, tool, number or scope. Write the edited text back into
`cover_letter.yaml`.

## 6. Build

```bash
uv run ay app build letter $1
```

On exit 2, follow the fix loop in `no_fab.md`. A `pages` problem means cut the
body, or merge two paragraphs, and build again.

## 7. Move the role on

```bash
uv run ay track $1 tailored --if saved --note "resume + cover letter on file"
```

This fires only from `saved`, so a re-run never drags an applied role back.

## 8. Report

```
Cover letter: <dir>/<Name>_Cover_Letter.docx, pdf: <status, pages>, <words> words
Company answers: <answered keys> / null: <keys>
Next: /apply $1
```
