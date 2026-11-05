---
description: Research a company and write company_answers.yaml (why_company / why_role / what_interests_you_about_product) into a job_id's built folder. Reuses an existing file unless --refresh. /apply and /cover-letter both call it.
model: opus
effort: medium
allowed-tools:
  - Bash(cd *)
  - Bash(git rev-parse *)
  - Bash(uv run app *)
  - Read
  - Write
  - WebSearch
  - WebFetch
argument-hint: <job_id> [--refresh]
---

# /company-answers — the "why us" answers, once per role

`$1` = the 8-hex `job_id`. Read `.claude/shared/no_fab.md` first.

## 1. Find the folder

```bash
cd "$(git rev-parse --show-toplevel)" && uv run app dir $1
```

An empty `dir` means no resume is built yet: stop and tell the user to run
`/tailor $1`. If `has_answers` is true and `$ARGUMENTS` has no `--refresh`,
report "already on file" and stop. /apply may already have sent those words, so
never overwrite them silently.

## 2. Research

Read `<dir>/jd_snapshot.md`. Find the company's actual focus, specific enough to
show you looked, never its tagline:

- Search the company name **plus a disambiguating detail from the JD** (domain
  phrase, HQ). Many names collide with unrelated companies. When the job URL's
  ATS slug suggests the company's own domain, fetch its About/Mission page
  directly.
- Take at most 1-2 concrete themes: what they build, who they serve, a stated
  focus tied to this JD's work. Prefer what they value or are trying to change
  over who runs them or how they are funded. Cite an exec, a funding round or a
  dollar figure only when the JD itself does.
- Nothing specific, the wrong company, or only marketing copy counts as no
  research. That is a correct outcome, not a failure.

## 3. Write `<dir>/company_answers.yaml`

```yaml
researched_at: YYYY-MM-DD
sources: ["https://..."]          # the pages the answers rest on
uses: []                          # bullet ids, only if a sentence claims your experience
why_company: "2-4 sentences from the research."      # or null
why_role: "2-4 sentences from the JD's actual work."  # almost never null
what_interests_you_about_product: null               # or 2-4 sentences
```

`null` means "no research". /apply parks a question rather than send an
invented "why us".

## 4. Build

```bash
cd "$(git rev-parse --show-toplevel)" && uv run app build answers $1
```

On exit 2, follow the fix loop in `no_fab.md`. Report which keys are answered
and which are null.
