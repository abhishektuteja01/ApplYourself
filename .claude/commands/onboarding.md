---
description: Set up your own copy of the pipeline in about half an hour — profile files, your first vertical, discovery config, and the two Word templates. Resumable: re-run to continue where you left off. Also runs as a setup audit on an existing install.
model: opus
effort: high
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - AskUserQuestion
argument-hint: "[audit | stage <n>]"
---

# /onboarding — set up your own copy

Seven stages, about 20 minutes, plus `/new-vertical`'s own 10-15 for your first
lane. Ends at a working `discover` → `/score` → `/tailor` → `/cover-letter`. Everything this command writes is user data under
`profile/`. It never edits code, tests, or another command.

## The interview contract (binding)

- **You do the work; the user supplies hints and a verdict.** Read their resume,
  draft the file, show it once, let them strike or reword. Never walk a user
  through a file field by field.
- **Fill in generously.** A populated file the user trims beats an empty one
  they have to fill later. Every optional list — synonyms, aliases, tags, leans,
  sources — gets a real first pass from you, never `[]` with a promise that some
  other command will handle it. Generous means *complete*, not *inflated*: the
  no-fabrication rule below is what bounds it.
- **Batch questions.** One `AskUserQuestion` call per stage, up to four related
  questions, every option carrying a real default. Ask only what you cannot
  infer from their resume or from a sane default.
- Per stage: **draft → show the full proposed text → confirm → apply exactly
  what was confirmed → verify → one-line report.** Never apply unconfirmed. If
  the user edits a draft, re-show the revised version before applying.
- Record progress in `profile/.onboarding.md` after each stage (format at the
  bottom). Re-running resumes from the first incomplete stage. A user who quits
  after any stage loses nothing.

## Hard rules

- **No fabrication.** Bullets, skills and dates come ONLY from the user's own
  resume and their answers. If their resume does not support a claim, it does
  not go in the file. Never invent a metric — if the resume says "improved
  reporting" with no number, the canonical text says that too, and never fill a
  gap to make a lane look stronger.
- **Never run `sudo`, `launchctl`, `git commit`, or `git push`.** Print those
  commands for the user to run.
- Never edit `src/`, `tests/`, or `.claude/`. If a stage seems to need that,
  stop and report — something is wrong with the config contract, not the setup.
  The one exception is delegated, not taken here: `/new-vertical`, which Stage 4
  hands the whole lane to, may add structural assertions for the new lane to
  `tests/test_real_config_drift.py` under its own rules.
- Do not run `discover` or `/score` on the user's behalf. Hand off with the
  command and let them run it.

## Stage 0 — audit (1 min)

Always run this first, including when `$ARGUMENTS` is `audit`. No writes.

```bash
echo "--- toolchain ---"
# The pin, not `python3 --version`: the system interpreter is irrelevant here and
# reporting it hides a mismatch, since uv builds the venv from .python-version.
echo "pinned python: $(cat .python-version 2>/dev/null || echo '.python-version MISSING')"
uv --version || echo "MISSING: uv"
echo "--- required (pipeline cannot run without these) ---"
for f in profile/verticals.yaml profile/preferences.md \
         profile/scoring_rubric.md profile/bullets.md profile/skills_master.md; do
  test -s "$f" && echo "ok   $f" || echo "MISSING $f"
done
echo "--- per-command ---"
for f in profile/resume_template.docx profile/cover_letter_template.docx \
         profile/voice_samples.md profile/contacts.yaml; do
  test -s "$f" && echo "ok   $f" || echo "absent  $f"
done
# Absent is not fatal here: src/discovery/config.py falls back to code defaults.
# Worth flagging anyway, because the default location_allowlist is not narrowed
# to anywhere the user chose.
test -s profile/discovery.yaml \
  && echo "ok   profile/discovery.yaml" \
  || echo "absent  profile/discovery.yaml (discovery runs on default filters)"
echo "--- validity ---"
uv run verticals-check 2>&1 | tail -5
echo "--- progress ---"
test -f profile/.onboarding.md && cat profile/.onboarding.md || echo "no progress file"
```

Map each result to the stage that fixes it in a short checklist, name the resume
point, and start there. If everything is present and `verticals-check` passes,
report that and stop — that is the audit. `$ARGUMENTS` of `stage <n>` jumps
straight to that stage instead.

## Stage 1 — install (2 min)

```bash
uv sync
uv run pytest tests -q 2>&1 | tail -3
```

Tests must be fully green before continuing. If they are not, stop and report —
do not start writing profile data on a broken install.

## Stage 2 — your constraints (2 min)

Unlocks: scoring that knows what you can accept.

```bash
cp profile/preferences.example.md profile/preferences.md
cp profile/scoring_rubric.example.md profile/scoring_rubric.md
```

One batched `AskUserQuestion`: work authorization (offer the template's three
variants verbatim — this is what the scoring rubric's false-positive guard
defers to), target locations, compensation floor, deal-breakers. Then write
`preferences.md` with the two unused authorization variants deleted. Keep it
short: it goes in the packet for every row scored.

`scoring_rubric.md` ships working defaults and every judge reads it, so the copy
alone is enough. Mention the three `suggested_action` thresholds exist and move
on unless the user has an opinion.

Then reconcile `profile/sponsorship_rules.yaml` against the authorization
answer. Its lists as shipped assume the user is authorized now and needs
sponsorship later — the F-1 OPT case — which is also harmless for a citizen or
permanent resident. **Only if the user needs sponsorship up front**, two edits
are required; apply both and report them in one line:

- Move every `opt_ok:` phrase into `ineligible:`. Those phrases ("no visa
  sponsorship", "will not sponsor") mean the employer will not sponsor. Left in
  `opt_ok:` they label exactly the postings the user cannot accept as
  acceptable, then shortlist and tailor them.
- Empty `false_positive_guard:`, whose three phrases are boilerplate only for
  someone already authorized.

## Stage 3 — bullets and skills from your resume (6 min)

Unlocks: scoring, and every generated document. This is the stage that matters
most; say so, and do the writing yourself.

1. Ask them to **drop their current resume into `profile/` and say when it is
   there** — no path to type, and `profile/*` is gitignored, so the file is
   never staged. Then find it yourself: glob `profile/` for `.docx`, `.pdf` and
   `.md` files that are not one of the known profile files or `*.example.*`
   templates. Take the only match; if there are several, name them and ask which
   one; if there are none, say what you looked for and ask again. Extract it:
   - `.docx` → `uv run profile-extract <file>`
   - `.pdf` or `.md` → read it directly
2. `cp profile/bullets.example.md profile/bullets.md`, delete the example
   entries, keep the header comment.
3. **Draft every bullet yourself** from what the resume actually says. Group the
   experience into contexts (one per employer, project, or degree) and pick a
   short `<CTX>` tag for each; ids are `B-<CTX>-NN`, zero-padded
   (`B-WID-01`), because `skills_master.md`'s `evidence:` references point at
   them. Fill `source`, `tags` and `evidence` from the resume.
4. **Fill `allowable_synonyms` for every bullet — five to eight entries, like
   the template ships.** These are the vocabulary `/tailor` is allowed to reword
   into, so an empty list means a bullet that can only ever appear one way.
   Draw them from how the same work is named elsewhere in the industry: the
   process ("month-end close"), the artifact ("throughput reporting"), the tool
   ("SQL reporting"), the outcome ("close process automation"). The bound is
   that a synonym must re-package the *same* claim — never a wider scope, a
   bigger number, or a more senior verb. "Supported" does not become "led".
   `/suggest-synonyms` adds JD-specific phrasings later; it should be extending
   a real list, not starting one.
5. `cp profile/skills_master.example.md profile/skills_master.md`, delete the
   example entries, keep the header, and draft the whole inventory from the
   bullets you just wrote. Be thorough — every tool, language, platform and
   method the bullets actually evidence, not a highlight reel. Every entry needs
   an `evidence` reference to a real `B-*` id; a skill with no bullet behind it
   does not go in the file. Fill `allowable_synonyms` here too with real display
   aliases (`"RAG"` for Retrieval-Augmented Generation, `"Postgres"` for
   PostgreSQL). Leave `vertical_lean` empty — Stage 4 fills it once a lane
   exists, and tags every skill rather than a favoured few.
6. Show both finished files in one pass and ask a single question: strike
   anything you would not defend on a call, and reword anything that overstates.
   Apply their edits verbatim. Ask about specifics only where the resume is
   genuinely ambiguous — cap it at four questions, batched.
7. Offer, once, to set `bullets_diction_pass_completed: true` in
   `profile/de_ai_rules.yaml`, which exempts the confirmed canonical text from
   Tier 2 banned-phrase linting. Leave it `false` if they would rather read the
   bullets themselves first — the flag only claims the pass happened. That file
   is committed, not user data, so say that this is the one edit here that shows
   in `git status`.

## Stage 4 — your first vertical (3 min here, plus ~10-15 min in `/new-vertical`)

Unlocks: discovery, classification and scoring.

1. `cp profile/verticals.example.yaml profile/verticals.yaml`. This order is
   forced: `/new-vertical`'s preflight runs `verticals-check` and refuses to
   work on an invalid config, so the file must already exist and load. Do not
   strip the example lanes before step 3 — an empty `verticals:` mapping does
   not load either.
2. Run **`/new-vertical <name>`** and let it drive. It interviews for the block,
   classifier rules, `rubric.md`, `tailoring.md`, the scoring resume, and the
   `vertical_lean` tagging in `skills_master.md`. Do not duplicate any of it here.

   Tell it one thing first: the new lane's classifier rules go at the **top** of
   `classifier_rules`. The example rules are still in the file until step 3, and
   two of them match plain job-title words —
   `\b(?:sprocket|governance|compliance)\b` and
   `\b(?:operations|systems? analyst)\b`. A search term containing *compliance*,
   *governance*, *operations* or *systems analyst* otherwise classifies into an
   example lane, which fails `/new-vertical`'s misclassification check. First
   match wins, so a rule at the top passes now and still passes after step 3.
3. When it finishes, strip the example scaffolding from `verticals.yaml`. All
   four edits are required — the loader rejects the file if any is missed:
   - remove the `example_primary` and `example_secondary` blocks
   - remove the three `classifier_rules` entries whose `vertical` is one of
     those two, keeping the rules `/new-vertical` drafted. A rule naming a
     vertical that no longer exists is a hard `ValueError`, not a warning.
   - point `default_vertical` at the user's lane
   - leave `out_of_lane.reasoning` in place

   Leave the `profile/verticals/example_*` directories alone — they are
   committed templates, not the user's config.
4. Verify: `uv run verticals-check`

## Stage 5 — discovery config (2 min)

Unlocks: the overnight scrape.

1. `cp profile/discovery.example.yaml profile/discovery.yaml`
2. Propose `location_allowlist` from the locations they gave in Stage 2 — this
   is the **hard** geographic filter, and rows outside it are dropped in
   cleaning, so write in every spelling a board might use (the metro, the state
   abbreviation, "Remote", "Hybrid") rather than one canonical string. Confirm
   that, and ask about `deadline_hours` and which sources to enable in the same
   batch, with every source on by default — a thin first shortlist is the
   common disappointment, and a disabled source is the usual cause.
3. Offer `cp profile/companies.example.yaml profile/companies.yaml` as a
   one-liner: `data/universe/*.csv` already ships thousands of boards, so this
   is only for companies they specifically care about, and `name` must match how
   job boards spell it because it feeds `job_id`.

## Stage 6 — the two Word templates (2 min)

Unlocks: `/tailor` and `/cover-letter`, which refuse to run without them.

```bash
cp profile/resume_template.example.docx        profile/resume_template.docx
cp profile/cover_letter_template.example.docx  profile/cover_letter_template.docx
```

Both copies work as shipped. Give them exactly three things to know, then move
on — restyling is a Word session they can do any time:

- **The resume template's body is rebuilt.** The renderer clears body paragraphs
  and writes the resume markdown in their place, so the sample text is
  discarded. Headers, footers and text boxes are *preserved* and appear on every
  generated resume — so keep a name and contact line out of the Word header,
  because those come from the lane's scoring resume. Restyle the five named
  paragraph styles freely, but do not rename them, keep Word's built-in
  `Hyperlink` character style present, and use no tables or images.
- **The cover letter template is preserved, not rebuilt** — the opposite
  contract. Every paragraph that is not exactly `{{SALUTATION}}`, `{{BODY}}`,
  `{{DATE}}`, `{{CLOSING}}` or `{{SIGNOFF_NAME}}` ships verbatim in every
  letter. Its letterhead is *literal placeholder text*, not a token: `NAME` at
  the top, `City, ST | Num| Email` under it, `NAME` again after `Sincerely,`.
  Left unedited, it mails a letter headed `NAME`. Have them fix those three
  before the first `/cover-letter` run.
- **Style the copy, never the `.example.docx`.** Those two are tracked, and
  re-saving one in Word stamps the editor's name into the document metadata;
  `uv run python scripts/scrub_example_templates.py` strips it, `--check`
  confirms.

## Stage 7 — verify and hand off (2 min)

```bash
uv run verticals-check
uv run pytest tests -q 2>&1 | tail -3
```

Then hand off — do not run these:

```bash
uv run discover                    # first scrape; writes jobs/clean.parquet
/score                             # judges the new rows, writes today's shortlist
/tailor <job_id>                   # tailored resume for one row
/cover-letter <job_id>             # letter into that row's tailor output dir
```

Tell them what to expect: an empty or thin shortlist on the first run is normal,
since only postings whose titles match a classifier rule enter the pipeline at
all.

---

# Later, when you want it

Mention these at hand-off in a couple of lines. None is an interview, and none
blocks the happy path.

- **`/suggest-synonyms`** — run it once `/score` has produced a shortlist. It
  reads `shortlist/*.md` and `jobs/scored.parquet` and extends the
  `allowable_synonyms` written in Stage 3 with phrasings taken from real
  postings. With neither file present it has nothing to audit.
- **`/outreach`** — needs `profile/voice_samples.md` and hard-refuses without it.
  `cp profile/voice_samples.example.md profile/voice_samples.md`, then paste in
  real messages they actually sent (referral, recruiter reply, alumni ask);
  polished samples produce outreach that sounds like nobody.
  `profile/contacts.example.yaml` is optional alongside it.
- **`/track` and `/standup`** — nothing to configure. 11 states: `saved, skip,
  tailored, applied, recruiter_contact, screen, interview, offer, rejected,
  withdrawn, ghosted`. The last four are terminal and reject every
  out-transition; `/track` is the only writer of state.
- **`/new-vertical`** again, for a second lane.
- **Publishing your fork publicly** — set up the PII gate first; the README's
  "Before you push" section is the whole procedure.

## Nightly discovery (macOS only)

Offer this only after the happy path works end to end. It depends on `launchd`,
`caffeinate` and `pmset`. `scripts/nightly_discovery.sh` derives the repo from
its own location and needs no editing.

Show `pmset -g sched` first — the block below **replaces** their wake schedule
wholesale — then print it for them to run. Do not run any of it: it writes
outside the repo, needs `sudo`, and calls `launchctl`.

```bash
LABEL=com.$USER.applyourself.discovery
mkdir -p logs ~/Library/LaunchAgents
sed -e "s|__LABEL__|$LABEL|g" -e "s|__REPO__|$PWD|g" \
    scripts/launchagent.example.plist > ~/Library/LaunchAgents/$LABEL.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/$LABEL.plist
sudo pmset repeat wakeorpoweron MTWRFSU 01:55:00
launchctl print gui/$(id -u)/$LABEL | head -20
```

`mkdir -p logs` is not optional: `launchd` will not create the intermediate
directory for `StandardOutPath`, and a missing `logs/` means the job fails to
spawn with no log to explain why. Replacing an existing agent rather than
installing a first one: run `launchctl bootout gui/$(id -u)/$LABEL` before the
bootstrap — copying the plist alone changes nothing, since launchd runs the
configuration it loaded.

Four ways an empty morning happens: the Mac was asleep at 02:00 with no wake
scheduled (a `launchd` job whose time falls during sleep is skipped, not
deferred); the wake fired at 01:55 but idle sleep took it back down before 02:00
(move the wake to 01:59 if their `pmset sleep` is under 5 minutes —
`caffeinate` is inside the job and cannot help before it starts); the Mac was
shut down or on battery (`wakeorpoweron` boots to the login window, where the
`gui/` domain holding the agent is not loaded); or `sudo pmset repeat` silently
replaced an existing schedule. After the first night: `ls logs/` and read the
newest `discovery_<timestamp>.log`.

---

# Progress file

Write `profile/.onboarding.md` after every stage. Gitignored. Judgment calls
only — anything inferable from the filesystem belongs to Stage 0's audit, which
is the authority when the two disagree.

```markdown
# onboarding progress

stage_completed: 4              # 0-7; 7 means setup is done
notes:
  - contexts agreed: WID (Widget Corp), SPR (side project), EDU (degree)
  - user declined a compensation floor
  - needs sponsorship, so opt_ok phrases were moved to ineligible
  - deferred: voice samples, second vertical
```
