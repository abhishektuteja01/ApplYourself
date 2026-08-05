# job-search-pipeline

A personal, human-gated job-search pipeline. It scrapes job postings, scores
them against a profile you write, and drafts tailored resumes, cover letters
and outreach — as files on disk, for you to read, edit and send yourself.

**It never submits anything.** There is no auto-apply, no form filling, no
sending. Every outward action is yours. This is deliberate and structural, not
a missing feature: no module in this repo posts to an employer, an ATS, or a
mailbox. If you want that, this is the wrong tool.

It is sponsorship-aware throughout — the scoring pre-screen skips postings that
state a hard ineligibility, and the outreach drafts have per-channel rules for
when to raise work authorization.

## How it works: judgment lives outside `src/`

The one rule that shapes the whole codebase:

> **No module under `src/` ever calls an LLM.**

`src/` is deterministic plumbing — parquet I/O, config loading, cleaning,
linting, docx rendering, state transitions. Every act of *judgment* (scoring a
posting, choosing which bullets to tailor, writing a cover letter) happens
inside a [Claude Code](https://claude.com/claude-code) slash-command session
defined in `.claude/commands/*.md`, which shells out to the `src/` helpers for
the deterministic parts.

That split is why the pipeline is auditable: re-running `discover` or `score`'s
plumbing on the same inputs gives byte-identical output, and everything a model
decided is written down in the artifacts next to the resume it produced.

**The slash commands are the product.** `src/` on its own is a scraper and a
docx renderer. You need Claude Code to run the interesting half.

## Pipeline

1. **Discovery** (`uv run discover`) — deterministic, LLM-free scrape. Manual
   clips in `inbox/*.md` → JobSpy (LinkedIn, Indeed) → Greenhouse/Lever/Ashby
   JSON boards over `data/universe/*.csv`. Board and inbox rows are classified
   into a lane by title at fetch time; unclassified rows are dropped.
2. **Cleaning** (automatic, at the end of every discovery run) — normalize,
   drop short/stale/out-of-area rows, dedupe (exact, then fuzzy at
   `rapidfuzz.WRatio >= 90`), assign a stable `job_id`. Writes
   `jobs/clean.parquet`, the only discovery output anything downstream reads.
3. **Scoring** (`/score`, `/rescore`) — parallel judge agents score each row
   against that lane's rubric and your resume, writing `jobs/scored.parquet`
   and a dated shortlist in `shortlist/`. Deterministic pre-screens auto-skip
   out-of-lane titles, per-lane disqualifiers and hard-ineligible sponsorship
   phrases before any judge sees the row.
4. **Application material** (`/tailor`, `/cover-letter`, `/outreach`) — writes
   docx + pdf plus audit artifacts into `applications/<lane>/<dir>/`. Nothing
   is fabricated: every claim must trace to a canonical bullet you wrote.
5. **Tracking** (`/track`, `/standup`) — one `pipeline/<job_id>/state.yaml` per
   role, moving through an 11-state machine. `/track` is the only writer of
   state transitions.

`job_id = sha1(company + "|" + title)[:8]`, deliberately excluding URL and
description so it stays stable across re-scrapes.

## Setup

Requires **Python 3.12** (pinned `>=3.12,<3.13`), [uv](https://docs.astral.sh/uv/),
and Claude Code for the slash commands.

```bash
git clone <this repo> && cd <this repo>
uv sync
uv run pytest tests -q     # should be fully green
```

### Configuration

Copy the templates and edit:

```bash
cp profile/verticals.example.yaml  profile/verticals.yaml
cp profile/discovery.example.yaml  profile/discovery.yaml
cp profile/companies.example.yaml  profile/companies.yaml
cp -r profile/verticals/example_primary   profile/verticals/<your_lane>
uv run verticals-check     # validates config + per-lane files, fails loud
```

Everything under `profile/` is gitignored user data. Nothing you write there
is ever committed.

### The files you have to write yourself

These have no template — they are your actual experience, and the pipeline
refuses to invent them. Create them under `profile/`:

| file | what it is |
|---|---|
| `bullets.md` | Canonical resume bullets. **The source of truth for every generated document** — `/tailor` may reword within the synonyms you allow, never beyond them. |
| `skills_master.md` | Your skills inventory; the Skills section is assembled from here, not written fresh. |
| `preferences.md` | Location, comp, role-shape preferences. |
| `scoring_rubric.md` | Shared scoring schema and sponsorship precedence, on top of each lane's own `rubric.md`. |
| `voice_samples.md` | Writing samples so outreach sounds like you. `/outreach` **refuses to run** without it. |
| `contacts.yaml` | People to reach out to. Optional. |
| `resume_template.docx`, `cover_letter_template.docx` | Word templates the renderer fills. |

Two rule files ship with sensible defaults and are yours to tune:
`profile/de_ai_rules.yaml` (banned phrasing) and `profile/sponsorship_rules.yaml`.

## Lanes ("verticals")

A **vertical** is a job lane — a distinct kind of role you're pursuing, each
with its own search terms, title classifier rules, disqualifiers, scoring
rubric, tailoring defaults, and the resume it gets judged against. Verticals
are pure config: `src/` is vertical-agnostic and hardcodes no lane, search
term, or company.

Run **`/new-vertical`** to add one. It's an interview — it drafts each piece,
shows it to you, and applies only what you confirm. `profile/verticals.example.yaml`
documents every field, and `profile/verticals/example_*/` show the per-lane
files each lane needs.

## Commands

Deterministic CLIs:

```bash
uv run discover [--resume <run_id>]   # scrape -> jobs/clean.parquet
uv run verticals-check                # validate config + per-lane files
uv run ingest-url <url>               # pull one posting into inbox/
uv run score <subcommand>             # scoring plumbing (dump/split/merge/...)
uv run track <job_id> <state>         # state transition
uv run tailor-prep <job_id>           # /tailor's deterministic front-matter
```

Slash commands (in Claude Code): `/score`, `/rescore`, `/tailor`,
`/cover-letter`, `/outreach`, `/track`, `/standup`, `/new-vertical`,
`/suggest-synonyms`, `/no_ai_slop`.

Read a command's `.md` before running it — the real orchestration logic lives
there, not in `src/`.

## Tests

```bash
uv run pytest tests -q
```

Tests run against a synthetic three-lane config in `tests/fixtures/verticals.yaml`
(fictional "widget"/"sprocket"/"cog" lanes), never your real one.
`tests/test_real_config_drift.py` additionally checks your live
`profile/verticals.yaml` when it exists, and skips on a fresh clone.

## License

MIT — see [LICENSE](LICENSE).

The company seed lists in `data/universe/` are vendored from
[kalil0321/ats-scrapers](https://github.com/kalil0321/ats-scrapers) (MIT); see
`data/universe/README.md`.

Scraping job boards may conflict with their terms of service. Whether to run
this, and against what, is your call.
