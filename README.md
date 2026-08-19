# ApplYourself

A personal, human-gated job-search pipeline. It scrapes job postings, scores
them against a profile you write, drafts tailored resumes, cover letters and
outreach, and can submit applications to a few ATS boards on your behalf.

**What it sends, and what it does not.** Outreach messages and cover letters are
only ever written to disk — nothing in this repo emails, DMs or posts a message
anywhere. Application *submission* is different: `/apply` drives a real browser
(Playwright) and can fill and submit an employer's application form. It is
opt-in per run — the default fills the form and stops; only `apply run --submit`
actually presses submit, and it lists the roles and asks you to type a
confirmation first unless you pass `--yes`. Submission covers **Greenhouse, Lever and Ashby only**.
Workday, LinkedIn, Indeed and company careers pages are always manual-apply:
they are discovered and scored like any other posting, but `/apply` will never
submit to one.

**The typed confirmation is not guaranteed.** `/apply` always passes `--yes`,
which skips the CLI's prompt, and its own replacement confirmation (Step 6b) is
skipped when the form needed no drafted answers. For a role whose form is
entirely standard fields, `/apply <job_id> --submit` submits for real with no
second confirmation.

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

That split is why the pipeline is auditable: no model ever decides something
without writing it down, in an artifact stored next to the resume it produced.
The `src/` half holds no judgment at all — given the same inputs and the same
seen-ledger, it does the same thing every time.

**The slash commands are the product.** `src/` on its own is a scraper, a docx
renderer and a form filler. You need Claude Code to run the interesting half —
the deciding.

## Pipeline

1. **Discovery** (`uv run discover`) — deterministic, LLM-free scrape. Manual
   clips in `inbox/*.md` → JobSpy (LinkedIn, Indeed) → Greenhouse/Lever/Ashby
   JSON boards and Workday over `data/universe/*.csv`. Board and inbox rows are classified
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
   docx + pdf plus audit artifacts into `applications/<vertical>/<dir>/`. Nothing
   is fabricated: every claim must trace to a canonical bullet you wrote.
   **The PDF step needs Microsoft Word and `osascript`, so it is macOS-only.**
   Everything else is portable; without Word you get the docx and convert it
   yourself.
5. **Tracking** (`/track`, `/standup`) — one `pipeline/<job_id>/state.yaml` per
   role, moving through an 11-state machine. `/track` is the only writer of
   state transitions.
6. **Submission** (`/apply`; `src/apply/`, `src/apply_cli.py`) — takes roles
   with a resume on file, scans the board's form, fills it from
   `profile/application_answers.yaml` plus a per-run answers file, and either
   submits or parks the role on whatever it could not resolve. Greenhouse,
   Lever and Ashby submit; everything else is reported as manual-apply, not
   failed. A cover letter is produced only when the board's own form asks for
   one.

`job_id = sha1(company_normalized + "|" + title_normalized)[:8]`, deliberately
excluding URL and description so it stays stable across re-scrapes.

## Setup

Requires **Python 3.12** (pinned `>=3.12,<3.13`), [uv](https://docs.astral.sh/uv/),
and Claude Code for the slash commands. PDF output additionally needs
**Microsoft Word on macOS** (it is driven by `osascript`); every other stage runs
anywhere Python does. The core install needs no C toolchain.

```bash
git clone <this repo> && cd <this repo>
uv sync
uv run pytest tests -q --ignore=tests/discovery   # should be fully green
```

Only for `uv run discover`: the location filter requires the system
**libpostal** C library, via the opt-in `discovery` dependency group. There is
no fallback parser, so `discover` will not run without it — but scoring,
tailoring and `/ingest` all do. On macOS, `brew install libpostal`. On Linux
there is no distro package; build it from source
([openvenues/libpostal](https://github.com/openvenues/libpostal)). Budget for
it: the build downloads roughly 2GB of data files, and the development headers
must be present for the `postal` Python binding to compile.

```bash
brew install libpostal   # macOS; on Linux build libpostal from source first
uv sync --group discovery
uv run pytest tests -q     # the discovery tests need libpostal; now green too
```

Only for `/apply`: the browser driver is an opt-in dependency group, and needs a
Chrome to drive. Every other stage works on a clone that never installs it.

```bash
uv sync --group apply
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 uv run playwright install chrome   # macOS
```

### Configuration

Run **`/onboarding`** in Claude Code. It reads your resume, drafts every
`profile/` file for you, and asks only what it cannot infer — your constraints,
your first lane, where you will work. About half an hour end to end, and you can
stop after any stage and re-run to continue. The one part that needs your
attention is the bullet review: you strike anything you would not defend on a
call.

It also runs as a setup audit: `/onboarding audit` on an existing install
reports what is missing or invalid without changing anything.

To do it by hand instead, every file has a template next to it:

```bash
cp profile/verticals.example.yaml       profile/verticals.yaml
cp profile/discovery.example.yaml       profile/discovery.yaml
cp profile/scoring_rubric.example.md    profile/scoring_rubric.md
cp profile/preferences.example.md       profile/preferences.md
cp profile/bullets.example.md           profile/bullets.md
cp profile/skills_master.example.md     profile/skills_master.md
cp profile/voice_samples.example.md     profile/voice_samples.md
cp profile/application_answers.example.yaml profile/application_answers.yaml
cp profile/companies.example.yaml       profile/companies.yaml   # optional
cp profile/contacts.example.yaml        profile/contacts.yaml    # optional

# The two Word designs. /tailor refuses without the first, /cover-letter without
# the second. Open each and make it yours — keep the placeholder paragraphs.
cp profile/resume_template.example.docx        profile/resume_template.docx
cp profile/cover_letter_template.example.docx  profile/cover_letter_template.docx

uv run verticals-check     # validates config + per-lane files, fails loud
```

`profile/de_ai_rules.yaml` ships `bullets_diction_pass_completed: false`, which
holds your canonical bullet text to the same banned-phrase linting as generated
prose. Once you have read your bullets for diction yourself, set it to `true` to
exempt them. (`/onboarding` offers to flip it once you have reviewed them.)

To add a lane of your own, run **`/new-vertical`** — it interviews you and
writes every piece. Copying `profile/verticals/example_primary/` by hand is not
enough on its own: the copied directory is unreferenced until you also rename
the block key in `profile/verticals.yaml`, its `display_name`, its
`resume_file` (which still points at the original), and the `vertical:` values
in `classifier_rules`. Miss any of those and `verticals-check` still passes
while your lane does nothing.

The `profile/` files you fill in are gitignored user data — nothing you write
into them is ever committed. What *is* tracked there is only the scaffolding:
every `.example.*` template, the `example_*` lane directories, and the two rule
files that ship as real defaults (see below).

### The content only you can supply

Every file below has a `.example` template documenting its schema, but the
templates are shapes, not content: the pipeline will not invent your experience.
The work is filling them in.

| file | what it is |
|---|---|
| `bullets.md` | Canonical resume bullets. **The source of truth for every generated document** — `/tailor` may reword within the synonyms you allow, never beyond them. |
| `skills_master.md` | Your skills inventory; the Skills section is assembled from here, not written fresh. |
| `preferences.md` | Work authorization, location, comp, deal-breakers. |
| `scoring_rubric.md` | Shared scoring schema and sponsorship precedence, on top of each lane's own `rubric.md`. Ships working defaults. |
| `voice_samples.md` | Messages you actually sent, so outreach sounds like you. `/outreach` **refuses to run** without it. |
| `application_answers.yaml` | The standing answers `/apply` fills forms from — contact details, work authorization, demographics, links. A field it cannot resolve here parks the role instead of guessing. |
| `contacts.yaml` | People to reach out to. Optional. |
| `resume_template.docx` | Word template whose five named paragraph styles the resume renderer fills. Its body text is discarded. |
| `cover_letter_template.docx` | Your own letter design. Everything that is not a `{{PLACEHOLDER}}` is **preserved into every letter**, so nothing decorative is free. |

The two `.example.docx` templates are the repo's only tracked binaries, so the
PII gate allowlists them by name and `tests/test_example_templates.py` stands in
for the text scan it skips. Every string they contain is a generic stand-in —
`Name`, `City, ST`, `Bullet Text 1` — and the tests fail on any text outside that
approved set, on document metadata, and on an unexpected part appearing inside
the archive. The cover letter's letterhead is literal text, not a placeholder
token, so **replace `NAME` and the contact line in your own copy** or every
letter you send is headed `NAME`.

They are hand-authored in Word, and every save stamps the editor's name into the
metadata, so run `uv run python scripts/scrub_example_templates.py` after any
edit (`--check` exits 1 if either file still needs it).

Two rule files ship as real defaults rather than templates, since they are rules
and not personal data: `profile/de_ai_rules.yaml` (banned phrasing) and
`profile/sponsorship_rules.yaml` (whose `false_positive_guard` assumes you are
already authorized to work — remove those phrases if you need sponsorship).

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
uv run ingest-url <url>               # fetch one posting -> raw parquet, then rerun cleaning
uv run score <subcommand>             # scoring plumbing (dump/split/merge/...)
uv run track <job_id> <state>         # state transition
uv run tailor-prep <job_id>           # /tailor's deterministic front-matter
uv run profile-extract <file>         # dump a .docx resume's text
uv run apply <subcommand>             # prepare / plan / fill / run [--submit]
```

`uv run apply` needs `uv sync --group apply`. `plan` prints the fill plan without
touching a form; `fill` fills one real form and stops; `run` walks the eligible
queue. `--submit` is off by default and is the only thing that presses submit:
`apply run --submit` requires an explicit `--limit` (unless `--job-id` names
one role), prints the roles it is about to apply to and asks for a typed
confirmation unless `--yes` is passed (non-interactive stdin without `--yes` is
refused, never auto-confirmed), `--rate` is clamped to a 30-second minimum, and
at most one application per company goes out per run.

Slash commands (in Claude Code): `/onboarding`, `/score`, `/rescore`, `/tailor`,
`/cover-letter`, `/company-answers`, `/apply`, `/outreach`, `/track`,
`/standup`, `/new-vertical`, `/suggest-synonyms`, `/ingest`, `/no_ai_slop`.

### Running it nightly (macOS)

`scripts/nightly_discovery.sh` plus `scripts/launchagent.example.plist` schedule
the scrape at 22:30, wrapped in `caffeinate` so sleep does not kill it mid-run. A
`launchd` job whose start time falls while the Mac is asleep is skipped rather
than deferred, so it needs a `pmset` wake a few minutes earlier. `/onboarding`
prints the install block for you at the end; scoring stays a morning decision,
since it needs a Claude Code session.

Read a command's `.md` before running it — the real orchestration logic lives
there, not in `src/`.

## Tests

```bash
uv run pytest tests -q --ignore=tests/discovery
```

Drop the `--ignore` to include `tests/discovery`, which needs
`uv sync --group discovery` (libpostal).

Tests run against a synthetic three-lane config in `tests/fixtures/verticals.yaml`
(lanes `example_primary`, `example_secondary` and `example_tertiary`, whose
contents are drawn from a fictional widget/sprocket/cog world), never your real
one. `tests/test_real_config_drift.py` additionally checks your live
`profile/verticals.yaml` when it exists, and skips on a fresh clone.

## Before you push: the PII gate

If you fork this and push anywhere public, `scripts/pii_scan.sh` fails the push
when a denylisted string reaches a tracked file:

```bash
cp profile/pii_denylist.example.txt profile/pii_denylist.txt   # then fill it in
git config core.hooksPath .githooks                            # once per clone
git add -A && ./scripts/pii_scan.sh                            # or run it by hand
```

This is opt-in and off the setup path: `/onboarding` does not touch it, because
a fork you never push anywhere public does not need it. Set it up before your
first push if you do publish, and work through the categories deliberately —
name, email, phone, address, government identifiers, local filesystem paths,
handles, other people's names, employers and schools.

Your denylist is gitignored — the list of strings to keep out is itself the thing
being kept out. Patterns match whole words by default, so a short abbreviation in
your list will not flag every longer word that happens to contain it; prefix a
pattern with `~` when you *do* want it to match inside longer words (a handle,
for instance). A missing denylist is an error, not a pass — the gate refuses to
report a scan it never ran.
`LICENSE` and the `data/universe/` CSVs are allowlisted in the script by name,
never by glob: MIT attribution and vendored company names are supposed to be
there.

## License

MIT — see [LICENSE](LICENSE).

The company seed lists in `data/universe/` are vendored from
[kalil0321/ats-scrapers](https://github.com/kalil0321/ats-scrapers) (MIT); see
`data/universe/README.md`.

Scraping job boards may conflict with their terms of service. Whether to run
this, and against what, is your call.

The same applies to `/apply`, which automates form filling and submission into
third-party ATS systems. Some of those services restrict automated submission in
their terms. Read them, and decide for yourself whether to run it.
