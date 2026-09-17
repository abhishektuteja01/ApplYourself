---
paths:
  - "src/discovery/**"
  - "tests/discovery/**"
  - "profile/discovery.yaml"
  - "profile/discovery.example.yaml"
  - "profile/companies.yaml"
  - "profile/companies.example.yaml"
---

# Discovery invariants (`src/discovery/`)

`jobs/clean.parquet` is the only discovery output anything downstream reads.
`clean.preview.jsonl` is a view of the same rows for a command session; the run
report under `jobs/runs/` is a log. Nothing downstream may read `jobs/raw/`.

`CLEAN_COLUMNS` is a closed schema in a fixed order and `coerce_schema` raises on
a missing column. Add a column there and to `PREVIEW_COLUMNS` in the same change,
or the judge cannot see it.

`found_by_term` / `found_by_remote` record the query that surfaced a row: the
jobspy lanes stamp the search term and the query's remote flag, Workday the term
that first found the posting. Every other source leaves the defaults `""` / False.

## `job_id`

`job_id = sha1(company_normalized + "|" + title_normalized)[:8]`. The hash inputs
are frozen: url and `jd_text` stay out, so the id survives a re-scrape. Changing
them orphans every `pipeline/<job_id>/state.yaml` and `applications/<dir>` key.
The 8-character length changes only behind an explicit migration.

Two rows agreeing on `(company_normalized, title_normalized)` are one posting by
construction — the hash already says so — so dedupe merges them unconditionally,
whatever the JD evidence says. A blank `company_normalized` is dropped at step 1b
rather than hashed, because it would key the id on the title alone.

## Searching is not accepting

`location_allowlist` is what cleaning accepts; `search_locations` is what the
jobspy lanes query. The ATS sources crawl globally and filter at cleaning, so the
allowlist may be as wide as you like. Absent, `search_locations` falls back to
`effective_countries()` and is capped at `MAX_SEARCH_LOCATIONS`; `validate()`
errors above the cap.

`parse_location` returns `city` for every country; only the admin1 -> state
inference is US-only, so `location_allowlist.cities` filters worldwide. A city
with no state canonicalizes as `"City, Country"`.

A location that is exactly a region acronym (`EMEA`, `EMEIA`, `APAC`, `LATAM`,
`ANZ`) expands to its constituent countries and goes through the
`candidate_countries` branch: overlap with the allowlist keeps it for review, no
overlap drops it. The list is closed. Blank, `Worldwide`, `Anywhere` and `Remote`
say nothing about country and stay keeps.

## Cleaning step order is the spec

The numbered list in `cleaning.py`'s module docstring is normative; steps 0–3b are
row-local and run per shard inside `load_filtered_window`, everything after needs
the whole window. Reordering changes which rows exist. Two orderings in particular
are load-bearing: the location filter runs before the seen-ledger, so an
out-of-allowlist row never gets `first_seen` stamped while invisible; and `job_id`
is assigned before dedupe, because a merge group's surviving id is chosen from the
ids its own members already carry.

## Dedupe and the two ledgers

The merge decision lives in `dedupe.py` and nowhere else. Board identity comes from
the one table in `sources/ats/registry.py` — never a second hostname list. Two
titles in one company that disagree on level tokens are different roles however
similar their JD, and only an identical url overrides that.

The surviving `job_id` comes from `aliases.select_sticky_id` and is never
recomputed from the canonical company name; the canonical and the sticky id are
allowed to disagree. `jobs/company_aliases.parquet` is append-only — a variant
already pinned keeps its canonical whatever a later run prefers.

Career-board sources plus `manual` are exempt from the `posted_date` staleness
cutoff: presence on the company's own board this run is the liveness signal, and
the seen-ledger governs their pipeline lifetime instead. Retention is tiered on the
latest `fit_score`; a never-scored row gets the high tier, because NaN means
unjudged, not bad.

Discovery reads `pipeline/*/state.yaml` and never writes it (R10). A row with a
state.yaml is exempt from both the staleness cutoff and expiry.

## Reading the run report back

`run_report.py` is the only reader of `jobs/runs/*.md`, and it is a reader: it
never writes and it imports stdlib only, so a digest works without the
`discovery` dependency group. Every trend number the `/discover` digest shows is
computed there; the command session renders and does not recompute.

Two cleaning formats parse to the same `after_dedupe` / `dropped_dedupe` keys —
the current single `after dedupe: N (merged M)` line and the older
`after exact dedupe` + `after near dedupe` pair. Add a funnel line to the
cleaning writer and add its label to `_FUNNEL_KEYS` in the same change.

Parsing degrades, never raises: a truncated report, a missing `## Cleaning`
half, a zero-byte file and an unknown section all yield a partial record plus a
`parse_notes` entry. `SourceStatus.SKIPPED` is representable ahead of a writer
for it, and is excluded from every median and every alarm — a cadence skip is
not a failure. A zero-row lane is an alarm only when it recorded no error and
its own median is above zero.

New-`job_id` counts are not in the report, so the digest cannot show them.
