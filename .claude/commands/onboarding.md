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
argument-hint: "[audit | stage <n>]"
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
for f in profile/verticals.yaml profile/discovery.yaml profile/preferences.md \
         profile/scoring_rubric.md profile/bullets.md profile/skills_master.md \
         profile/pii_denylist.txt; do
  test -s "$f" && echo "ok   $f" || echo "MISSING $f"
done
echo "--- optional (per-command) ---"
for f in profile/voice_samples.md profile/contacts.yaml \
         profile/resume_template.docx profile/cover_letter_template.docx; do
  test -s "$f" && echo "ok   $f" || echo "absent  $f"
done
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

`$ARGUMENTS` of `stage <n>` jumps straight to that stage.

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
3. Explain two mechanics as they come up: patterns match whole words unless
   prefixed with `~`, and regex metacharacters need escaping.
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
5. Reconcile `false_positive_guard:` in `profile/sponsorship_rules.yaml` with
   the authorization answer from step 2. It ships with three "must be authorized
   to work" phrases, which are correct only for someone already authorized. If
   the user needs sponsorship, **remove them** — left in place they suppress a
   real signal and inflate the shortlist. Show the diff and confirm before
   editing.

## Stage 4 — bullets from your real resume (45–75 min)

Unlocks: scoring, and every generated document.

This is the longest stage and the one that matters most. Say so up front.

1. Ask for the path to their current resume. Extract it:
   - `.docx` → `uv run profile-extract <file>`
   - `.pdf` or `.md` → read it directly
2. `cp profile/bullets.example.md profile/bullets.md`, then delete the example
   entries, keeping the header comment.
3. Group their experience into contexts (one per employer, project, or degree)
   and agree a short `<CTX>` tag for each.
4. **Per bullet, one at a time:** draft `canonical` from what their resume
   actually says, then show it and ask a single question — "can you defend this
   sentence on a call?" Accept, reword, or drop. Fill `source`, `tags`,
   `evidence` from their answer.
5. Leave `allowable_synonyms: []` for now. Track B fills it from real job
   postings via `/suggest-synonyms`; guessing synonyms before seeing a posting
   wastes the pass.
6. Every 3–4 bullets, report progress and offer to pause. This stage is
   resumable mid-way: bullets already written stay written.

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

1. `cp profile/verticals.example.yaml profile/verticals.yaml`
2. Run **`/new-vertical <name>`** and let it drive. It interviews for the block,
   classifier rules, `rubric.md`, `tailoring.md` and the scoring resume. Do not
   duplicate its work here.
3. When it finishes, remove the `example_primary` and `example_secondary` blocks
   from `verticals.yaml` and point `default_vertical` at the user's lane. Leave
   the `profile/verticals/example_*` directories alone — they are committed
   templates, not the user's config.
4. Fill the `vertical_lean` values in `skills_master.md` for the new lane.
5. Verify: `uv run verticals-check`

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
   name and rejects a template that is missing one. No tables, no images.
3. The template's sample text is placeholder only; the renderer clears the body
   and rebuilds it from the resume markdown, so what they type there does not
   reach a generated resume. Their name and contact line come from the lane's
   scoring resume, not the template.
4. Run `/suggest-synonyms` to populate `allowable_synonyms` in `bullets.md` and
   `skills_master.md` from real shortlist postings.

## B2 — unlock `/cover-letter` (15 min)

1. `cp profile/cover_letter_template.example.docx profile/cover_letter_template.docx`
2. Explain the opposite contract: this template is **preserved**, not rebuilt.
   Every paragraph that is not exactly `{{SALUTATION}}`, `{{BODY}}`, `{{DATE}}`,
   `{{CLOSING}}` or `{{SIGNOFF_NAME}}` ships verbatim in every letter. Anything
   they add — a letterhead, an address block — goes to employers.
3. Have them style it and replace `{{SIGNOFF_NAME}}` and `{{DATE}}` with static
   text only if they want those fixed.

## B3 — unlock `/outreach` (20 min)

1. `cp profile/voice_samples.example.md profile/voice_samples.md`
2. Ask for **real messages they actually sent**, one channel at a time:
   referral, recruiter reply, alumni ask. `/outreach` hard-refuses to run
   without this file, and polished samples produce outreach that sounds like
   nobody.
3. Optional: `cp profile/contacts.example.yaml profile/contacts.yaml`.
4. Remind them every real name in either file belongs in
   `profile/pii_denylist.txt`, then re-run the scan.

## B4 — `/track` and `/standup`

Nothing to configure. Explain the 11 states — `saved, skip, tailored, applied,
recruiter_contact, screen, interview, offer, rejected, withdrawn, ghosted` —
that `offer, rejected, withdrawn, ghosted` are terminal and reject every
out-transition, and that `/track` is the only writer of state.

---

# Track C — nightly discovery (macOS only, optional)

Offer this only after Track A works end to end. Skip on non-macOS: it depends on
`launchd`, `caffeinate` and `pmset`.

1. Show the wrapper: `scripts/nightly_discovery.sh`. It derives the repo from
   its own location and needs no editing.
2. Install the agent, substituting the label and repo path:

```bash
sed -e "s|__LABEL__|com.$USER.applyourself.discovery|g" \
    -e "s|__REPO__|$PWD|g" \
    scripts/launchagent.example.plist \
    > ~/Library/LaunchAgents/com.$USER.applyourself.discovery.plist
```

3. Print these for the user to run themselves — never run them:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.$USER.applyourself.discovery.plist
sudo pmset repeat wakeorpoweron MTWRFSU 01:55:00
```

Explain why the wake matters: a LaunchAgent whose start time falls while the Mac
is asleep is skipped, not deferred. The wake is five minutes before the job.

4. Verify: `launchctl print gui/$(id -u)/com.$USER.applyourself.discovery`,
   and after the first night, `ls logs/`.

---

# Progress file

Write `profile/.onboarding.md` after every stage. Gitignored. Judgment calls
only — anything inferable from the filesystem belongs to Stage 0's audit.

```markdown
# onboarding progress

stage_completed: 4
track: A
notes:
  - contexts agreed: WID (Widget Corp), SPR (side project), EDU (degree)
  - user declined a compensation floor
  - deferred: second vertical
```
