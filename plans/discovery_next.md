# Discovery v2 — Handoff for the Next Stage (extraction/scoring)

Read this in a fresh session before building anything downstream of discovery.
Implementation details live in `plans/discovery_plan.md`; do not reopen them.

## What discovery is

Deterministic, LLM-free, vertical-agnostic scraping engine, run overnight as one
sequential process: `uv run python -m src.discovery [--resume <run_id>]`.
Sources (fixed): manual inbox clips → JobSpy (LinkedIn + Indeed, query-driven by
per-vertical search terms) → Greenhouse, Lever, Ashby JSON boards walked over a
large committed company universe (`data/universe/*.csv`) plus the user's
watchlist (`profile/companies.yaml`). Board rows are title-classified into a
vertical at fetch time; unclassified rows are dropped. A location allowlist
(from `profile/discovery.yaml`) drops rows positively parsed to a place outside
it; unknown/remote locations are always kept.

## The interface downstream consumes

**`jobs/clean.parquet`** — the ONLY discovery output to read. Rolling 14-day
materialized view, rebuilt at the end of every run. One row per unique job.
Columns: `job_id`, `source`, `company`, `title`, `location`
(canonicalized "City, ST" / "Remote" when parseable), `posted_date` (may be
null — null means unknown, not stale), `url`, `jd_text` (≤25k chars),
`vertical`, `scraped_date`, seen-ledger columns.

Do NOT read `jobs/raw/*` (duplicate-laden audit shards, auto-pruned) or
`jobs/universe_health.parquet` (discovery-internal). `jobs/runs/<run_id>.md` is
the human-readable run report. `jobs/seen.parquet` is the seen-ledger
(`first_seen` per job_id; retention tiers by fit score; written by cleaning,
read by shortlist rendering — scoring interacts with it as in v1, unchanged).

## Guarantees downstream can rely on

1. No LLM touched any row. Everything is raw scraped fact; nothing is judged.
2. `vertical` is non-empty on every row. Search-source rows carry the vertical
   of the term that found them; board/inbox rows carry the title-classifier
   result. Scoring never reclassifies.
3. `job_id = sha1(company_normalized + "|" + title_normalized)[:8]`, stable
   across re-scrapes and sources (url excluded from the hash). v2 hardened
   company normalization, so a small one-time set of ids differs from v1-era
   ids; old `pipeline/<id>/state.yaml` entries keep their ids, no migration.
4. Dedupe keeps the longest `jd_text` among duplicates.
5. Search-source rows age out 14 days after `posted_date`; board rows are
   exempt (presence on the board = live) and governed by the seen-ledger only.
6. Idempotent and crash-safe: any run, even partial, ends with a valid
   clean.parquet. Canonical application state lives in `pipeline/<id>/state.yaml`,
   never in `jobs/*`.
7. Volume: no discovery-side caps. Expect clean.parquet to grow from ~6k rows
   to 10–20k+ once the full universe is live. Scoring must absorb this — the
   scoring-v2 funnel design (cheap extraction + local embeddings + deterministic
   ranking first, LLM judge only on a top slice) exists for exactly this;
   its locked decisions are in project memory and
   `docs/score/v2_architecture_findings.md`.

## Locked decisions (do not relitigate)

- Public open-source code: generic and vertical-agnostic; all personal config in
  gitignored `profile/` with committed `.example` templates; universe CSVs are
  public info, committed.
- Deterministic only — no browser automation, no LLM in discovery, no paid
  services, no ATS connectors beyond Greenhouse/Lever/Ashby.
- No auto-submit anywhere in the product, ever.
- `/onboarding` (interactive profile/ setup) is a separate future stage;
  discovery fails loudly when config is missing.
