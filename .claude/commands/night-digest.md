---
description: Write the short morning email from a night's digest_input.json. Run by the night's digest stage; never by hand.
argument-hint: "<run_dir>"
model: sonnet
effort: low
allowed-tools:
  - Read
  - Write
---

# /night-digest — the morning email

Read `$ARGUMENTS/digest_input.json`. Write `$ARGUMENTS/digest.md`. Nothing else.
Use only the numbers and questions in that file. Never add one.

## Shape

```
# Night MM-DD: <ready_to_submit> ready to submit, <waiting_on_you> waiting on you

<2–4 plain sentences: how the night went, what matters most, what to do first.
Name a failed or partial stage here in one sentence, and a limit only if it
cost something: what it held back, e.g. "apply prep finished at 10am and
handled 40 of 56".>

## The night in numbers
- Found: <discovery.new> new jobs (<discovery.took>)
- Scored: <scored> → <added_to_sheet.at_lane_cutoff> new at <lane cutoff>+, <added_to_sheet.fill_only_band> at <fill_only>–<lane cutoff - 1>
- Tailored: <tailored.ok> resumes (add failed or deferred only when not 0)
- Ready: <ready_to_submit> to submit (add "+ N waiting out a company cooldown" when not 0)
- Spent: $<spend_usd>

## Answer these to unblock most roles
1. <one question, in plain words>  <options, short, e.g. Yes / No>
    - unblocks <companies>

## Smaller things
- ...

## Quick yes/no on your skills
- Did you use <thing> on <context>?
```

## Rules

- **Group parked questions.** `parked_questions` lists each form label once.
  Merge labels only when one answer settles all of them: sponsorship worded
  three ways is one ask; LinkedIn, website and GitHub links are one ask;
  salary, CTC and bonus are one "pay" ask. Two different facts stay two asks
  (needing sponsorship and being authorized to work are not one Yes / No).
  Each numbered ask is one short question with its options, then the companies
  it unblocks. Most companies first. At most 5 numbered asks, and only asks
  that hold up 2 or more companies.
- Everything else goes to "Smaller things", one line per company: name at most
  3 of its questions in a few words each, then "and N more". Every `missing`
  entry is one line ("Triomics needs a cover letter").
- Shorten option lists: Yes / No stays; a long list becomes "pick your state"
  or similar.
- "Smaller things" also takes `drafts_to_confirm` ("N roles have drafted
  answers waiting for your OK") and `written_answers_to_review` when not 0.
- Leave out any section with nothing in it. Leave out a 0 count.
- No job ids, no commands, no file names, no dollars per stage.
- Numbers with commas (25,631). Times as "10am", not ISO stamps.
- Plain words, short sentences. No filler, no closing line, no sign-off.
- Markdown shapes only: `#`, `##`, `- ` bullets (nested by four spaces),
  numbered lines, `**bold**`, plain lines.
