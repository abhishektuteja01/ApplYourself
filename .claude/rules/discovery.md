---
paths:
  - "src/discovery/**"
  - "src/shared/http_client.py"
  - "src/shared/job_boards.py"
  - "src/shared/identity.py"
  - "src/shared/title_gate.py"
  - "src/shared/location/**"
  - "scripts/audit_slugs.py"
  - "tests/discovery/**"
  - "tests/shared/test_identity.py"
  - "tests/shared/test_location.py"
  - "profile/discovery.yaml"
  - "profile/discovery.example.yaml"
  - "data/universe/**"
---

# Discovery invariants (`src/discovery/`)

## The flow

`run.py` checks tonight's due boards and searches, saves what they find into
`jobs/jobs.parquet` as it arrives, then rebuilds `jobs/clean.parquet` and writes
`jobs/runs/<run_id>.json` + `.md`. `clean.parquet` is the only discovery output
anything downstream reads, apart from `jobs/maybe_duplicates.jsonl` (see the
store), which only `summary` and `app dupes` read. `jobs/raw/` holds nightly debug shards (every
classified listing plus its gate verdict), pruned after `raw_keep_days`; nothing
in the pipeline reads them.

## The store (`store.py`, job_id matching in `dedupe.py`)

One row per posting, keyed by `posting_key` (`identity.posting_key`): a board
url keys on the board's own posting id, LinkedIn on its numeric id, Indeed on
`jk`, anything else on host + path + query minus known tracking params. Never
strip every param: `gh_jid`, `jk` and unknown careers pages carry the id there.

A new posting gets its `job_id` by the first rule that matches: a known url or
key, then a known `(company_normalized, title_normalized)` pair, then the same
role on another board (`same_job_elsewhere`), else the hash. The pair and
fuzzy rules join an id only when the title agrees with that id's own title
(the pair it hashes from, when stored); otherwise it is an `other_title` maybe. A stored
row's `job_id`, `first_seen`, `company_normalized` and `title` never change: a
refresh cannot move the id pair, though it may improve the shown `company`. A posting's `board` is its url's own ATS + slug,
whichever lane found it. Two postings of one board never merge, and two titles
that disagree on level tokens never merge.

`same_job_elsewhere` merges only when the titles agree (`TITLE_MIN`, and no
role word apart beyond workplace and location words), the names are one employer
(`same_company`: equal squashed, leading whole words, or a one-word slug that
starts with the other; never a near-spelling), and the JDs, boilerplate trimmed
by scoring.yaml's `trim_jd` and word order ignored, score `JD_MERGE`. A pair
that misses on only one of these (`other_name`, `weak_jd` from `JD_MAYBE`,
`other_title`) is never merged: `store.flush` appends it once to
`jobs/maybe_duplicates.jsonl`. `discover-dupes` runs the same matcher over ids
already stored and records them there too; no id is ever rewritten.

`job_id = sha1(company_normalized + "|" + title_normalized)[:8]`, inputs frozen.
Inherited ids (a pair whose id is not its own hash) are legitimate: they carry
merges from before the store. Ids are stored, never recomputed: a hash already
held by a different pair is salted (title + `#2`, `#3`, ...) until free.
`jobs/job_ids.parquet` holds every (job_id, pair) the store ever held and is
never pruned: `_Index` reads it, so a pair `forget` dropped keeps its id and a
forgotten id is never minted for another pair. `store.flush` adds to it once it
exists; `discover-ids --dry-run|--apply` builds it (and the store's `dedupe_key`)
from jobs.parquet and pipeline/events.jsonl, `--dry-run` exiting 1 on a conflict.
`dedupe_key` (`identity.dedupe_key`: `DEDUPE_SUFFIXES` dropped, spaces squashed)
is for matching only: ids sharing it under another pair spelling are an
`other_name` maybe, never a join. Widen `DEDUPE_SUFFIXES`, never the frozen
id list, then re-run `discover-ids --apply` to refresh stored keys.

Writes go through `store.flush` / `live_view.write_view`, under `parquet_io.locked`. The
store is saved before board state, so a board is never marked checked while
its postings are missing.

`last_score` is a frozen legacy copy from when `/score` pruned: nothing writes
it, and retention reads it only for a job scored.parquet does not hold.

## The live view (`live_view.py`)

A posting is in the view when seen within `live_days`, and (search lanes only)
posted within `search_max_age_days` or undated. Then the title gate and the
location allowlist, both re-applied at build time, then JD >= `MIN_JD_CHARS`.
One row per `job_id`: an applyable url, then a resolved location, then the
newest sighting, then the longest JD. Retention runs from the job's earliest
`first_seen`: `high_days` at fit >= threshold or unscored, else `low_days`; an
unscored row `score_filter.drop_reason` drops today gets `low_days`.
Tracked roles skip the search-age cut and retention; `ACTIVE_STATES` roles are
always in. Output columns are exactly `CLEAN_COLUMNS`, sorted by `job_id`.

## Boards (`sources/boards/base.py`)

`data/universe/boards.csv` is `ats,name,slug`, the one board list, one row per
(ats, slug) whatever the case. `boards_retired.csv` (same columns) retires
those exact spellings, and a retirement outlives a re-add; an old-layout
`active=0` row still retires. The last hand-list row wins the name. The name
feeds `job_id`.

`jobs/boards.parquet` is one row per board. A board is due when `every_days`
have passed since `last_checked`; due boards run oldest first. An error leaves
`last_checked` alone; three 404s in a row make it `dead`, checked every
`dead_every_days`. There is no cursor and no resume: a killed run leaves the
rest due.

Workday (`company|wd#|site`) and Eightfold (`host|domain`) filter server-side,
so each tenant is searched with each lane's first search term. Vendor modules
only list a board and fetch one posting. The loop, the gate, known-vs-new,
detail fetching and board results live in `BoardLane` once.

## The feed (`sources/feed/stapply.py`)

One nightly parquet of public postings from many ATSs and job sites. `prepare`
runs before the other lanes: a snapshot older than `max_snapshot_age_hours` is
skipped (an error in the report, every board crawled); otherwise the file is
downloaded, checked against its sha256, title-gated a row group at a time, and
deleted. A board whose postings the file carried is `crawl.covered`: the board
lanes skip it without marking it checked, so it is first in line on a night
the feed is missing. A company is named by our `boards.csv` name, then the
feed's own company list (by slug, or site host for Phenom), then the row; the feed's
`country_iso` is added to a location only when the parser finds no country. One
paced page read serves a new row with a short JD and any row whose company
still `looks_like_slug`, read from the page's schema.org `JobPosting` block
(`jobposting_text`, also `ingest-url`'s first try; `jobposting_employer`). A
better stored name is never replaced by a slug; a site (across subdomains) whose
first ten reads tonight give nothing is not read again tonight.
An Oracle Recruiting Cloud row's JD comes from its requisition-detail API
(`feed/oracle.py`), since its job page is built in the browser; its key is tenant +
requisition id. One named only by its tenant code ("Egug") is named from its
candidate site page's `og:site_name`, read once per site a night. Feed rows are
board rows to the view: no search-age cut.

Every source parses flags, salaries and placeholder company names through
`parse.as_bool`, `parse.yearly_salary` and `identity.normalize_company`.

## Locations

`parse_location` is the one country reader; `apply_cli.job_country` falls back
to it piece by piece. A country code closing an address names the country
("Amsterdam, NH, NL"), and one that is also a US state code counts only when the
address shows otherwise ("Pune, MH, IN", "Hamburg, DE"); a leading code
("TUN - ARIANA") is the country unless the rest names another outright; a
segment that is one region's own name ("Northern Virginia") is that region
unless a same-country or major city has the name.

## Searching is not accepting

The answer bank's `work_authorization.countries` is the one country list. The
view accepts your own country and every other row in it (`location_allowlist.countries`
overrides that); `search_locations` is
what the jobspy lanes query (absent: the allowlist, capped at
`MAX_SEARCH_LOCATIONS`). The top-level `abroad` block (`lanes`, `sources`) adds,
on each listed source, one query per other bank country x lane `abroad_terms`,
remote=False; those countries must be in the allowlist, and the view drops a
row in an abroad country whose lane is not listed. `sources.*.abroad` is retired. A LinkedIn `companies` block (`terms`, `ids`) adds one home query per
term x company id; those rows have no lane of their own and are classified by
title. A location that is exactly a region acronym expands to its countries:
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
