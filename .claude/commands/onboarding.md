---
description: Set up your own copy of the pipeline in about fifteen minutes — four questions, real scored jobs on screen at minute 8. Resumable: re-run to continue where you left off. Also runs as a setup audit on an existing install.
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
argument-hint: "[audit | step <n>]"
---

# /onboarding — set up your own copy

Five steps, ~15 minutes, four questions, real scored postings at step 3. Step 0
is bootstrap and carries no number the user sees. Everything written is user
data under `profile/`, except one flag in the committed `de_ai_rules.yaml`.

## Contract (binding)

- **Four questions, total.** Step 1: work authorization. Step 2:
  `/new-vertical`'s single confirm-or-edit. Step 4: two, batched into one
  `AskUserQuestion`. If a step seems to need a fifth, you inferred too little.
- **You do the work; the user gives hints and a verdict.** Draft the file, show
  it once, let them strike or reword. Never walk a user through a file field by
  field.
- **Show, don't ask.** Filled files, assumed defaults and the by-exception
  ambiguity list are printed for correction, not interviewed. Never apply
  unconfirmed text; if the user edits a draft, re-show it before applying.
- **NO-FAB over everything drafted from the resume** — read
  `.claude/shared/no_fab.md`. Nothing enters `bullets.md`, `skills_master.md` or
  `application_answers.yaml` that the resume or the four answers do not support.
- **Never run `sudo`, `launchctl`, `git commit` or `git push`.** Print them.
- Never edit `src/`, `tests/` or `.claude/`. If a step seems to need it, stop and
  report — the config contract is wrong, not the setup. One delegated exception:
  `/new-vertical` may add structural assertions for the new lane to
  `tests/test_real_config_drift.py` under its own rules.
- Step 3's `discover` and `/score` are the only things you run for the user.
  Everything downstream is handed off.
- Print the position line first in every numbered step, verbatim:
  - `Step 1 of 5 · ~13 min left · you'll see real jobs at step 3.`
  - `Step 2 of 5 · ~11 min left · you'll see real jobs at step 3.`
  - `Step 3 of 5 · ~8 min left · this is the jobs step.`
  - `Step 4 of 5 · ~4 min left · real postings, two questions.`
  - `Step 5 of 5 · done · your daily loop.`
- Write `profile/.onboarding.md` after each step (schema at the bottom).
  Re-running resumes at the first incomplete step. `$ARGUMENTS` of `step <n>`
  jumps there; `audit` runs step 0a and stops.

## Step 0 — bootstrap (unnumbered, ~2 min, 0 questions)

### 0a. Audit — no writes. This is also all `audit` mode runs.

```bash
echo "--- toolchain ---"
# The pin, not `python3 --version`: uv builds the venv from .python-version, so
# the system interpreter is irrelevant and reporting it hides a mismatch.
echo "pinned python: $(cat .python-version 2>/dev/null || echo '.python-version MISSING')"
uv --version || echo "MISSING: uv"
echo "--- required (pipeline cannot run without these) ---"
for f in profile/verticals.yaml profile/preferences.md profile/scoring_rubric.md \
         profile/bullets.md profile/skills_master.md \
         profile/resume_template.docx profile/cover_letter_template.docx; do
  test -s "$f" && echo "ok   $f" || echo "MISSING $f"; done
echo "--- optional (later menu) ---"
for f in profile/discovery.yaml profile/application_answers.yaml \
         profile/voice_samples.md profile/contacts.yaml profile/pii_denylist.txt; do
  test -s "$f" && echo "ok   $f" || echo "absent  $f"; done
# Probe the C library, not the binding: `uv run --group discovery` would try to
# compile `postal` against a libpostal that may not be there.
if pkg-config --exists libpostal 2>/dev/null || ls /usr/local/lib/libpostal.* \
   /opt/homebrew/lib/libpostal.* /usr/lib/libpostal.* >/dev/null 2>&1
then echo "ok   libpostal"; else echo "absent  libpostal (uv run discover only)"; fi
echo "--- validity ---"
uv run verticals-check 2>&1 | tail -5
echo "--- progress ---"
test -f profile/.onboarding.md && cat profile/.onboarding.md || echo "no progress file"
```

Map each result to the step that fixes it, in a short checklist, and start
there. If everything is present and `verticals-check` passes, report that and
stop — that is the audit. `audit` mode always stops here.

**libpostal, before anything begins.** `uv run discover`'s location filter needs
the `postal` binding (opt-in group `discovery`), compiled against the system
`libpostal` C library. No fallback parser. If the probe says absent, print the
install command now — the user can run it in another terminal while onboarding
continues. Not fatal: it only decides whether step 3 scrapes or falls back to
pasted URLs.

- macOS: `brew install libpostal && uv sync --group discovery`
- Linux: build from source (`github.com/openvenues/libpostal`) — no distro
  packages it; install its dev headers, expect a ~2 GB data download.

### 0b. Install, then one background test run

```bash
uv sync
mkdir -p logs
nohup uv run pytest tests -q --ignore=tests/discovery > logs/onboarding_tests.log 2>&1 &
```

That is the only test run in this command. Do not wait for it and do not start a
second one — step 5 reads the log at hand-off.

`onboard-scaffold` does every remaining chore, but two of its flags are answers
you do not have yet (`--vertical`, `--work-auth`), so it runs at the top of step
1. Nothing between here and there needs it.

## Step 1 — your resume and your work authorization (~2 min, 1 question)

1. Ask them to **drop their current resume into `profile/` and say when it is
   there** — no path to type, and `profile/*` is gitignored. Then find it: glob
   `profile/` for `.docx`, `.pdf` and `.md` files that are neither a known
   profile file nor a `*.example.*` template. One match, take it; several, name
   them and ask which; none, say what you looked for and ask again.
   - `.docx` → `uv run profile-extract <file>`
   - `.pdf` or `.md` → read it directly

2. **Question 1**, one `AskUserQuestion`: *"What's your work authorization?"*
   - `Citizen or permanent resident`
   - `Need sponsorship now`
   - `Authorized now, time-limited` (F-1 OPT, STEM OPT, any visa with an end date)

   One answer, three files — the scaffold flag, `preferences.md`'s
   authorization section, `application_answers.yaml`:

   | answer | `--work-auth` | `application_answers.yaml` |
   |---|---|---|
   | citizen or PR | `citizen` | `citizen_or_pr` |
   | needs sponsorship now | `needs_now` | `needs_sponsorship_now` |
   | time-limited | `time_limited` | `time_limited` |

3. Bootstrap, continued — both scaffold inputs now exist. Derive the lane name
   from the resume (snake_case, `^[a-z][a-z0-9_]*$`); step 2 carries it into
   what `/new-vertical` confirms.

   ```bash
   uv run onboard-scaffold --vertical <lane> --work-auth <citizen|needs_now|time_limited> --dry-run
   uv run onboard-scaffold --vertical <lane> --work-auth <citizen|needs_now|time_limited>
   ```

   Copies every `profile/*.example.*` to its real name including the two Word
   templates, strips the example lanes from `verticals.yaml`, reconciles
   `sponsorship_rules.yaml`. Never overwrites — an existing file is skipped and
   listed. Read the report; `SKIPPED` and `FAILED` are the only parts that need
   you. `--with-apply` and `--with-optional` are step 5's menu, not this path.

   Both Word templates work as copied, with one thing to fix before the first
   `/cover-letter`: the letter's letterhead is literal placeholder text — `NAME`
   at the top, `City, ST | Num| Email` under it, `NAME` again after `Sincerely,`
   — and mails as written.

4. `preferences.md` — delete the two authorization variants the user did not
   pick, and keep it short: it rides in the packet for every row scored.
   `scoring_rubric.md` ships working defaults every judge reads, so the copy is
   enough. Print the assumed block and move on; step 4 confirms it against real
   rows: **US-wide, no compensation floor, every source on, 4-hour deadline.**

5. `bullets.md` and `skills_master.md` — **draft both yourself** from what the
   resume says. This is the step that decides every generated document.
   - Group into contexts (one per employer, project or degree), pick a short
     `<CTX>` tag, id the bullets `B-<CTX>-NN` zero-padded (`B-WID-01`) —
     `skills_master.md`'s `evidence:` references point at them. Fill `source`,
     `tags` and `evidence` from the resume.
   - **Two `allowable_synonyms` per bullet**: the process name and the artifact
     name. A synonym re-packages the *same* claim — never a wider scope, a bigger
     number, or a more senior verb. `/suggest-synonyms` grows the list from real
     postings later.
   - `skills_master.md`: every tool, language, platform and method the bullets
     evidence, each with an `evidence` reference to a real `B-*` id and up to two
     display aliases (`"Postgres"` for PostgreSQL). A skill with no bullet behind
     it does not go in the file. Leave `vertical_lean` empty — `/tune-vertical`
     tags it once real rows exist.
   - **Review by exception.** Print only the 3-5 items the resume genuinely
     leaves ambiguous, each with the reading you took, then one line: everything
     else was transcribed as written, and `profile/bullets.md` is editable any
     time. No question here — corrections are volunteered.

6. `profile/application_answers.yaml`, only if it exists (it ships behind
   `--with-apply`): **fill it, never interview for it.** `identity`, `education`
   and `employment` from the resume just parsed, with `location` and `country`
   as canonical place names; `work_authorization` from question 1, which the
   loader cross-checks against `preferences.md`'s "## Work authorization"
   section; `rules` as shipped. Show the filled file once. A field the resume
   cannot answer stays empty — `/apply` parks the role rather than inventing an
   answer. Absent, say nothing: step 5's menu adds it and re-runs
   `/onboarding step 1` to fill it from the same resume.

7. One line, no question: `bullets_diction_pass_completed` in
   `profile/de_ai_rules.yaml` exempts confirmed canonical text from Tier 2
   banned-phrase linting. It stays `false` unless the user asks for it — the flag
   only claims the pass happened. It is the one edit here that shows in
   `git status`.

## Step 2 — your lane, drafted from your resume (~3 min, 1 question)

After the scaffold, `verticals.yaml` holds `schema_version`, `default_vertical`
set to the lane, an empty `verticals:`, an empty `classifier_rules:`, and
`out_of_lane.reasoning`. It does not load yet, and `verticals-check` fails.
That is `/new-vertical`'s expected input, not damage to repair.

Run **`/new-vertical <lane>`** and let it drive. Quick mode writes the loader's
minimum — the block, one catch-all classifier rule, `rubric.md`, `tailoring.md`
and `resume_<lane>.md` — and asks one confirm-or-edit. That is question 2. Do not
duplicate any of it here and do not pre-empt its question.

It ends with `verticals-check` passing. If it does not, stop and report.

Skill weights, the title gate, rubric tier boundaries, classifier collisions and
`vertical_lean` tagging are all deliberately absent. They are `/tune-vertical`'s,
after step 3 puts real postings on screen.

## Step 3 — your first scrape and score (~4 min, 0 questions)

The jobs step, and the one network call. Deliberately narrow: **one source, the
lane's first two search terms, half-hour deadline** — three flags on the run,
nothing written to `profile/`. Say what that means before running — Indeed
search only. No LinkedIn (rate-limits hard on a first run), no Greenhouse,
Lever, Ashby or Workday board crawls (those reach only companies already in
`profile/companies.yaml` or `data/universe/*.csv`, and take far longer than this
step has). Expect a fraction of an overnight run.

If libpostal was absent at step 0, skip the scrape and go straight to the
fallback below — the location filter cannot run without it.

`location_allowlist` stays at the shipped `countries: ["United States"]` — the
US-wide default step 1 stated. Nothing here needs undoing later: tomorrow's
plain `uv run discover` runs the full config.

```bash
uv run discover --source indeed --max-terms 2 --deadline-hours 0.5
uv run python -c "
import pandas as pd; d = pd.read_parquet('jobs/clean.parquet')
print(len(d), 'rows'); print(d.title.value_counts().head(20).to_string())"
```

Then `/score`, and read the shortlist it writes.

**If the scrape returns zero rows**, say so rather than continuing into an empty
step 4. One line on the likely cause — the first two search terms do not match how
these boards title the role, the deadline cut the run short, or Indeed rate-limited —
then take the fallback: have them paste two or three job URLs, run `uv run
ingest-url <url> --vertical <lane>` on each, then `/score`. Step 4 runs against
those rows instead. It needs postings, not a scrape.

## Step 4 — react to real postings (~3 min, 2 questions)

Show ~10 scored rows in one table: title, company, location, fit score,
suggested action, and the judge's one-line reason. Then one `AskUserQuestion`
carrying both remaining questions:

- **Question 3** — *"Which of these would you actually apply to?"* Multi-select
  over the ten, plus `None of them`.
- **Question 4** — *"I assumed US-wide, no compensation floor, every source on,
  and a 4-hour deadline. Anything wrong here?"* Options: `Looks right` ·
  `Narrow the locations` · `Set a compensation floor` · `Turn a source off`.

Apply what they said, in one pass, then stop. No second round.

- **Terms.** Drop the terms behind a title family they rejected wholesale; add
  the family the keeps share if the current terms missed it. Step 3 only used
  the first two, so the rest of the list is untested — leave it.
- **Rubric feel.** A row they would apply to that scored low, or a high scorer
  they rejected, is one tier-boundary edit in the lane's `rubric.md`. If it takes
  more than one, that is `/tune-vertical` — say so and leave the rubric alone.
- **Question 4's answer.** Locations and any comp floor go into
  `preferences.md`; locations also into `discovery.yaml`'s `location_allowlist`,
  the hard geographic filter that drops rows in cleaning. Canonical names or
  codes (`states: ["Texas"]` or `["TX"]`, `continents: ["Europe"]`), not every
  spelling a board might use.

```bash
uv run verticals-check
```

Term and rule changes take effect on the next `discover`; `/rescore` re-judges
rows already scored.

## Step 5 — your daily loop (~1 min, 0 questions)

Read the background test run first:

```bash
tail -3 logs/onboarding_tests.log
```

Green — one line, and note that `tests/discovery` was excluded; once libpostal is
installed, `uv run pytest tests -q` covers it. Not green, or still running:
report what failed and do not declare setup done.

```
Every morning        uv run discover      overnight scrape -> jobs/clean.parquet
Then                 /score               judges new rows, writes today's shortlist
Per role you like    /tailor <job_id>     tailored resume
                     /cover-letter <job_id>
Optional             /apply               fills the form; submits only on --submit
End of day           /track <job_id> <state>
Weekly               /standup             regenerates pipeline.md
```

Three things that will otherwise confuse them on day two:

- A thin first shortlist is normal. Only postings whose titles match a classifier
  rule enter the pipeline at all.
- `/track` is the only thing that writes state; every other command appends to
  side lists. Eleven states, of which `rejected, withdrawn, ghosted` and `offer`
  are terminal and reject every out-transition.
- `/apply` never touches Workday, LinkedIn, Indeed or a company careers page.
  Greenhouse, Lever and Ashby only; the rest stay manual.

---

# Later, when you want it

Two lines at hand-off. None is an interview, none blocks the daily loop.

- **`/tune-vertical <lane>`** — the deep pass on the lane: search terms, skill
  weights, the title gate, classifier collisions, disqualifier phrases, rubric
  tiers, `vertical_lean` tags. Run it once a few days of shortlists exist. This
  is the fix when the shortlist stays thin or keeps surfacing the wrong titles.
- **`/suggest-synonyms`** — extends the two synonyms per bullet with phrasings
  taken from real postings. Needs `shortlist/*.md` and `jobs/scored.parquet`.
- **The `/apply` path**, if step 1 skipped it:
  `uv run onboard-scaffold --vertical <lane> --work-auth <status> --with-apply`
  (it skips everything already present), then `/onboarding step 1` to fill
  `application_answers.yaml` from the resume.
- **The PII gate** — `profile/pii_denylist.txt`, which matters once
  `application_answers.yaml` holds a real email and phone.
  `./scripts/pii_scan.sh` checks tracked files. Required before publishing a
  fork; the README's "Before you push" section is the whole procedure.
- **`/outreach`** — hard-refuses without `profile/voice_samples.md`. Paste in
  real messages they actually sent; polished samples sound like nobody.
  `contacts.yaml` is optional alongside it. `--with-optional` copies both, plus
  `companies.yaml` (only for boards they specifically care about —
  `data/universe/*.csv` ships thousands, and `name` must match how boards spell
  it, because it feeds `job_id`) and the denylist.
- **Restyling the two Word templates.** The resume body is rebuilt, but headers,
  footers and text boxes survive onto every resume — keep name and contact out of
  the Word header. Restyle the five named paragraph styles without renaming them,
  keep the `Hyperlink` character style, no tables or images. The cover letter is
  preserved instead of rebuilt: every paragraph that is not a `{{TOKEN}}` ships
  verbatim. Style the copies, never the tracked `.example.docx`;
  `uv run python scripts/scrub_example_templates.py` strips a re-save's metadata.
- **A second lane** — `/new-vertical <name>` again.

## Nightly discovery (macOS only)

Offer this only once the daily loop works. Show `pmset -g sched` first — the
block **replaces** their wake schedule wholesale — then print it for them to run.
Do not run any of it: it writes outside the repo, needs `sudo`, and calls
`launchctl`. `scripts/nightly_discovery.sh` derives the repo from its own
location and needs no editing.

```bash
LABEL=com.$USER.applyourself.discovery
mkdir -p logs ~/Library/LaunchAgents
sed -e "s|__LABEL__|$LABEL|g" -e "s|__REPO__|$PWD|g" \
    scripts/launchagent.example.plist > ~/Library/LaunchAgents/$LABEL.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/$LABEL.plist
sudo pmset repeat wakeorpoweron MTWRFSU 22:25:00
launchctl print gui/$(id -u)/$LABEL | head -20
```

`mkdir -p logs` is not optional — `launchd` will not create the directory for
`StandardOutPath`, and the job then fails to spawn with no log to say why.
Replacing an existing agent: `launchctl bootout gui/$(id -u)/$LABEL` first;
copying the plist alone changes nothing. After the first night, read the newest
`logs/discovery_<timestamp>.log`. Four ways an empty morning happens: asleep at
22:30 with no wake scheduled (a job whose time falls during sleep is skipped,
not deferred); idle sleep took the Mac back down before 22:30 (move the wake to
22:29 if `pmset sleep` is under 5 minutes — `caffeinate` is inside the job); shut
down or on battery (`wakeorpoweron` boots to the login window, where the `gui/`
domain is not loaded); or `sudo pmset repeat` replaced an existing schedule.

---

# Progress file

Write `profile/.onboarding.md` after every step. Gitignored. Judgment calls only
— anything inferable from the filesystem belongs to step 0a's audit, which is the
authority when the two disagree.

```markdown
# onboarding progress

step_completed: 3               # 0-5; 5 means setup is done
notes:
  - lane: revenue_ops; contexts WID (Widget Corp), SPR (side project), EDU (degree)
  - time-limited authorization; sponsorship_rules reconciled by the scaffold
  - application_answers.yaml not installed (no --with-apply)
  - deferred: /tune-vertical, voice samples, nightly launchd
```
