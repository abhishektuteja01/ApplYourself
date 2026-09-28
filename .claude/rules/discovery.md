---
paths:
  - "src/discovery/**"
  - "src/ats_http.py"
  - "src/ats_registry.py"
  - "scripts/audit_slugs.py"
  - "tests/discovery/**"
  - "profile/discovery.yaml"
  - "profile/discovery.example.yaml"
  - "data/universe/**"
---

# Discovery invariants (`src/discovery/`)

## The flow

`run.py` checks tonight's due boards and searches, saves what they find into
`jobs/jobs.parquet` as it arrives, then rebuilds `jobs/clean.parquet` and writes
`jobs/runs/<run_id>.json` + `.md`. `clean.parquet` is the only discovery output
anything downstream reads. `jobs/raw/` holds nightly debug shards (every
classified listing plus its gate verdict), pruned after `raw_keep_days`; nothing
in the pipeline reads them.

## The store (`store.py`)

One row per posting, keyed by `posting_key` (`normalize.posting_key`): a board
url keys on the board's own posting id, LinkedIn on its numeric id, Indeed on
`jk`, anything else on host + path + query minus known tracking params. Never
strip every param: `gh_jid`, `jk` and unknown careers pages carry the id there.

A new posting gets its `job_id` by the first rule that matches: a known url or
key, then a known `(company_normalized, title_normalized)` pair, then the same
role from a *different* source (`same_job_elsewhere`), else the hash. A stored
row's `job_id` and `first_seen` never change. Two postings of one board never
merge, and two titles that disagree on level tokens never merge.

`job_id = sha1(company_normalized + "|" + title_normalized)[:8]`, inputs frozen.
Inherited ids (a pair whose id is not its own hash) are legitimate: they carry
merges from before the store. A hash that collides with a different pair drops
the newcomer and names it in the report.

Writes go through `store.flush` / `write_view`, under `parquet_io.locked`. The
store is saved before board state, so a board is never marked checked while
its postings are missing.

`last_score` remembers a job's fit_score after `/score` prunes it; without it an
expired low scorer comes back unscored and is judged again.

## The live view (`build_view`)

A posting is in the view when seen within `live_days`, and (search lanes only)
posted within `search_max_age_days` or undated. Then the title gate and the
location allowlist, both re-applied at build time, then JD >= `MIN_JD_CHARS`.
One row per `job_id`: an applyable url, then a resolved location, then the
newest sighting, then the longest JD. Retention runs from the job's earliest
`first_seen`: `high_days` at fit >= threshold or unscored, else `low_days`.
Tracked roles skip the search-age cut and retention; `ACTIVE_STATES` roles are
always in. Output columns are exactly `CLEAN_COLUMNS`, sorted by `job_id`.

## Boards (`sources/boards.py`)

`data/universe/boards.csv` (tracked, license-clean) and `boards.local.csv`
(gitignored, yours) are `ats,name,slug,active`. One board per (ats, slug)
whatever the case. `active=0` retires that exact spelling and outlives a re-add;
`active=1` in the local file overrides a retirement; local wins the name. The
name feeds `job_id`.

`jobs/boards.parquet` is one row per board. A board is due when `every_days`
have passed since `last_checked`; due boards run oldest first. An error leaves
`last_checked` alone; three 404s in a row make it `dead`, checked every
`dead_every_days`. There is no cursor and no resume: a killed run leaves the
rest due.

Vendor modules only list a board and fetch one posting. The loop, the gate,
known-vs-new, detail fetching and board results live in `BoardLane` once.

## Searching is not accepting

`location_allowlist.countries` is what the view accepts; `search_locations` is
what the jobspy lanes query (absent: the allowlist, capped at
`MAX_SEARCH_LOCATIONS`). An `abroad` block (`countries`, `verticals`) adds one
query per country x `abroad_terms`, remote=False; its countries must be in the
allowlist, and the view drops a row in an abroad country whose lane is not
listed. A location that is exactly a region acronym expands to its countries:
overlap keeps it unresolved, no overlap drops it.

## Config

`discovery.yaml` is `schema_version: 2`. Every key is checked; an unknown or
removed key is reported by name with its replacement. `validate()` rejects
`live_days <= max(every_days) + 1`.

## The report

`digest.py` reads `jobs/runs/*.json` and `jobs/boards.parquet` and computes
every number `/discover` shows; the session renders, it does not recompute.
A lane's trend number is `passed` (postings that passed the gate). A zero is an
alarm only with no error and a median above zero.
