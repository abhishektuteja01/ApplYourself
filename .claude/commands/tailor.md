---
description: Tailor a one-page resume for a job_id: the session picks bullets, wording, summary and skills; `uv run app build resume` builds it.
model: opus
effort: medium
allowed-tools:
  - Bash(cd *)
  - Bash(git rev-parse *)
  - Bash(uv run verticals-check *)
  - Bash(uv run app *)
  - Read
  - Write
argument-hint: <job_id>
---

# /tailor — one role, one resume

`$1` = the 8-hex `job_id`. Run every step through to done without pausing
(unattended runs depend on it). Read `.claude/shared/no_fab.md` first; it holds
the writing rules and the fix loop this command follows.

You make four choices. Code does everything else: it copies the fixed lines
(name, contact, headers, dates, education) from `profile/resume.yaml`, keeps
bullets in canonical order, checks every rule below, renders the docx and pdf,
writes `trace.md`, and records the folder in `state.yaml`.

## 1. Open the folder

```bash
uv run verticals-check && uv run app new $1
uv run app dupes $1
```

Stop on a nonzero exit of `app new` and show the error. The JSON gives
`work_dir`, `vertical`, `company`, `title` and `keywords_to_mirror`. `app dupes`
lists other job_ids that may be this same role and are already tailored,
applied or submitted; carry them to the report and keep going.

## 2. Read

- `<work_dir>/jd_snapshot.md` — the JD
- `profile/bullets.md` — claims, `allowable_synonyms`, and `default_for` (the
  lane's baseline picks)
- `profile/skills_master.md` — skills, aliases, and `show` (which line each sits
  on per lane)
- `profile/resume.yaml` — each context's per-lane `{min, max}` bullet budget
- the lane's `resume:` block in `profile/verticals/<vertical>/lane.yaml` — section order, total
  bullet budget, skill lines, baseline summary
- `profile/verticals/<vertical>/tailoring.md` — how this lane adapts to a JD

## 3. Write `<work_dir>/resume_plan.yaml`

Start from the lane's baseline and move toward the JD's `keywords_to_mirror`.
`uv run app schema resume` prints every key the file may hold; this is an example.

```yaml
summary:
  text: "One or two sentences framed per tailoring.md."
  uses: [B-XXX-01, B-YYY-02]      # the bullets its facts come from
entries:                          # the order of first appearance orders the projects
  - {id: B-XXX-01}                # canonical text
  - {id: B-XXX-03, text: "..."}   # reworded under REPHRASE-LICENSE
skills:                           # optional; a line left out keeps its default order
  "Line label": [SKILL-A, {id: SKILL-B, alias: "an allowable synonym"}]
```

- **Bullets:** keep a bullet that lands a keyword, and drop one that lands none,
  within each context's `{min, max}` and the lane's total. Reword only when a
  synonym carries the JD's word for the same claim.
- **Summary:** omit `summary` to keep the lane baseline. A written summary may
  only state facts from its `uses` bullets. No tricolon ("X, Y, and Z"), no
  "not only X but also Y".
- **Skills:** reorder a line so JD-matching skills lead. A skill may appear
  only on the line its `show` names.

## 4. Build

```bash
uv run app build resume $1
```

Exit 2 lists `problems` (`where`, `code`, `detail`): fix the plan and re-run,
following the fix loop in `no_fab.md`. After the fifth failed build, run it once
with `--final`. That swaps each failing rewording for its canonical bullet and a
failing summary for the lane baseline, and reports what it swapped.

## 5. Report

```
Tailored: <dir>
  <Name>_Resume.docx, pdf: <status, pages>
  trace.md — every line's source; rewordings show before -> after
  keywords not landed: <list from trace.md, or none>
  swapped by --final: <list, or none>
  possible duplicate of: <job_id company (state) per `app dupes` entry, or none>
Next: /cover-letter $1 or /apply $1
```
