---
paths:
  - "src/applyourself/discovery/**"
  - "src/applyourself/shared/http_client.py"
  - "src/applyourself/shared/job_boards.py"
  - "src/applyourself/shared/identity.py"
  - "src/applyourself/shared/title_gate.py"
  - "src/applyourself/shared/location/**"
  - "scripts/dev/audit_slugs.py"
  - "tests/discovery/**"
  - "tests/shared/test_identity.py"
  - "tests/shared/test_location.py"
  - "profile/discovery.yaml"
  - "profile/discovery.example.yaml"
  - "data/universe/**"
---

# Discovery invariants (`src/applyourself/discovery/`)

## The flow

`run.py` checks tonight's due boards and searches, saves what they find into
`jobs/jobs.parquet` as it arrives, then rebuilds `jobs/clean.parquet` and writes
`jobs/runs/<run_id>.json` + `.md`. `clean.parquet` is the only discovery output
anything downstream reads, apart from `jobs/maybe_duplicates.jsonl` (see the
store), which only `summary` and `app dupes` read. `jobs/raw/` holds nightly debug shards (every
classified listing plus its gate verdict and drop `reason`; the feed's passes
only), pruned after `raw_keep_days`; nothing in the pipeline reads them.

A `--jobs-dir` other than `jobs/` is a shadow run: it writes only there (its
own store and `boards.parquet`, copied from `jobs/` first), reads `pipeline/`
and the real `jobs/scored.parquet` for retention, and never touches `inbox/`
(the manual lane is skipped). `--profile-dir D` (shadow runs only) reads
`D/verticals.yaml`, its lanes under `D/verticals/`, and `D/discovery.yaml`
where present, in place of `profile/`'s; scoring.yaml and the bank stay real.

## The title gate (`shared/title_gate.py`)

One rule for every source, `lane_for(title, hint)`. Titles and terms are NFC;
terms match loosely (`shared/title_terms.py`: hyphens, spaces, plurals, word
endings, `title_variants`, never from inside a Latin word). A search row
keeps its term's lane when the title fits it: one of the lane's rules (a
`{match, require_any}` rule marked `search_is_context` needs only its match
term here), one of its search, LinkedIn or abroad terms, or a legacy
include / strong-keep term. Otherwise, and for board and feed rows, it goes to
the first classifier lane, else to the first lane one of whose search terms
it holds, else is dropped `no_lane`. A title in a lane passes unless it holds
a `title_exclude_terms` word (the global list in verticals.yaml `defaults:`,
matched as written): `banned: <word>`; a `keep: true` rule or a legacy
strong-keep term of that lane beats the ban. Each lane counts its drops per
reason with a few sample titles (`LaneStats.drops`, `drop_samples`), and the
run report lists them. A manual add, a tracked role in the view whose lane is
configured, and a stored row whose lane is no longer configured always pass in
the lane they have.

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
row's `job_id`, `company_normalized` and `title` never change, and its
`first_seen` only restarts when it comes back after more than `live_days`
unseen: a refresh cannot move the id pair, though it may improve the shown `company`. A posting's `board` is its url's own ATS + slug,
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

A posting is in the view when seen within `live_days`, and (search lanes only,
when `search_max_age_days` is set; absent is no cut) posted within it or
undated. Then the title gate and the
location allowlist, both re-applied at build time (a stored row whose title no
longer fits its lane moves to the lane it fits now), then JD >= `MIN_JD_CHARS`.
One row per `job_id`: an applyable url, then a resolved location, then the
newest sighting, then the longest JD. Retention runs from the job's earliest
`first_seen`, or with `retention_from: newest` from the newest `first_seen`
among its postings that passed the gates above (a repost restarts it): `high_days` at fit >= threshold or unscored, else `low_days`; an
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
so each tenant is searched with each lane's first `terms_per_lane` search
terms (default 3; the key is refused on any other source). Vendor modules
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
`jobs/feed_failed_reads.json` (`feed/failed_reads.py`) counts the nights a
new posting's page read gave no JD: after `page_read_max_failures` (default 3)
its page is read again only every `page_read_retry_days` (default 7); a JD
takes it off the record. Failures skip page reads only, never an API read.
An arbeitsagentur.de 404 marks the posting `gone` there: neither its API nor
its page is read, and a stored one is not seen (so it ages out of the view by
`live_days`), until `forget_after_days` pass; an API answer clears the mark.
The stored postings of tracked roles are never checked or held back.
An Oracle Recruiting Cloud row's JD comes from its requisition-detail API
(`feed/oracle.py`), since its job page is built in the browser; its key is tenant +
requisition id. One named only by its tenant code ("Egug") is named from its
candidate site page's `og:site_name`, read once per site a night. An
arbeitsagentur.de row's JD comes from the job search's detail API
(`feed/arbeitsagentur.py`, the url's reference number in base64), and a
Greenhouse, Lever, Ashby or Workday row's from that board's own posting API
(the board lane's client); with none there, the page is read as before. A 404 from such an API is the posting's
answer and never gives the API up for the night. Feed rows are
board rows to the view: no search-age cut.

Every source parses flags, salaries and placeholder company names through
`parse.as_bool`, `parse.yearly_salary` and `identity.normalize_company`.

## Locations

`parse_location` is the one country reader; `apply.role_facts.job_country` falls back
to it piece by piece. A country code closing an address names the country
("Amsterdam, NH, NL"), and one that is also a US state code counts only when the
address shows otherwise ("Pune, MH, IN", "Hamburg, DE"); a leading code
("TUN - ARIANA") is the country unless the rest names another outright; a
segment that is one region's own name ("Northern Virginia") is that region
unless a same-country or major city has the name.

## Searching is not accepting

A lane's `search_terms` is its one search list: LinkedIn, Indeed, the
`abroad` block and the Workday/Eightfold boards all read it. Its
`linkedin_terms` and `abroad_terms`, when set, still replace it on LinkedIn
and abroad.

The answer bank's `work_authorization.countries` is the one country list. The
view accepts your own country and every other row in it (`location_allowlist.countries`
overrides that); `search_locations` is
what the jobspy lanes query (absent: the allowlist, capped at
`MAX_SEARCH_LOCATIONS`). The top-level `abroad` block (`lanes`, `sources`) adds,
on each listed source, one query per other bank country x lane `abroad_terms`
(absent: its `search_terms`),
remote=False; those countries must be in the allowlist, and the view drops a
row in an abroad country whose lane is not listed. `sources.*.abroad` is retired. A LinkedIn `companies` block (`terms`, `ids`) adds one home query per
term x company id; those rows have no lane of their own and are classified by
title. A location that is exactly a region acronym expands to its countries:
overlap keeps it unresolved, no overlap drops it.

## Config

`discovery.yaml` is `schema_version: 2`. Every key is checked; an unknown or
removed key is reported by name with its replacement. `validate()` rejects
`live_days <= max(every_days) + 1` and a `results_wanted` (results per query,
per jobspy source) above `MAX_RESULTS_WANTED`.

## The report

`digest.py` reads `jobs/runs/*.json` and `jobs/boards.parquet` and computes
every number `/discover` shows; the session renders, it does not recompute.
A lane's trend number is `passed` (postings that passed the gate). A zero is an
alarm only with no error and a median above zero.
