# Discovery v2 — Execution Plan

SCOPE: scraping through `jobs/clean.parquet` only. Nothing downstream (scoring,
extraction, embeddings, ranking, shortlists) is part of this plan.

YOU ARE THE IMPLEMENTER. Follow steps in order. Do not skip steps, reorder steps,
or invent alternatives. Every decision is already made in this document. If a step
cannot be completed as written, STOP, mark the step `[BLOCKED: reason]` in this
file, report to the user, and wait. Never work around a blocker silently.

STATUS TRACKING: Each step has a checkbox. After completing a step, edit THIS file:
mark `[x]` plus a one-line note (what was done, any deviation). On session restart,
resume at the first unchecked step. Never skip a verify sub-step.

## R. Global rules (apply to every step)

- R1: No LLM calls anywhere in this plan's code. All of `src/discovery/` is
  deterministic. Never replace a deterministic step with an LLM call or a
  "smart" heuristic.
- R2: Code is vertical-agnostic and company-agnostic. Never hardcode a vertical
  name, search term, or company name in `src/`. All such values come from
  `profile/*.yaml` or `data/universe/*.csv`.
- R3: Every per-source and per-company failure is caught, logged, and written as
  a report line. A failure never crashes the run.
- R4: Every source emits exactly the row schema in §2. No extra columns, no
  missing columns.
- R5: A discovery run ALWAYS ends by running cleaning over whatever raw shards
  exist, even after a crash or deadline. Use try/finally.
- R6: Before writing any parser against a remote endpoint, verify the actual
  response with `curl` and record the real payload shape. Never guess payload
  shapes. If an endpoint does not work after 30 minutes of attempts, mark the
  step `[BLOCKED]` and stop.
- R7: One git commit per completed step. Message format:
  `discovery-v2 step N.M: <what>`.
- R8: Tests never read gitignored `profile/` files. `tests/conftest.py` injects
  `tests/fixtures/verticals.yaml`. Any classifier change must be mirrored into
  that fixture in the same step.
- R9: Do not build anything listed in §9.

Files to read before Phase 1 (read fully, once):
- `src/discovery/discovery.py` — current orchestrator (JobSpy + boards + inbox)
- `src/discovery/careers.py` — Greenhouse/Lever/Ashby connectors. Its patterns
  (fetch_json retry loop, BoardResult, html_to_text, _row) are the house style.
  Reuse these patterns; do not invent new ones.
- `src/discovery/cleaning.py` — dedupe/normalize/clean.parquet builder
- `src/verticals.py` — verticals config loader

## 1. Target architecture (end state)

```
src/discovery/
  __main__.py          # entry: uv run python -m src.discovery [--resume <run_id>]
  orchestrator.py      # run loop: sources in order, deadline, shards, report
  schema.py            # canonical row schema + make_row + validate_frame
  htmlutil.py          # html_to_text (moved from careers.py)
  location.py          # raw location string -> (country, state, city, remote)
  config.py            # discovery.yaml loader
  universe.py          # committed CSVs + watchlist merge, priority order, health ledger
  inbox.py             # manual inbox/*.md ingest (moved from discovery.py, logic unchanged)
  cleaning.py          # existing, modified per Phase 1 and Phase 4
  sources/
    base.py            # Source ABC + SourceResult
    jobspy_source.py   # "linkedin" and "indeed" Sources via python-jobspy
    ats/
      http.py          # shared fetch_json retry helper
      registry.py      # ATS_SOURCE_NAMES constant (imported by cleaning)
      greenhouse.py
      lever.py
      ashby.py
data/universe/
  greenhouse.csv  lever.csv  ashby.csv  README.md
profile/
  verticals.yaml       # existing, gitignored; classifier_rules extended in Phase 5
  companies.yaml       # existing watchlist, gitignored; contract unchanged
  discovery.yaml       # new, gitignored; committed example: discovery.example.yaml
jobs/
  raw/<run_id>_<source>.parquet
  runs/<run_id>.md
  clean.parquet
  universe_health.parquet
```

Sources, run order (fixed): manual (inbox) → linkedin → indeed → greenhouse →
lever → ashby → cleaning. No other sources. No lock file. Wall-clock deadline
from discovery.yaml; when reached: finish the in-flight company, skip the rest,
proceed to cleaning.

## 2. Canonical row schema (`schema.py`)

Every source returns `list[dict]` with EXACTLY these keys:

| key | type | rule |
|---|---|---|
| site | str | source name: linkedin, indeed, greenhouse, lever, ashby, manual |
| company | str | as posted |
| title | str | as posted |
| location | str | "" if unknown |
| job_url | str | listing URL |
| job_url_direct | str | apply/origin URL if known, else same value as job_url |
| description | str | plain text/markdown, truncate at 25_000 chars |
| date_posted | datetime.date or None | NEVER a string |
| is_remote | bool | |
| min_amount | float or None | |
| max_amount | float or None | |
| currency | str | "" if unknown |
| job_type | str | "" if unknown |
| job_level | str | "" if unknown |
| vertical | str | non-empty at fetch time, always |

Orchestrator adds after fetch: `ingested_run_id: str`, `scraped_date: pd.Timestamp`.

`schema.py` must provide:
- `COLUMNS: list[str]` — the exact key list above.
- `make_row(**kwargs) -> dict` — fills defaults, coerces types.
- `validate_frame(df) -> df` — asserts column set == COLUMNS + orchestrator
  columns, coerces min_amount/max_amount with `pd.to_numeric(errors="coerce")`,
  coerces date_posted with `pd.to_datetime(errors="coerce")`.

Edge case (mandatory): a frame where min_amount/max_amount are all None must end
up with float dtype after validate_frame, otherwise cross-shard concat breaks.
Follow the existing coercion pattern at careers.py:384.

## 3. Source interface (`sources/base.py`)

```python
@dataclass
class SourceResult:
    rows: list[dict]
    report_lines: list[str]
    errors: list[str]

class Source(ABC):
    name: str
    @abstractmethod
    def fetch(self, ctx) -> SourceResult: ...
```

`ctx` carries: verticals config, universe entries for this source, discovery.yaml
config, `deadline_reached() -> bool`, logger.

Implementation rules:
- `fetch()` checks `ctx.deadline_reached()` between companies/queries; if True,
  return immediately with rows collected so far.
- `fetch()` never raises. Catch per-company/per-query, append to `errors`,
  continue (pattern: careers.py scrape_boards).
- Sleep `pacing_seconds` (from discovery.yaml, hard floor 0.5) between
  companies/queries.

## 4. Company universe (`universe.py` + `data/universe/*.csv`)

CSV format, header row required: `name,slug,extra`. `extra` stays empty for all
three ATSes; keep the column anyway.

`universe.load(ats: str) -> list[UniverseCompany]` returns ALL companies (never
cap), ordered:
1. `profile/companies.yaml` entries for this ATS (`priority=True`)
2. slugs whose health-ledger `last_yield > 0`
3. all remaining slugs

Dedupe by (ats, slug); watchlist entry wins over CSV entry.

Health ledger `jobs/universe_health.parquet`, columns:
`ats, slug, consecutive_404s, last_ok, last_yield, pruned_at`.
Update rules:
- HTTP 404 for a slug → increment `consecutive_404s`. When it reaches 3, set
  `pruned_at` = today. Pruned slugs are skipped by `load()`.
- Any success → `consecutive_404s = 0`, `pruned_at` = null, `last_ok` = today,
  `last_yield` = number of rows returned.
- Recheck: if `pruned_at` is >= 14 days ago, include the slug in `load()` output
  (one retry); on success it un-prunes per the rule above, on 404 reset
  `pruned_at` = today.
- Never edit `data/universe/*.csv` at runtime.

Edge cases (all mandatory, all tested):
- Watchlist entry whose ATS is not one of greenhouse/lever/ashby → report line
  "unsupported ats", skip, do not crash.
- Missing or empty CSV for an ATS → use watchlist entries only.
- CSV row with empty name or slug → skip with a warning log.

## 5. Config (`src/discovery/config.py` + `profile/discovery.example.yaml`)

Committed example file content (user copies to gitignored `profile/discovery.yaml`):

```yaml
schema_version: 1
deadline_hours: 6
location_allowlist:
  countries: ["United States"]
  # states: ["TX", "NY"]      # optional narrowing; empty/absent = all states
  # cities: ["Austin"]        # optional narrowing; empty/absent = all cities
sources:
  linkedin:   {enabled: true, pacing_seconds: 3}
  indeed:     {enabled: true, pacing_seconds: 2}
  greenhouse: {enabled: true, pacing_seconds: 1}
  lever:      {enabled: true, pacing_seconds: 1}
  ashby:      {enabled: true, pacing_seconds: 2}
raw_retention_days: 30
```

Loader behavior (exact):
- `profile/discovery.yaml` missing → return the defaults above, log INFO.
- File malformed YAML → raise ValueError.
- Unknown key under `sources:` → raise ValueError.
- `profile/verticals.yaml` missing → exit with an error message naming the file.

## 6. Classifier rules (contract for Phase 5)

`classifier_rules` in verticals.yaml currently holds plain string terms
(substring match against title, case-insensitive). Extend the loader to ALSO
accept dict rules:

```yaml
classifier_rules:
  ai_eng:
    - "AI Engineer"                                   # plain: matches anywhere
    - {match: "ML Engineer", require_any: ["LLM", "GenAI"]}  # compound
```

Compound rule semantics: title matches only if `match` is a substring AND at
least one `require_any` term is also a substring (both case-insensitive).
Plain-string rules keep exact current behavior. The fixture
`tests/fixtures/verticals.yaml` must gain the same rule shapes.

The concrete ai_eng rule list lives in the gitignored config and is approved by
the user in step 5.2 — do not invent it. Approved (final, post-5.2 review):
match-anywhere = AI Engineer, AI Software Engineer, LLM, GenAI, Generative AI,
Applied AI, AI Agents, AI Solution; compound-gated = Forward Deployed (require_any
[Engineer]), ML Engineer / Machine Learning / Research Engineer with require_any
[LLM, GenAI, Generative, NLP, Foundation Model]. Do NOT include "Member of
Technical Staff" in any form.

### 6.1 Title exclusion (contract for step 5.3)

Each vertical in verticals.yaml MAY carry an optional `title_exclude_terms`
list of strings. Semantics (exact):

- Matching is case-insensitive and WORD-BOUNDARY based (regex `\b<term>\b`
  after `re.escape(term)`), never plain substring. "Sr" must match "Sr. AI
  Engineering Lead" but must NOT match "Srinagar"; "Intern" must NOT match
  "Internal".
- Applied in cleaning, AFTER vertical classification, to EVERY row of that
  vertical regardless of source (linkedin/indeed search rows included, not just
  board rows) — EXCEPT rows with `source == "manual"`: inbox clips were chosen
  by the user deliberately and are never excluded.
- A row matching any exclude term of its vertical is DROPPED from
  clean.parquet. Add one run-report line per vertical: "title-excluded N rows".
- Absent or empty list → no exclusion for that vertical. Verticals without the
  key behave exactly as today.
- Fixture rule R8 applies: mirror the key into `tests/fixtures/verticals.yaml`.

The concrete ai_eng list is user-approved (do not modify without a new gate):

```yaml
title_exclude_terms:
  [Senior, Sr, Staff, Principal, Lead, Director, VP, Vice President,
   Head, Manager]
```

## 7. Location handling (`location.py` + cleaning wiring)

`parse_location(raw: str) -> LocationParse(country, state, city, remote)`:
1. Remote check: if the string contains "remote" (case-insensitive) →
   `remote=True` (continue parsing the rest; "Remote - Austin, TX" yields both).
2. State match: match full state names, or two-letter abbreviations ONLY when
   preceded by a comma+space token boundary ("Austin, TX"). A bare two-letter
   token with no city context resolves to nothing (never guess CA = California
   or Canada).
3. City match: geonamescache (new pinned dependency) US cities lookup; a city
   hit fills state+country from geonames data when the string didn't provide them.
4. Country: explicit country names via geonamescache countries; "USA", "US",
   "United States" variants → United States.
5. Nothing matched → all fields empty, `remote` as found.

Allowlist filter in cleaning (three outcomes, exact):
- Parsed country/state/city all within `location_allowlist` (hierarchical: a
  listed country admits all its states/cities unless `states`/`cities` narrow
  it) → KEEP.
- Parsed to a place positively OUTSIDE the allowlist → DROP.
- Nothing parsed, or remote-only → KEEP.
Also rewrite the `location` column to canonical `"City, ST"` (or `"Remote"`,
or original string when unparsed) after parsing.

## 8. Implementation phases

### Phase 1 — Skeleton & plumbing

- [x] **1.1 schema.py + htmlutil.py.** Create both per §2. Move `html_to_text` (Note: created schema.py, htmlutil.py and modified careers.py as requested. All tests passing.)
  and its regexes from careers.py to htmlutil.py; careers.py imports from it.
  Tests: make_row defaults; validate_frame rejects missing/extra column;
  all-None amounts coerce to float dtype.
  Verify: `uv run pytest -x`.
- [x] **1.2 sources/base.py.** Per §3. (Note: Created sources/base.py and sources/__init__.py with Source and SourceResult. Tests passed.)
  Verify: `uv run pytest -x`.
- [x] **1.3 config.py + discovery.example.yaml.** Per §5. (Note: Created config.py and discovery.example.yaml. Tests passed.)
  Tests: missing file → defaults; malformed → ValueError; unknown source key →
  ValueError.
  Verify: `uv run pytest tests/test_discovery_config.py -x`.
- [x] **1.4 Shard-aware cleaning.** (Note: updated cleaning.py and added ATS registry, tests pass)
  `jobs/raw/<run_id>_*.parquet` (new) AND `jobs/raw/<run_id>.parquet` (legacy —
  old archives must keep loading; the 14-day window reads past runs' files).
  Create `sources/ats/registry.py` with `ATS_SOURCE_NAMES = {"greenhouse",
  "lever", "ashby"}`; replace the hardcoded `CAREER_SOURCES` at cleaning.py:57
  with an import of it.
  Tests: mixed legacy+shard files load together; same-run shards concat; a
  board-source row with date_posted older than 14 days survives drop_stale.
  Verify: `uv run pytest tests/test_cleaning.py -x`.
- [x] **1.5 Company normalization hardening.** (Note: updated normalize_company, added legacy_normalize_company, report output changed. Tests pass.)
  normalizer (feeds job_id hashing): lowercase, strip punctuation, strip legal
  suffixes ONLY as trailing tokens: inc, llc, corp, corporation, ltd, limited,
  co, gmbh, plc, sa.
  Tests: "Databricks Inc.", "Databricks", "databricks, inc" all hash-equal;
  "Best Co Labs" keeps "Co" (not trailing); "Coinbase" unchanged (never strip
  mid-word).
  Edge case: this changes job_ids for previously-seen suffixed companies. Add a
  run-report line on the first cleaning run after this change: count of job_ids
  that differ vs the seen ledger. Do NOT migrate any state; old pipeline entries
  keep their ids.
  Verify: `uv run pytest tests/test_cleaning.py -x`.
- [x] **1.6 Orchestrator + inbox move.** (Note: Created inbox.py and orchestrator.py, updated __main__.py, tests passed)
  local start time formatted `%Y-%m-%d_%H%M`; scraped_date = start date
  normalized; ALL shards of one run share both even across midnight. Iterate
  enabled sources in the §1 fixed order; for each: fetch → validate_frame →
  write `jobs/raw/<run_id>_<source>.parquet` → append report section → check
  deadline. try/finally: always run cleaning + write the report file.
  `--resume <run_id>`: skip any source whose shard file already exists.
  Move inbox ingest logic into inbox.py unchanged, registered as a pseudo-source
  named "manual" (runs first). `__main__.py` calls the orchestrator. Do NOT
  delete anything from discovery.py yet.
  Tests: resume skips existing shard; deadline_hours=0 → no sources fetch, only
  cleaning runs; a run with zero rows still writes an empty-frame audit parquet
  (preserve current behavior).
  Verify: `uv run pytest tests/test_orchestrator.py -x`.

### Phase 2 — Migrate existing sources

- [x] **2.1 jobspy_source.py.** Wrap the current scrape loop from discovery.py
  (scrape_one + the vertical×site×term×remote iteration) as two Sources,
  "linkedin" and "indeed" (separate shards). Preserve exactly: reduced
  linkedin_terms set, linkedin_fetch_description=True, hours_old=336,
  results_wanted=50, description_format="markdown". Country/location values come
  from config, not literals. Edge case: JobSpy sometimes returns None → treat as
  empty frame (existing pattern). After this step, delete the parts of
  discovery.py the orchestrator + jobspy_source now supersede.
  Verify: `uv run pytest -x`; then live: set every source except indeed
  `enabled: false` in profile/discovery.yaml, run
  `uv run python -m src.discovery`, confirm the indeed shard file exists, the
  report has an indeed section, and clean.parquet rebuilt.
- [x] **2.2 Migrate greenhouse/lever/ashby into sources/ats/**. One module each;
  copy parsers from careers.py verbatim; shared retry helper in
  sources/ats/http.py. Each fetch(): walk `universe.load(ats)` (watchlist-only
  until Phase 3), per company: fetch board JSON -> for each posting: classify
  title via `cleaning.classify_vertical_from_title` -> drop unclassified ->
  build row via make_row. Port careers.py tests; delete careers.py at the end
  of this step. Verify: `uv run pytest -x`; live run with only greenhouse
  enabled and a 2-company watchlist -> correct shard + report.

### Phase 3 — Universe

- [x] **3.1 universe.py + health ledger.** Per §4. (Note: Implemented, prioritizing watchlist over CSVs, prune skipping/resetting, tests pass)
  Tests: priority ordering (watchlist first, then last_yield>0, then rest);
  (ats,slug) dedupe with watchlist winning; 404 increment and reset-on-success;
  slug pruned at 3 consecutive 404s is excluded; pruned slug with pruned_at 14+
  days old is included again; unsupported-ats watchlist entry skipped; empty CSV
  → watchlist-only; empty name/slug row skipped.
  Verify: `uv run pytest tests/test_universe.py -x`.
- [x] **3.2 Seed CSVs.** Clone github.com/kalil0321/ats-scrapers (MIT). Convert (Note: Parsed 9000+ companies across greenhouse, lever, ashby into data/universe)
  its tenant lists for greenhouse, lever, ashby into §4 CSV format with a
  throwaway script (do not commit the script; commit the CSVs). Within each CSV:
  dedupe slugs; drop rows with empty slug or a URL pasted where a slug belongs.
  Write `data/universe/README.md`: source repo, MIT license, refresh procedure.
  Verify: `uv run python -c "from src.discovery import universe; print({a: len(universe.load(a)) for a in ['greenhouse','lever','ashby']})"`
  — all three nonzero.
- [x] **3.3 First scale run + report reshape.** Change the ATS report format (Note: Modified ATS plugins to new format, ran successfully with lowered deadline)
  BEFORE the run: per-ATS summary line (companies polled / ok / 404 / errors /
  rows kept) + detail lines ONLY for errors and watchlist companies (never one
  line per company). Then: live run, greenhouse only, full universe. Confirm:
  pacing ≈ 1s/company, health ledger written, shard row count sane, cleaning
  completes, report matches the new format.
  Verify: inspect `jobs/runs/<run_id>.md`; paste the summary to the user.

### Phase 4 — Location

- [x] **4.1 location.py.** (Note: Added location.py with geonamescache and robust parsing. Tests pass.)
  Per §7. Add geonamescache to pyproject (pinned).
  Test cases (all mandatory): "Austin, TX" → (US, TX, Austin, False);
  "Remote" → remote=True, rest empty; "" → all empty; "Berlin, Germany" →
  (Germany, ...); "San Francisco Bay Area" → city San Francisco OR all-empty
  (either acceptable, assert no crash and no wrong country); "New York, London
  or Singapore" → must NOT resolve to a single US location (all-empty
  acceptable); bare "CA" → all empty; "Remote - Austin, TX" → remote=True AND
  (US, TX, Austin).
  Verify: `uv run pytest tests/test_location.py -x`.
- [x] **4.2 Wire into cleaning.** (Note: Implemented filter_and_canonicalize_location inside cleaning.py. Tests pass.)
  Apply the §7 three-outcome filter using
  `location_allowlist` from config; rewrite `location` to canonical form.
  Tests: US row kept; "Berlin, Germany" dropped; empty location kept;
  "Remote" kept; canonical rewrite "austin, texas" → "Austin, TX".
  Verify: `uv run pytest tests/test_cleaning.py -x`; then a live run and
  spot-check 20 clean.parquet location values; paste them to the user.

### Phase 5 — Classifier rules

- [x] **5.1 Compound rule support.** Extend src/verticals.py per §6; update
  `tests/fixtures/verticals.yaml` with at least one compound rule.
  Tests: plain string rules behave exactly as before; compound rule matches
  "Senior ML Engineer - LLM Serving"; compound rule does NOT match
  "ML Engineer, Ads Ranking"; case-insensitive.
  Verify: `uv run pytest tests/test_verticals.py tests/test_cleaning.py -x`.
- [x] **5.2 ai_eng rules update — USER GATE.** Draft the new ai_eng
  `classifier_rules` block per the §6 direction. Run the classifier over all
  titles currently in clean.parquet; produce two lists: titles newly matched,
  titles no longer matched. STOP: present the draft block + both lists to the
  user. Only after explicit approval: write the block into
  profile/verticals.yaml AND mirror it into tests/fixtures/verticals.yaml.
  Verify: `uv run pytest -x`.
- [x] **5.3 Title exclusion (seniority filter).** Implement §6.1 exactly:
  loader support for optional `title_exclude_terms` in src/verticals.py,
  word-boundary exclusion in cleaning after classification, manual-source
  exemption, per-vertical report line. Write the user-approved ai_eng list
  from §6.1 into profile/verticals.yaml and mirror the key into
  tests/fixtures/verticals.yaml.
  Tests (all mandatory): "Senior Software Engineer (AI Agents)" dropped;
  "Sr. AI Engineering Lead" dropped; "Software Engineer, Applied AI, New Grad"
  kept; word-boundary — "Internal Tools AI Engineer" kept ("Internal" is not
  "Intern"); a manual-source row titled "Senior AI Engineer" kept; a vertical
  without the key excludes nothing.
  Verify: `uv run pytest -x`; then re-run the classifier+exclusion over all
  titles in clean.parquet and paste the count of newly-dropped titles plus a
  20-title sample to the user.

### Phase 6 — Hardening

- [x] **6.1 Raw retention.** At the end of cleaning: delete `jobs/raw/*` files
  whose run-date (parsed from the FILENAME prefix, never mtime) is older than
  `raw_retention_days`. Files with unparseable names → leave untouched. Report
  line: "pruned N raw files".
  Tests: old file deleted; recent kept; unparseable name kept.
  Verify: `uv run pytest tests/test_cleaning.py -x`.
- [x] **6.2 Report final pass.** Ensure the run report contains: per-source
  sections (jobspy: per-term rows; ATS: §8-3.3 summary format), a ZERO-rows
  flag per source, universe health summary (pruned count), inbox counts,
  per-source timing, deadline-hit marker.
  Verify: run once, inspect the report against this list item by item.
- [x] **6.3 Dress rehearsal.** Full run under `caffeinate -i uv run python -m
  src.discovery`. Kill the process mid-ATS-source. Run `--resume <run_id>`.
  Confirm: completed shards skipped, run completes, clean.parquet sane, report
  coherent. Fix whatever breaks and note it here. Then one full overnight run.
- [x] **6.4 Docs.** Rewrite README for the public repo: pipeline diagram
  (§1 flow), config contract (§5), universe provenance (§4/README), politeness
  posture (personal use, polite pacing, public endpoints only, no auth
  circumvention), `--resume`, retention. Factual tone, no marketing language.

## 9. Out of scope — do not build, do not propose

- Anything downstream of clean.parquet: scoring, extraction, embeddings,
  ranking, shortlists (separate plan, separate session).
- Browser automation of any kind.
- Any ATS connector beyond greenhouse/lever/ashby (no Workday, Recruitee,
  Personio, SmartRecruiters, Workable, Rippling, Breezy, JazzHR, iCIMS, Taleo,
  SuccessFactors).
- Company caps, lock files, per-company extra title terms.
- "Member of Technical Staff" as a classifier term.
- Embedding APIs or any paid service.
- Changes to scoring, tailoring, tracking, LaunchAgent/scheduling.
- Editing committed universe CSVs at runtime.
- HTML-parsing fallbacks for ATS JSON endpoints that stop working.
