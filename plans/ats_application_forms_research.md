# Research: fetching ATS application-form questions (2026-07-28)

**Status: brainstorming only. Nothing built, no code written, no decision made.**
This is a record of live endpoint probing + a corpus measurement so the work
doesn't have to be redone. Everything below was verified against real endpoints
on 2026-07-28 unless explicitly marked as unverified.

## The idea

Discovery captures the **JD**. It does not capture the **application form** — the
custom questions, work-authorization dropdowns, years-of-experience gates, essay
prompts, and EEO/demographic blocks you only see after clicking Apply. For anyone
who needs sponsorship, a chunk of the real disqualification logic lives in that
form, not in the JD text. Goal would be: pull the form ahead of time, both to prep answers
and to catch gates before investing in a tailored resume.

## What each ATS actually exposes (all three probed live)

### Greenhouse — documented public API, stable. WORKS.

```
GET https://boards-api.greenhouse.io/v1/boards/<slug>/jobs/<posting_id>?questions=true
```

Verified against `anthropic` / posting `5101378008`. Top-level keys returned:

```
absolute_url, data_compliance, internal_job_id, location, metadata, id,
updated_at, requisition_id, title, company_name, first_published, language,
application_deadline, content, departments, offices, ai_disclaimer,
include_ai_disclaimer, ai_opt_out_request_url, compliance,
demographic_questions, questions, location_questions
```

`questions` returned **19 entries**, each with `label`, `required`, and a
`fields[]` list of `{type, name, values[]}`. Field types seen: `input_text`,
`input_file`, `textarea`, `multi_value_single_select`. Live sample:

| required | question | type |
|---|---|---|
| ✅ | Are you open to working in-person in one of our offices 25% of the time? | select (2 opts) |
| ✅ | AI Policy for Application | select (2 opts) |
| ✅ | Why Anthropic? | textarea |
| ✅ | Do you require visa sponsorship? | select (2 opts) |
| ✅ | Will you now or will you in the future require employment visa sponsorship…? | select (2 opts) |
| ✅ | Are you open to relocation for this role? | select (2 opts) |
| ✅ | Have you ever interviewed at Anthropic before? | select (2 opts) |
| ⬜ | When is the earliest you would want to start working with us? | input_text |
| ⬜ | Additional Information | textarea |

Note **two separate sponsorship questions** on one form, plus `demographic_questions`
and `location_questions` as separate top-level blocks.

### Ashby — undocumented GraphQL, schema found by error-probing. WORKS.

```
POST https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting
Headers: Content-Type: application/json, Origin: https://jobs.ashbyhq.com, UA: Mozilla/5.0
```

Working query (verified against `ramp` / posting `34413f8d-26bf-4bbc-8ade-eb309a0e2245`):

```graphql
query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
  jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName,
             jobPostingId: $jobPostingId) {
    applicationForm { sections { title fieldEntries { field isRequired } } }
  }
}
```

Schema facts learned the hard way (**GraphQL introspection is disabled** — `__type`
and `__schema` are rejected; the shape below came from parsing validation-error
messages):
- `jobPosting` is type `JobPostingDetails`. There is **no** `applicationFormDefinition`
  field on it — that guess fails.
- `applicationForm` is type `FormRender`. It has `sections`, typed
  `[FormSectionRender!]!`. It does **not** have `formDefinition`, `fields`,
  `definition`, `formData`, `form`, or `sectionData` (all probed, all rejected).
- `sections[].fieldEntries` is `[FormFieldEntry!]!`.
- `fieldEntries[].field` is an **opaque JSON scalar** — must be selected bare, with
  no subfield selection, or the query fails. It deserializes to a dict with
  `title`, `type`, `path`, `selectableValues[]`.
- `FormSectionRender` has `title` but **not** `descriptionPlain`.
- `isRequired` lives on the entry, not the field.

Live result — the most valuable of the three, because these gates are invisible in
the JD:

| required | question | type |
|---|---|---|
| ✅ | Do you have a minimum of 7 years of experience building software? | Boolean |
| ✅ | Do you have a minimum of 5 years of experience building in AWS (with Terraform)? | Boolean |
| ✅ | Please elaborate on your experience building software in AWS (with Terraform) | LongText |
| ✅ | Here's some text encoded in a common format. Figure out the correct secret… | LongText |
| ✅ | In the above question, we ask you to figure out a secret to prove two things… | LongText |
| ✅ | Where do you plan on working from (for payroll tax purposes)? | Location |
| ⬜ | Cover Letter | File |

That posting embeds a **decode puzzle inside the application form itself** and two
hard YoE gates. None of it appears in `descriptionPlain`.

The board-listing endpoint already used by discovery
(`https://api.ashbyhq.com/posting-api/job-board/<slug>`) returns per-job keys:
`id, title, department, team, employmentType, location, secondaryLocations,
publishedAt, isListed, isRemote, workplaceType, address, jobUrl, applyUrl,
descriptionHtml, descriptionPlain` — **no form data**, so the second GraphQL call
is unavoidable.

The hosted apply page embeds `window.__appData`, but it holds only org/feature-flag
config (`ddRumApplicationId`, `organization.activeFeatureFlags`, …) — **the form is
not in the HTML**, it's fetched client-side. HTML scraping Ashby is not an option.

### Lever — no JSON form data. HTML scrape only. WORKS but brittle.

The public postings API returns **zero** form information. Verified against
`matchgroup` posting `3414ba28-35f7-45d3-8e13-35c883959635`:

```
GET https://api.lever.co/v0/postings/<slug>/<id>?mode=json
→ additionalPlain, additional, categories, createdAt, descriptionPlain,
  description, id, lists, salaryRange, salaryDescription, salaryDescriptionPlain,
  text, country, workplaceType, opening, openingPlain, descriptionBody,
  descriptionBodyPlain, hostedUrl, applyUrl
```

No `customQuestions`, no `questions`, no `form`. Questions exist **only** in the
apply-page HTML at `https://jobs.lever.co/<slug>/<id>/apply` (~750 KB response).

Working extraction: split on `<li class="application-question`, strip tags per
block; required-ness is signalled by the presence of the `✱` glyph inside the
block. That yielded **23 fields** correctly, including:

- Are you located in the NYC area? *(required)*
- If you answered no to the above question, are you open to relocating? *(required)*
- Are you willing to come into the NYC office during the week (3 days)…? *(required)*
- Are you authorized to work in the United States? *(required)*
- Will you now or in the future require our company to file a petition or application for employment-based immigration status…? *(required)*
- Will you require a reasonable accommodation to complete the hiring process…? *(optional)*

Field `name` attributes are `cards[<uuid>][field0..N]` — the machine names are
UUID-keyed and carry no semantics, so the human-readable label must come from the
surrounding markup. Approaches that **did not** work: `class="application-question"`
as a `<div>` split, `<div class="card">` split, `application-question-text`. Only
the `<li class="application-question` split matched.

## Corpus measurement — where the roles actually live

Measured 2026-07-28 by joining `clean.parquet` (for `url`, `source`, `company`) to
`scored.parquet` (for `fit_score`, `shortlist_rank`, `suggested_action`) on
`job_id`. Both were 2,212 rows, inner join 2,212.

`scored.parquet` carries **no `url` or `site` column** — the ATS host is only
derivable via the join to `clean.parquet.url`. Bucketing is a substring match on
that URL (`greenhouse.io` or `gh_jid=` → greenhouse, etc.).

### All 2,212 scored rows

| host | n |
|---|---|
| linkedin (aggregator) | 1059 |
| ashby | 417 |
| greenhouse | 339 |
| other/unknown | 192 |
| lever | 122 |
| workday | 35 |
| indeed (aggregator) | 35 |
| icims | 7 |
| taleo | 2 |
| jobvite | 2 |
| successfactors | 1 |
| smartrecruiters | 1 |

By discovery `source`: linkedin 1059, ashby 394, greenhouse 339, indeed 302, lever 118.
(Ashby's 417-by-URL vs 394-by-source gap = Ashby URLs arriving via other sources.)

### Shortlisted only (n=75) — the pessimistic cut

| host | n |
|---|---|
| linkedin | 43 (57%) |
| ashby | 12 |
| greenhouse | 10 |
| other/unknown | 4 |
| lever | 3 |
| workday | 2 |
| icims | 1 |

Form-reachable: **25/75 = 33%**.

### fit_score >= 80 (n=313) — the cut that matters

| host | n |
|---|---|
| linkedin | 113 |
| **ashby** | **108** |
| **greenhouse** | **54** |
| lever | 14 |
| other/unknown | 13 |
| workday | 4 |
| indeed | 4 |
| jobvite | 2 |
| icims | 1 |

- Greenhouse + Ashby = **162/313 (52%)**
- \+ Lever = **176/313 (56%)**
- `suggested_action` within this cut: **tailor 304**, manual-review 7, skip 2
- On the shortlist **and** >=80 (n=44): linkedin 23, ashby 12, greenhouse 6, lever 2, other 1

## Conclusions from the data

1. **Ashby is the feature; Greenhouse is the bonus.** 108 vs 54 at fit>=80, a 2:1
   split. Any "start with Greenhouse because it's documented" plan optimizes the
   smaller half. The undocumented GraphQL is the load-bearing dependency, so its
   fragility is the central risk, not an edge case.
2. **Lever is not worth an HTML scraper.** 14 rows at fit>=80, 3 on the shortlist.
   Highest maintenance cost, lowest yield.
3. **The LinkedIn bucket is worth less than its count.** The 113 LinkedIn rows at
   fit>=80 skew heavily to staffing and consultancy firms. That segment largely
   doesn't run Greenhouse/Ashby, and "applying" there is a recruiter email, not
   a form.
4. **Yield is very uneven across lanes.** A lane whose postings concentrate on
   aggregators gets almost nothing from this; check per-lane reachability before
   building.
5. **Volume is a non-issue at this threshold.** 304 of the 313 are already
   `suggested_action: tailor`, so gating the fetch on fit>=80 means fetching roughly
   the rows you'd tailor anyway — order ~50 requests per scoring window, not thousands.

## Design constraints if this is ever built

- **Deterministic → belongs in `src/` (R7-clean).** Fetch + parse is plumbing, no
  LLM judgment. Sketch was `src/appform.py` exposing `fetch_form(job_url) -> list[Question]`
  dispatching on ATS, plus a CLI writing `app_questions.md` into the existing
  `applications/<vertical>/<dir>/`.
- **It cannot live in discovery.** Every one of these is a *per-posting* request,
  while discovery makes *one request per company* over hundreds of companies. Adding
  a form fetch per kept row multiplies request count ~50–100× and blows the deadline
  budget. Must be lazy — triggered for a specific job, at tailor time or on demand.
- **The real fragility is URL reversal, not the fetch.** `clean.parquet` stores
  `url`, not the ATS posting id. `parse_ats_url()` in `src/discovery/ingest_url.py:72`
  already reverses per-posting ATS URLs and is the obvious reuse point — but it needs
  a case for **Greenhouse company-hosted URLs carrying `?gh_jid=`**. Confirmed live:
  `stripe` returns `https://stripe.com/jobs/search?gh_jid=7954688` and `databricks`
  returns `https://databricks.com/company/careers/open-positions/job?gh_jid=8437000002`,
  while `anthropic` returns the plain `job-boards.greenhouse.io/anthropic/jobs/<id>`.
  Both shapes are in the universe.
- **Must degrade gracefully per source.** A broken Ashby query or a Lever markup
  change should print "form unavailable, open the link" and never fail the run.
- **Output should split "prep needed" from "routine"** — essays, YoE gates,
  sponsorship, anything `LongText`/`textarea` in one bucket; name/email/resume/EEO in
  another. Otherwise the signal drowns.

## Adjacent findings worth their own decision

- **`job_url_direct` is being dropped.** JobSpy returns a `job_url_direct` field
  pointing at the employer's real ATS posting; `src/discovery/schema.py` does not
  keep it, so jobspy rows store only the LinkedIn permalink. Capturing it would give
  direct-apply links regardless of whether the form feature happens. **Unverified:**
  how often LinkedIn rows actually populate it (easy-apply postings won't have one,
  and it may only populate when `linkedin_fetch_description=True`). Measure on a live
  scrape before assuming it converts the 113.
- **Form gates as a scoring input, not just prep.** `hard_ineligible` pre-screening
  currently reads sponsorship phrases out of `jd_text` only. The Anthropic form had
  two sponsorship dropdowns and the Ramp form had two hard YoE gates — none of it in
  the JD. If forms were fetched pre-tailor, gates could kill rows before effort is
  spent, which is a bigger win than answer prep.
- **Recurring essay prompts cluster.** "Why <company>?", "biggest technical
  challenge", "tell us about a time" recur across boards and are the same shape as
  `/cover-letter` output. A small answer bank keyed to `profile/bullets.md` may beat
  regenerating per application.
- **Unrun check:** how many of the LinkedIn-URL companies at fit>=80 already appear
  as slugs in `data/universe/*.csv`. That would resolve some fraction to a known ATS
  with no schema change at all. Not measured.
