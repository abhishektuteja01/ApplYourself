---
description: Set up your own copy of the pipeline — the PII gate, the profile files, your first vertical, discovery config, and optional nightly automation. Resumable: re-run to continue where you left off. Also runs as a setup audit on an existing install.
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
argument-hint: "[audit | stage <n> | B1..B4 | C]"
---

# /onboarding — set up your own copy

Everything this command writes is user data under `profile/`. It never edits
code, tests, or another command.

## The interview contract (binding)

- **One question per turn.** Use `AskUserQuestion` with real defaults. Never
  batch questions, never present a wall of fields to fill.
- Per stage: **state what it unlocks and its estimate → draft → show the full
  proposed text → wait for explicit confirmation → apply exactly what was
  confirmed → verify → report → offer to stop.**
- Never apply without confirmation. If the user edits a draft, re-show the
  revised version before applying.
- **End every stage by offering to stop.** Say what the next stage is and its
  estimate. A user who stops after Stage 3 must lose nothing.
- Record progress in `profile/.onboarding.md` after each stage (format at the
  bottom). Re-running resumes from the first incomplete stage.

## Hard rules

- **No fabrication.** Bullets, skills and dates come ONLY from the user's own
  resume and their answers. If their resume does not support a claim, it does
  not go in the file. Never fill a gap to make a lane look stronger.
- **Never run `sudo`, `launchctl`, `git commit`, or `git push`.** Print those
  commands for the user to run.
- Never write a denylist pattern, bullet, or skill the user has not confirmed.
- Never edit `src/`, `tests/`, or `.claude/`. If a stage seems to need that,
  stop and report — something is wrong with the config contract, not the setup.
- Do not run `discover` or `/score` on the user's behalf. Hand off with the
  command and let them run it.

## Stage 0 — audit (2 min)

Always run this first, including when `$ARGUMENTS` is `audit`. No writes.

```bash
echo "--- toolchain ---"
# The pin, not `python3 --version`: the system interpreter is irrelevant here and
# reporting it hides a mismatch, since uv builds the venv from .python-version.
echo "pinned python: $(cat .python-version 2>/dev/null || echo '.python-version MISSING')"
uv --version || echo "MISSING: uv"
echo "--- required (pipeline cannot run without these) ---"
for f in profile/verticals.yaml profile/preferences.md \
         profile/scoring_rubric.md profile/bullets.md profile/skills_master.md \
         profile/pii_denylist.txt; do
  test -s "$f" && echo "ok   $f" || echo "MISSING $f"
done
echo "--- optional (per-command) ---"
for f in profile/voice_samples.md profile/contacts.yaml \
         profile/resume_template.docx profile/cover_letter_template.docx; do
  test -s "$f" && echo "ok   $f" || echo "absent  $f"
done
# Absent is not fatal here: src/discovery/config.py falls back to code defaults.
# Worth flagging anyway, because the default location_allowlist is not narrowed
# to anywhere the user chose.
test -s profile/discovery.yaml \
  && echo "ok   profile/discovery.yaml" \
  || echo "absent  profile/discovery.yaml (discovery runs on default filters)"
echo "--- validity ---"
uv run verticals-check 2>&1 | tail -2
test "$(git config core.hooksPath)" = ".githooks" \
  && echo "ok   pre-push hook wired" || echo "MISSING hooksPath"
echo "--- progress ---"
test -f profile/.onboarding.md && cat profile/.onboarding.md || echo "no progress file"
```

Then print a checklist mapping each result to the stage that fixes it, name the
resume point, and ask whether to start there. If everything is present and
`verticals-check` passes, report that and stop — that is the audit.

`$ARGUMENTS` of `stage <n>` (Track A), `B1`–`B4`, or `C` then jumps to that item
instead of the first incomplete one.

---

# Track A — reach a scored shortlist

Total ≈ 2.5–3.5 h. Designed for 2–3 sittings. Stop after any stage.

## Stage 1 — toolchain and the pre-push hook (10 min)

Unlocks: everything.

```bash
uv sync
uv run pytest tests -q 2>&1 | tail -3
git config core.hooksPath .githooks
```

Tests must be fully green before continuing. If they are not, stop and report —
do not start writing profile data on a broken install.

## Stage 2 — the PII gate (15 min)

Unlocks: pushing this repo anywhere public without leaking yourself.

`scripts/pii_scan.sh` reads the git **index**, so it only sees staged files. A
missing denylist is an error, not a pass.

1. `cp profile/pii_denylist.example.txt profile/pii_denylist.txt`
2. Read the example's header to the user, then interview **one category at a
   time**, in this order. Show the pattern you would write, confirm, append:
   name → email → phone → street address and city+ZIP → immigration or
   government identifiers → local filesystem paths (`~/Users/<shortname>/`) →
   account handles → other people (recruiters, referrers) → employers, clients,
   schools → private lane names.
3. Explain two mechanics as they come up. Patterns match whole words unless
   prefixed with `~`, which switches to matching anywhere — that is what a
   handle needs (`~myhandle` also catches `myhandle01`), and what a path needs
   (`~/Users/jsmith/` is the sigil plus `/Users/jsmith/`, not a home-directory
   shortcut; the leading `~` is stripped before matching). Regex metacharacters
   need escaping: `first\.last@example\.com`.
4. Verify: `git add -A && ./scripts/pii_scan.sh`

Their real denylist is gitignored. Never echo its contents into any other file.

## Stage 3 — preferences and the scoring contract (10 min)

Unlocks: scoring that knows your constraints.

1. `cp profile/preferences.example.md profile/preferences.md`
2. Ask, one at a time: work authorization (the template's three variants —
   this is what the scoring rubric's false-positive guard defers to), location,
   compensation floor, deal-breakers.
3. Delete the two unused authorization variants. Keep the file short: it is in
   the packet for every row scored.
4. `cp profile/scoring_rubric.example.md profile/scoring_rubric.md` — every
   judge reads it, so a missing file breaks `/score`. It ships working defaults;
   the only parts worth revisiting now are the three `suggested_action`
   thresholds. Offer to tune them, and move on if the user has no opinion yet.
5. Reconcile `profile/sponsorship_rules.yaml` with the authorization answer from
   step 2. Its lists assume the user is already authorized and needs no
   sponsorship. **If they need sponsorship, two edits are required** — show the
   diff and confirm before applying:
   - Move every `opt_ok:` phrase into `ineligible:`. Those phrases ("no visa
     sponsorship", "will not sponsor") mean the employer will not sponsor.
     Left in `opt_ok:` they label exactly the postings the user cannot accept as
     acceptable, then shortlist and tailor them.
   - Empty `false_positive_guard:`, whose three phrases are boilerplate only for
     someone already authorized.

   If they are a citizen or permanent resident, both lists are harmless as
   shipped; say so and move on.

## Stage 4 — bullets from your real resume (45–75 min)

Unlocks: scoring, and every generated document.

This is the longest stage and the one that matters most. Say so up front.

1. Ask for the path to their current resume. Extract it:
   - `.docx` → `uv run profile-extract <file>`
   - `.pdf` or `.md` → read it directly
2. `cp profile/bullets.example.md profile/bullets.md`, then delete the example
   entries, keeping the header comment.
3. Set `bullets_diction_pass_completed: false` in `profile/de_ai_rules.yaml`. It
   ships `true`, which exempts verbatim canonical text from banned-phrase
   linting — an exemption that makes no sense for bullets that do not exist yet.
   Step 8 offers to turn it back on.
4. Group their experience into contexts (one per employer, project, or degree)
   and agree a short `<CTX>` tag for each. Bullet ids are `B-<CTX>-NN` with a
   zero-padded two-digit sequence (`B-WID-01`), because `skills_master.md`'s
   `evidence:` references point at them.
5. **Per bullet, one at a time:** draft `canonical` from what their resume
   actually says, then show it and ask a single question — "can you defend this
   sentence on a call?" Accept, reword, or drop. Fill `source`, `tags`,
   `evidence` from their answer.
6. Leave `allowable_synonyms: []` for now. Track B fills it from real job
   postings via `/suggest-synonyms`; guessing synonyms before seeing a posting
   wastes the pass.
7. Every 3–4 bullets, report progress and offer to pause. This stage is
   resumable mid-way: bullets already written stay written.
8. When every bullet is written, offer to read them back for diction and, if the
   user accepts and is satisfied, set `bullets_diction_pass_completed: true`.
   Leave it `false` otherwise — it only claims the pass happened.

Never invent a metric. If their resume says "improved reporting" with no number,
the canonical text says that too.

## Stage 5 — the skills inventory (25 min)

Unlocks: the Skills section of every tailored resume.

1. `cp profile/skills_master.example.md profile/skills_master.md`, delete the
   example entries, keep the header.
2. Work **by category**, not one skill at a time: propose a batch drawn strictly
   from the bullets just written, and have the user strike anything they would
   not defend. Every entry needs an `evidence` reference to a real `B-*` id.
3. Leave `vertical_lean` empty. Stage 6 fills it once lanes exist.

A skill with no bullet behind it does not go in the file.

## Stage 6 — your first vertical (30–45 min)

Unlocks: discovery, classification and scoring.

1. `cp profile/verticals.example.yaml profile/verticals.yaml`. This order is
   forced: `/new-vertical`'s preflight runs `verticals-check` and refuses to work
   on an invalid config, so the file must already exist and load. Do not strip
   the example lanes before step 3 — an empty `verticals:` mapping does not load
   either.
2. Run **`/new-vertical <name>`** and let it drive. It interviews for the block,
   classifier rules, `rubric.md`, `tailoring.md`, the scoring resume, and the
   `vertical_lean` tagging in `skills_master.md`. Do not duplicate any of it here.
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

Offer to repeat this stage for a second lane, or move on.

## Stage 7 — discovery config (10 min)

Unlocks: the overnight scrape.

1. `cp profile/discovery.example.yaml profile/discovery.yaml`
2. Ask about `location_allowlist` — this is the **hard** geographic filter;
   rows outside it are dropped in cleaning. Then `deadline_hours`, then which
   sources to enable.
3. Optional: `cp profile/companies.example.yaml profile/companies.yaml` for a
   named watchlist. Explain that `data/universe/*.csv` already ships thousands
   of boards, so this is for companies they specifically care about, and that
   `name` must match how job boards spell it because it feeds `job_id`.

## Stage 8 — verify and hand off (5 min)

```bash
uv run verticals-check
uv run pytest tests -q 2>&1 | tail -3
git add -A && ./scripts/pii_scan.sh
```

Then hand off — do not run these:

```bash
uv run discover      # first scrape; writes jobs/clean.parquet
/score               # judges the new rows, writes today's shortlist
```

Tell them what to expect: an empty or thin shortlist on the first run is
normal, since only postings whose titles match a classifier rule enter the
pipeline at all.

---

# Track B — generating application material

Each item is independent. Do only the one the user wants next.

## B1 — unlock `/tailor` (30–50 min)

1. `cp profile/resume_template.example.docx profile/resume_template.docx`
2. Have them restyle it in Word: fonts, sizes and spacing on the five named
   paragraph styles. Do not rename the styles — the renderer looks them up by
   name and rejects a template that is missing one. **No tables, no images, and
   nothing in the header, footer or a text box.**
3. Only the **body** is rebuilt. The renderer clears body paragraphs and writes
   the resume markdown in their place, so the sample text there is discarded —
   but headers, footers and text boxes are preserved untouched and appear on
   every generated resume. Putting a name and contact line in the Word header is
   the most common resume-template habit and the one thing to avoid here: their
   name and contact line come from the lane's scoring resume.
4. Tell them never to edit `profile/resume_template.example.docx` itself. It is
   tracked, and re-saving it in Word stamps their name into the document
   metadata; the pre-push hook will block the push. They style the copy.
5. **Only once `/score` has produced a shortlist**, run `/suggest-synonyms` to
   populate `allowable_synonyms` in `bullets.md` and `skills_master.md`. It reads
   `shortlist/*.md` and `jobs/scored.parquet`; with neither present it has
   nothing to audit. If they have not scored yet, stop here and say so.

## B2 — unlock `/cover-letter` (15 min)

1. `cp profile/cover_letter_template.example.docx profile/cover_letter_template.docx`
2. Explain the opposite contract: this template is **preserved**, not rebuilt.
   Every paragraph that is not exactly `{{SALUTATION}}`, `{{BODY}}`, `{{DATE}}`,
   `{{CLOSING}}` or `{{SIGNOFF_NAME}}` ships verbatim in every letter — including
   tables, text boxes, headers and footers. A letterhead table here goes to every
   employer they write to.
3. **The shipped template's letterhead is literal placeholder text**, not a
   placeholder token: `NAME` at the top, `City, ST | Num| Email` under it, and
   `NAME` again after `Sincerely,`. Those are preserved verbatim, so a copy left
   unedited mails a letter headed `NAME`. Have them replace all three with their
   real details before the first `/cover-letter` run. Confirm by eye.
4. It ships `{{DATE}}`, `{{SALUTATION}}` and `{{BODY}}` only. `{{CLOSING}}` and
   `{{SIGNOFF_NAME}}` are absent by design, which is why the closing and signoff
   are static. If they would rather the renderer fill those, they can replace the
   static lines with those two tokens.
5. Same warning as B1: style the copy, never
   `profile/cover_letter_template.example.docx`, which is tracked and guarded.

## B3 — unlock `/outreach` (20 min)

1. `cp profile/voice_samples.example.md profile/voice_samples.md`
2. Ask for **real messages they actually sent**, one channel at a time:
   referral, recruiter reply, alumni ask. `/outreach` hard-refuses to run
   without this file, and polished samples produce outreach that sounds like
   nobody.
3. Optional: `cp profile/contacts.example.yaml profile/contacts.yaml`.
4. Add every real name from either file to `profile/pii_denylist.txt`. Both files
   are gitignored, so `pii_scan.sh` will never read them — the denylist entries
   are what stops those names surfacing later in something that *is* tracked.
   Do not present a rescan as verification of these two files; it cannot see them.

## B4 — `/track` and `/standup`

Nothing to configure. Explain the 11 states — `saved, skip, tailored, applied,
recruiter_contact, screen, interview, offer, rejected, withdrawn, ghosted` —
that `offer, rejected, withdrawn, ghosted` are terminal and reject every
out-transition, and that `/track` is the only writer of state.

---

# Track C — nightly discovery (macOS only, optional)

Offer this only after Track A works end to end. Skip on non-macOS: it depends on
`launchd`, `caffeinate` and `pmset`.

1. Show the wrapper: `scripts/nightly_discovery.sh`. It derives the repo from its
   own location and needs no editing.
2. Show the user's current wake schedule before touching anything —
   `pmset -g sched` — because the block below **replaces** it wholesale.
3. Print this whole block for the user to run themselves. Do not run any of it:
   it writes outside the repo, needs `sudo`, and calls `launchctl`.

```bash
LABEL=com.$USER.applyourself.discovery
mkdir -p logs ~/Library/LaunchAgents
sed -e "s|__LABEL__|$LABEL|g" -e "s|__REPO__|$PWD|g" \
    scripts/launchagent.example.plist > ~/Library/LaunchAgents/$LABEL.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/$LABEL.plist
sudo pmset repeat wakeorpoweron MTWRFSU 01:55:00
launchctl print gui/$(id -u)/$LABEL | head -20
```

Replacing an existing agent rather than installing a first one: run `launchctl
bootout gui/$(id -u)/$LABEL` before the bootstrap. Copying the plist alone
changes nothing — launchd runs the configuration it loaded, not the file on disk.

`mkdir -p logs` is not optional: `launchd` will not create the intermediate
directory for `StandardOutPath`, and a missing `logs/` means the job fails to
spawn with no log to explain why.

4. State the four ways this silently does not run, so an empty morning is
   diagnosable:
   - The Mac was asleep at 02:00 and no wake was scheduled — a `launchd` job
     whose time falls during sleep is skipped, not deferred.
   - The wake fired at 01:55 but the idle-sleep timer put the Mac back to sleep
     before 02:00. If their `pmset sleep` is under 5 minutes, move the wake to
     01:59. `caffeinate` is inside the job and cannot help before it starts.
   - The Mac was fully shut down, or on battery. `wakeorpoweron` boots to the
     login window, where the `gui/` domain holding the agent is not loaded. This
     works from sleep on AC power, not from a cold shutdown.
   - `sudo pmset repeat` silently replaced an existing repeating schedule (hence
     step 2).
5. After the first night: `ls logs/` and read the newest
   `discovery_<timestamp>.log`.

---

# Progress file

Write `profile/.onboarding.md` after every stage. Gitignored. Judgment calls
only — anything inferable from the filesystem belongs to Stage 0's audit.

```markdown
# onboarding progress

track_a_stage_completed: 4      # 0-8; 8 means Track A is done
track_b_done: [B3]              # any of B1 B2 B3 B4
track_c: declined               # done | declined | not_offered
notes:
  - contexts agreed: WID (Widget Corp), SPR (side project), EDU (degree)
  - user declined a compensation floor
  - deferred: second vertical
  - needs sponsorship, so opt_ok phrases were moved to ineligible
```

Track B items are also inferable from which files exist, so Stage 0's audit is
the authority when the two disagree. Record judgment calls here, not file
presence.
