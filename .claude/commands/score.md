---
description: Incrementally score new jobs against the user's profile and regenerate today's shortlist. Orchestrator mode fans out parallel Sonnet judge agents over per-vertical row ranges; judge mode (--range) scores one slice. Carries forward existing scores, judges only new job_ids, drops obsolete rows.
model: sonnet
effort: medium
allowed-tools:
  - Bash
  - Read
  - Write
  - Agent
---

# /score — incremental scoring + shortlist generation

Two modes, dispatched on `$ARGUMENTS`:

- **`--range A-B` present → judge mode** (requires `--vertical` too). You are
  a spawned judge agent: jump to the **Judge mode** section. You judge rows A
  through B (1-indexed lines) of
  `jobs/scored.staging/unscored_<vertical>.jsonl` and write batch files. You
  never dump, merge, touch `scored.parquet`, or render the shortlist.
- **No `--range` → orchestrator mode.** You run the deterministic plumbing
  via `src.score_cli` and spawn judge agents. You never judge a row yourself
  and never Read JD content or batch-file contents — your context stays
  counts-only. That isolation is the point of this architecture.

> **Parallelism rule:** parallel judging is allowed ONLY via
> orchestrator-assigned `--range` slices. A judge never picks its own rows or
> batch numbers — both derive from its assigned range and vertical, so gaps
> and collisions are impossible by construction. Never spawn a judge without
> an explicit range.

Context (details in `src/scoring_io.py` docstrings):
verticals are defined in `profile/verticals.yaml` (config order = primary
first = shortlist section order). Every row carries a precomputed `vertical`
field (a configured name) — never reclassified at scoring time.
Deterministic pre-screens run before any judge sees a row: out-of-lane
titles (`vertical=""`) and rows tripping their vertical's configured
disqualifier (title phrases, JD phrases, and/or an explicit years minimum)
auto-skip with fit=0; rows matching a `hard_ineligible` clearance/
citizenship phrase (`sponsorship_rules.yaml`) are pre-labeled
`sponsorship_label=ineligible` and excluded from the shortlist (carve-out
— never a `skip`, R8 separation holds).
Judges only judge what's left; a judged row showing a years requirement over
its vertical's `max_years` is a pre-screen bug to flag, not to silently cap.
The determinism boundary (R7) holds: `src/` does plumbing, judges do
judging.

---

# Orchestrator mode

## Step O1 — parse arguments + prerequisites

Orchestrator mode takes no flags (`--range`/`--vertical` are judge-mode
args). If someone passes anything else, explain: recovery is automatic
(Step O2), a nothing-new run already skips straight to the shortlist, and
vertical-scoped runs live on `/rescore --vertical` only.

```bash
test -f jobs/clean.parquet || { echo "ERROR: jobs/clean.parquet missing — run discovery first"; exit 1; }
test -f profile/sponsorship_rules.yaml || { echo "ERROR: profile/sponsorship_rules.yaml missing"; exit 1; }
uv run python -m src.verticals || { echo "ERROR: verticals config invalid or per-vertical rubric files missing — see message above"; exit 1; }
mkdir -p jobs/scored.staging shortlist
```

The `src.verticals` line also prints `verticals=<names> default=<name>` —
that's the configured vertical list (in order) used by Steps O4/O8.

## Step O2 — recover leftover staged batches

```bash
ls jobs/scored.staging/batch_*.json 2>/dev/null | wc -l
```

If > 0, a prior run died between judging and merging: run the Step O6 merge
now, before Step O3's dump wipes the files. On a merge `ValueError`, fix
only the named rows in the named batch file in place and re-run. Report the
recovered count, then continue.

## Step O3 — dump, then split by vertical

```bash
uv run python -m src.score_cli dump
```

Prints `rows_to_score` (rows judges must judge) plus the auto-skip counts —
`auto_skipped` (out-of-lane), `auto_ineligible` (hard-ineligible pre-label:
clearance/citizenship phrases from `sponsorship_rules.yaml`, labeled
`sponsorship_label=ineligible` and excluded from the shortlist), and one
`auto_skipped_<vertical>` per configured vertical (all already merged into
`scored.parquet`, never re-judged).

**If `rows_to_score=0`:** skip to Step O7.

```bash
uv run python -m src.score_cli split             # prints <vertical>=N per configured vertical
```

## Step O4 — assign ranges and fan out judge agents

For each configured vertical with a nonzero count, split its rows `1..count`
into consecutive ranges of 100 (last may be short). Spawn one judge per range
— all in a single message so they run in parallel, all verticals together —
with `model: sonnet` and the prompt being exactly this string, nothing more:

```
/score --range <A>-<B> --vertical <name>
```

No JD content, profile content, rubric, or reporting instructions in the
spawn prompt — the skill file and `profile/scoring_rubric.md` carry all of
it. Do not delete or rewrite the `unscored*.jsonl` files before the merge —
judges slice them by line number.

## Step O5 — completeness check

```bash
uv run python -m src.score_cli check-coverage    # exit 1 if gaps/dupes/strays
```

- **Clean** → Step O6.
- **Missing job_ids** → find their line numbers WITHOUT printing row content
  (JD text must not enter your context), e.g.
  `grep -n '<job_id>' jobs/scored.staging/unscored_<vertical>.jsonl | cut -d: -f1`,
  work out which assigned ranges are incomplete, and re-spawn ONLY those
  ranges (same prompt; a re-spawned judge overwrites its own batch files
  safely). Re-check. Still
  incomplete after ONE re-spawn → **stop and report** the failed ranges
  (R11) — do not judge them yourself, do not merge partial coverage.
- **Unexpected/duplicate job_ids** → a judge wrote outside its range: delete
  the offending batch files, re-spawn those ranges, same one-retry rule.

Row-level problems (subscores don't sum, bad label) never trigger a
re-spawn — they surface at the Step O6 merge and are fixed in place.

## Step O6 — merge

```bash
uv run python -m src.score_cli merge             # merges, prunes, cleans staging on success
```

On `ValueError: Cannot merge N invalid score(s)`: the error names the
offending row(s). Fix ONLY those rows in the named batch file in place and
re-run. Never re-spawn a judge for a row fix; never paper over invalid
rows — fail loud is the discipline.

## Step O7 — compute shortlist structure

```bash
uv run python -m src.score_cli shortlist-input   # prints n_scored, n_clean
```

Read `jobs/scored.staging/shortlist_input.json`:
- `main` — one key per configured vertical, in `verticals.yaml` config
  order: independently-ranked sections, each ≤ 25 rows with
  `fit_score >= 50`, sorted by fit_score DESC, sponsorship_pref DESC,
  posted_date DESC within its own vertical only. `ineligible` rows,
  `skip`ped rows, and rows whose pipeline state is `applied` or beyond
  never appear.
- `excluded` / `suppressed` — diagnostic-only groups (never rendered).

## Step O8 — render shortlist/<today>.md

Today = local-clock today. `M` = `n_scored`, `N` = `n_clean`
from Step O7. **One `## <display_name> (K)` section per configured vertical,
in `verticals.yaml` config order** (primary first — matches the `main` key
order), each independently numbered. `display_name` comes from the
vertical's config entry:

```markdown
# Shortlist — YYYY-MM-DD

(M of N scored, top 25 per vertical with fit ≥ 50)

## <display_name of first vertical> (K)

### 1. <fit_score> — <company> — <title>
- **job_id:** `<job_id>`
- **location:** <location> · **source:** <source> · **posted:** <posted_date>
- **fit:** <fit_score> (title <t> / skills <s> / seniority <se> / domain <d>)
- **sponsorship:** <label> — "<sponsorship_evidence>"
- **why:** <reasoning>
- **mirror in tailoring:** <kw1>, <kw2>, <kw3>
- **status:** <application_status if already_seen else "new">
- **suggested:** <suggested_action>
- **verify E-Verify** before submitting (manual v1 step)
- <url>

### 2. ...

## <display_name of next vertical> (J)

### 1. <fit_score> — <company> — <title>
(same per-row fields as above)
```

Empty section → single line `No keepers today in this vertical.` (once at
the top if all are empty). Don't fabricate filler.

## Step O9 — runtime assertions

Verify against the written file before reporting success:

- [ ] Each section ≤ 25 rows, every row `fit_score >= 50`.
- [ ] No `ineligible` row in any section.
- [ ] No row with `skip_count >= 1` in state.yaml in any section.
- [ ] Every row's four subscores sum to its displayed fit_score.
- [ ] No row outside its own vertical's section; every vertical ranked only against itself.
- [ ] Every keeper carries the "verify E-Verify" reminder.

Any failure is a bug — fix before reporting done.

## Step O10 — report

- Normal run: `judges=<count>, merged=<N>, pruned=<M>, shortlist=shortlist/YYYY-MM-DD.md (<keepers> keepers)`
- Nothing-new run (`rows_to_score=0`): shortlist line only (no judge/merge/prune counts).

Mention any Step O2 recovery, Step O5 re-spawn, or in-place row fix.

---

# Judge mode (`--range A-B --vertical <name>`)

You were spawned with an assigned single-vertical slice. Hard boundaries:
never dump, merge, or touch `scored.parquet`/shortlist; never read rows
outside your range or other judges' batch files; never pick your own batch
numbers. If `--vertical` is missing, `unscored_<vertical>.jsonl` is missing,
or your range's lines don't exist in it — stop and report, don't improvise.

## Step J1 — load rubric + profile

Read, in full:

- `profile/scoring_rubric.md` — **the shared scaffold: schema, sponsorship
  precedence, JD quality gate, action thresholds, generic self-check. Every
  judgment follows it.**
- `profile/verticals/<your vertical>/rubric.md` — **your vertical's tiers,
  caps, and additional self-check items. Apply ONLY this vertical's file.**
- the resume named by your vertical's `resume_file` in `profile/verticals.yaml`
  (every vertical sets one — `profile/verticals/<vertical>/resume_<vertical>.md`;
  the sap resume is the shared default if a key is ever absent) — what the user
  actually did. Read that path.
- `profile/bullets.md` — canonical attested bullets
- `profile/preferences.md` — locations / comp / deal-breakers
- your vertical's `skill_weights` in `profile/verticals.yaml` — weighted
  skill taxonomy for `jd_skill_overlap`
- `profile/sponsorship_rules.yaml` — phrase lists for the sponsorship label

## Step J2 — slice your rows

Extract ONLY your assigned lines (never Read the whole file — it would pull
other judges' rows into your context):

```bash
sed -n 'A,Bp' jobs/scored.staging/unscored_<vertical>.jsonl   # substitute range + vertical
```

Every row's `vertical` field must match your `--vertical`; a mismatch is a
split bug — stop and report, don't judge that row under either rubric.

## Step J3 — judge in batches of 10

Work through your slice in consecutive batches of 10 (final batch may be
short), applying ONLY your vertical's rubric file. Per batch, produce a
JSON array per the rubric's schema, run the rubric's self-check on every
row, then `Write` it to `jobs/scored.staging/batch_<vertical>_NNN.json`,
where `NNN = ceil(first_row_number/10)` zero-padded to 3 digits, using the
row's 1-indexed line number in `unscored_<vertical>.jsonl` — e.g.
`--range 51-100 --vertical <v>` writes `batch_<v>_006.json` (rows 51-60)
through `batch_<v>_010.json` (rows 91-100). Naming is collision-free across
judges by construction. **Per-batch staging is deliberate: a partial-batch
failure must survive on disk — do not write to /tmp, do not delete batch
files.**

## Step J4 — report

Reply with ONLY: your range and vertical, rows judged, batch file names with
per-file row counts. No JD content, no scores, no reasoning text — the
orchestrator's context must stay counts-only.
