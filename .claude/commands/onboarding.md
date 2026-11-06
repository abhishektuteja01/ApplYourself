---
description: Set up your own copy of the pipeline in about 21 minutes — eight questions instead of 33 decisions, real scored jobs on screen by minute 17. Resumable: re-run to continue where you left off. Also runs as a setup audit on an existing install.
model: opus
effort: high
disable-model-invocation: true
allowed-tools:
  - Bash(cd *)
  - Bash(git rev-parse *)
  - Bash(echo *)
  - Bash(cat *)
  - Bash(test *)
  - Bash(tail *)
  - Bash(mkdir -p *)
  - Bash(uv --version)
  - Bash(uv sync*)
  - Bash(git config core.hooksPath .githooks)
  - Bash(uv run python -c *)
  - Bash(uv run verticals-check*)
  - Bash(uv run onboard-scaffold *)
  - Bash(uv run profile-extract *)
  - Bash(uv run discover-digest*)
  - Bash(uv run ingest-url *)
  - Bash(uv run score *)
  - Bash(nohup uv run pytest tests *)
  - Bash(nohup uv run discover *)
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - Agent
  - AskUserQuestion
argument-hint: "[audit | step <n>]"
---

# /onboarding — set up your own copy

Five steps, ~21 minutes, eight questions instead of 33 decisions, real scored
postings on screen at step 4. The scrape starts at the end of step 1 and runs through step 2; scoring
starts at the top of step 3 and runs through the review questions. Step 0 is
bootstrap and carries no number the user sees. Everything written is user data
under `profile/`; nothing tracked changes.

## Contract (binding)

- **Eight questions, total.** Step 1: five — the roles (plain text), work
  authorization, and `/new-vertical pass-a`'s experience cap, level band and
  confirm-or-edit batched into one `AskUserQuestion`. Step 3: two, batched into
  one `AskUserQuestion`. Step 4: one. If a step seems to need a ninth, you
  inferred too little.
- **You do the work; the user gives hints and a verdict.** Draft the file, show
  it once, let them strike or reword. Never walk a user through a file field by
  field.
- **Show, don't ask.** Filled files, assumed defaults and the by-exception
  ambiguity list are printed for correction, not interviewed. Never apply
  unconfirmed text; if the user edits a draft, re-show it before applying.
- **NO-FAB over everything drafted from the resume** — read
  `.claude/shared/no_fab.md`. Nothing enters `resume.yaml`, `bullets.md`,
  `skills_master.md` or `application_answers.yaml` that the resume or the answers support.
- **Never run `sudo`, `launchctl`, `git commit` or `git push`.** Print them.
- Never edit `src/`, `tests/` or `.claude/`, and neither does anything you
  delegate to. If a step seems to need it, stop and report — the config
  contract is wrong, not the setup.
- You run only what a numbered step lists: the bootstrap in 0b, the scaffold in
  1.3, `discover` in 1.5, scoring in 3.4 and 3.6, `verticals-check` in 4.
  Everything downstream is handed off.
- Print the position line first in every numbered step, verbatim:
  - `Step 1 of 5 · ~18 min left · you'll see real scored jobs at step 4.`
  - `Step 2 of 5 · ~14 min left · your first scrape is running behind this.`
  - `Step 3 of 5 · ~7 min left · scoring starts here.`
  - `Step 4 of 5 · ~4 min left · real postings, one question.`
  - `Step 5 of 5 · done · your daily loop.`
- Write `profile/.onboarding.md` after each step (schema at the bottom).
  Re-running resumes at the first incomplete step. `$ARGUMENTS` of `step <n>`
  jumps there; `audit` runs step 0a and stops.

## Step 0 — bootstrap (unnumbered, ~3 min, 0 questions)

### 0a. Audit — no writes. This is also all `audit` mode runs.

```bash
echo "--- toolchain ---"
# The pin, not `python3 --version`: uv builds the venv from .python-version, so
# the system interpreter is irrelevant and reporting it hides a mismatch.
echo "pinned python: $(cat .python-version 2>/dev/null || echo '.python-version MISSING')"
uv --version || echo "MISSING: uv"
echo "--- required (pipeline cannot run without these) ---"
for f in profile/verticals.yaml profile/resume.yaml \
         profile/bullets.md profile/skills_master.md \
         profile/resume_template.docx profile/cover_letter_template.docx; do
  test -s "$f" && echo "ok   $f" || echo "MISSING $f"; done
echo "--- optional (later menu) ---"
for f in profile/discovery.yaml profile/application_answers.yaml \
         profile/voice_samples.md profile/contacts.yaml profile/pii_denylist.txt; do
  test -s "$f" && echo "ok   $f" || echo "absent  $f"; done
# Probe the Python binding, not the C library: both ingestion paths import
# `postal.parser`, and a system libpostal with no binding installed still dies
# at run time.
if uv run python -c "import postal.parser" >/dev/null 2>&1
then echo "ok   libpostal binding"
else echo "absent  libpostal binding (step 1's scrape needs it)"; fi
echo "--- validity ---"
uv run verticals-check 2>&1 | tail -5
echo "--- progress ---"
test -f profile/.onboarding.md && cat profile/.onboarding.md || echo "no progress file"
```

Map each result to the step that fixes it, in a short checklist, and start
there. If everything is present and `verticals-check` passes, report that and
stop — that is the audit. `audit` mode always stops here.

**libpostal is a hard prerequisite for the scrape at the end of step 1.** The
location filter imports the `postal` binding (opt-in group
`discovery`), compiled against the system `libpostal` C library. No fallback
parser, and no way around it: `uv run discover` and `uv run ingest-url` both
rebuild `clean.parquet` through it. Steps 2, 4 and 5 do not need it, and step 1 up to the scrape
does not either.

If the probe says absent, say so now and let 0b's `uv sync --group discovery`
try. The wheel builds only against a system libpostal that is already there, so
if that sync fails to compile, print these and have the user run the install in
another terminal while step 1's questions continue:

- macOS: `brew install libpostal`, then `uv sync --group discovery`
- Linux: build from source (`github.com/openvenues/libpostal`) — no distro
  packages it; install its dev headers, expect a ~2 GB data download. Then
  `uv sync --group discovery`.

### 0b. Install, then one background test run

```bash
uv sync
uv run python -c "import postal.parser" >/dev/null 2>&1 || uv sync --group discovery
git config core.hooksPath .githooks
mkdir -p logs
nohup uv run pytest tests -q --ignore=tests/discovery \
      --ignore=tests/repo/test_real_config_drift.py > logs/onboarding_tests.log 2>&1 &
```

If the second line fails to compile, that is the C library missing — print the
install block from 0a and carry on; step 1.5 is where it becomes blocking.

`test_real_config_drift.py` is excluded because step 1.3 leaves `verticals.yaml`
deliberately non-loading, and the lane is only complete once step 2's
`/new-vertical <lane> pass-b` writes the three prose files; that pass runs the
full check itself.

That is the only test run in this command. Do not wait for it and do not start a
second one — step 5 reads the log at hand-off.

`onboard-scaffold` does every remaining chore, but two of its flags are answers
you do not have yet (`--vertical`, `--work-auth`), so it runs inside step 1.

## Step 1 — your lane, and the scrape starts (~4 min, 5 questions)

Nothing here reads the resume. The whole point of this order is that the scrape
only needs the lane's `search_terms`, so it can be running while step 2 reads
the resume.

1. **Question 1**, plain text, no `AskUserQuestion`: *"What roles are you going
   for? A title or two is enough."* Keep their wording verbatim — it is the
   description you hand `/new-vertical` in 1.4.

2. **Question 2**, one `AskUserQuestion`: *"What's your work authorization?"*
   - `Citizen or permanent resident`
   - `Need sponsorship now`
   - `Authorized now, time-limited` (F-1 OPT, STEM OPT, any visa with an end date)

   One answer, two places — the scaffold flag and `application_answers.yaml`
   (step 2 fills it):

   | answer | `--work-auth` | `application_answers.yaml` home-country row |
   |---|---|---|
   | citizen or PR | `citizen` | `authorized: true, requires_sponsorship: false, unrestricted: true` |
   | needs sponsorship now | `needs_now` | `authorized: false, requires_sponsorship: true` |
   | time-limited | `time_limited` | `authorized: true, requires_sponsorship: true` |

3. Bootstrap, continued — both scaffold inputs now exist. Derive the lane name
   from the stated roles (snake_case, `^[a-z][a-z0-9_]*$`); 1.4 carries it into
   what `/new-vertical` confirms.

   ```bash
   uv run onboard-scaffold --vertical <lane> --work-auth <citizen|needs_now|time_limited> --dry-run
   uv run onboard-scaffold --vertical <lane> --work-auth <citizen|needs_now|time_limited>
   ```

   Copies every `profile/*.example.*` to its real name including the two Word
   templates, empties `lanes:` and the example classifier rules in `verticals.yaml`, sets your home
   country's row in `application_answers.yaml`'s `work_authorization.countries`. Never overwrites — an existing file is skipped and
   listed. Read the report; `SKIPPED` and `FAILED` are the only parts that need
   you. `--with-apply` and `--with-optional` are step 5's menu, not this path.

   Both Word templates work as copied, with one thing to fix before the first
   `/cover-letter`: the letter's letterhead is literal placeholder text — `NAME`
   at the top, `City, ST | Num| Email` under it, `NAME` again after `Sincerely,`
   — and mails as written.
   No Microsoft Word on this Mac: tell them every `uv run app build` needs `--no-pdf`.

4. Run **`/new-vertical <lane> pass-a "<their stated roles>"`** and let it drive.
   Pass A writes only the lane's `profile/verticals/<lane>/lane.yaml`, its `lanes:` entry and one classifier rule,
   and asks the experience cap and the target level — **questions 3 and 4** —
   plus the confirm-or-edit — **Question 5** — in one `AskUserQuestion`. Do not
   duplicate any of it here and do not pre-empt them.

   The level answer becomes the lane's `# Target level:` line, which pass B
   turns into the rubric's seniority tiers. No title is dropped for its
   level; the title gate drops only the `defaults:` ban list.

   Pass A ends with the loader working and a marker at
   `profile/verticals/<lane>/.pass_a_only`. `verticals-check` fails until step 2
   runs pass B; that failure is expected here, not damage to repair.

   If that marker is already on disk when you reach 1.4, a previous session got
   this far: skip pass A, leave questions 3-5 unasked, and go to 1.5.

5. **Start the scrape and move on. Do not wait for it.** LinkedIn, the lane's
   first two terms, six-minute deadline, backgrounded into a log step 3 reads.

   ```bash
   mkdir -p logs jobs
   uv run python -c "import postal.parser" || echo "BLOCKED: libpostal binding missing"
   nohup uv run discover --source linkedin --max-terms 2 --deadline-hours 0.1 \
         > logs/onboarding_scrape.log 2>&1 &
   ```

   **libpostal is blocking here**, not later: the location filter runs on every
   ingestion path, so the scrape cannot start without the binding. If it is missing, skip
   the `nohup` line, say so in one line, print the install block from 0a, and
   continue to step 2 — step 3 picks it up through the paste-URL fallback. Never
   dead-end the user on it.

   Say what the scrape is before starting it: LinkedIn search only, two terms.
   No Indeed unless the fallback fires, no Greenhouse, Lever, Ashby or Workday
   board crawls (those reach only boards listed in `data/universe/boards.csv`,
   and take far longer than this has). Expect a
   fraction of an overnight run. The view accepts the answer bank's countries
   (only your own, until you add rows); nothing here needs undoing, because tomorrow's
   plain `uv run discover` runs the full config.

## Step 2 — your resume (~7 min, 0 questions)

The scrape is running behind this step. Ask nothing; everything drafted here is
shown, and the one thing that needs a verdict is folded into step 3's questions.

1. Ask them to **drop their current resume into `profile/` and say when it is
   there** — no path to type, and `profile/*` is gitignored. Then find it: glob
   `profile/` for `.docx`, `.pdf` and `.md` files that are neither a known
   profile file nor a `*.example.*` template. One match, take it; several, name
   them and ask which; none, say what you looked for and ask again.
   - `.docx` → `uv run profile-extract <file>`
   - `.pdf` or `.md` → read it directly

2. Print the assumed discovery block and move on; step 3 confirms it against real
   rows: **US-wide, every source on, and a 4-hour limit on each nightly run.**
   Step 1's first scrape is capped much shorter so it finishes while they wait.

3. `resume.yaml`, `bullets.md` and `skills_master.md` — **draft all three
   yourself** from what the resume says. This is the step that decides every
   generated document.
   - `resume.yaml`: name, contact line and education, copied as the resume
     states them, plus one `contexts:` entry per employer or project (its
     `section`, `title`, `org`, `location`, `dates`). Leave each context's
     `lanes` empty — `/new-vertical pass-b` sets the budgets.
   - Id the bullets `B-<CTX>-NN` zero-padded (`B-WID-01`), where `<CTX>` is the
     context's key in `resume.yaml`. `skills_master.md`'s `evidence:`
     references point at them. Fill `tags` and `evidence` from the resume.
   - **Every useful synonym per bullet**, lane-aware: draft the full set of
     phrasings a posting in this lane could plausibly use for that same claim —
     the process name, the artifact name, the tool-flavored name, the
     industry-standard name. NO-FAB is unchanged: a synonym re-packages the
     *same* claim, never a wider scope, a bigger number or a more senior verb.
     `/suggest-synonyms` in step 4 is a top-up against real postings, not the
     source.
   - `skills_master.md`: every tool, language, platform and method the bullets
     evidence, each with an `evidence` reference to a real `B-*` id, the same
     maximal `allowable_synonyms` treatment, and up to two display aliases
     (`"Postgres"` for PostgreSQL). A skill with no bullet behind it does not go
     in the file. Leave `show` empty — `/new-vertical pass-b` places each skill
     on the lane's lines.
   - **Review by exception.** Print only the 3-5 items the resume genuinely
     leaves ambiguous, each with the reading you took, then one line: everything
     else was transcribed as written, and `profile/bullets.md` is editable any
     time. Carry that list into step 3 — it is one of that step's two questions.

4. `profile/application_answers.yaml`, which the scaffold always copies:
   **fill it, never interview for it.** `identity`, `education` and `employment`
   read every key they leave out from `resume.yaml`, so set only what the resume
   lacks or a form words differently: `identity.country` (and `location`) as
   canonical place names, a phone with its country code, a board-style `degree`
   ("Master's Degree"); `work_authorization.countries` from step 1's question 2 (your home
   country's row as in the table above, `default` for everywhere else, a
   citizen's row for each nationality); `claims`, `questions` and `fact_wordings`
   as shipped — they grow with each confirmed `/apply`. Show the filled file once. A field the resume cannot
   answer stays empty — `/apply` parks the role rather than inventing an answer.

5. Run **`/new-vertical <lane> pass-b`**. It writes `rubric.md`, `tailoring.md`
   and the lane's resume shape from the bullets just authored, removes the
   `.pass_a_only` marker, and ends with `verticals-check` passing. It asks
   nothing. If the check does not pass, stop and report.

   Skill weights, rubric tier boundaries, classifier collisions stay out of
   both passes; they are `/tune-vertical`'s.

## Step 3 — scoring starts (~3 min, 2 questions)

In this order. The score judges must be spawned before the questions, so the
user answers them while it runs.

### 3.1 Read the scrape log

```bash
tail -20 logs/onboarding_scrape.log
```

`clean rows: 0`, or the `linkedin` lane zero or crashed in
`uv run discover-digest` → re-run on
Indeed, backgrounded the same way, and carry on with 3.2 while it runs:

```bash
nohup uv run discover --source indeed --max-terms 2 --deadline-hours 0.1 \
      > logs/onboarding_scrape_indeed.log 2>&1 &
```

Still zero after that, or libpostal never arrived: say so in one line with the
likely cause — the first two search terms do not match how these boards title
the role, the deadline cut the run short, or both boards rate-limited — then take
the fallback. Have them paste two or three job URLs and run
`uv run ingest-url <url> --vertical <lane>` on each. Step 4 needs postings, not a
scrape.

```bash
uv run python -c "
import pandas as pd; d = pd.read_parquet('jobs/clean.parquet')
print(len(d), 'rows'); print(d.title.value_counts().head(20).to_string())"
```

### 3.2 The title gate

Nothing to draft. A title reaches the lane when it fits the lane's classifier
rule or one of its search terms (`src/applyourself/shared/title_gate.py:lane_for`),
and the gate drops only the `defaults:` ban list. Leave the lane's title lists
absent; strays are the judge's to score down.

### 3.3 What the ban list drops

Show what the gate dropped for this lane before 3.5 asks the user to confirm.
The nightly debug shards keep every classified listing with its drop `reason`.

```bash
uv run python -c "
import glob, pandas as pd
LANE = '<lane>'
d = pd.concat(pd.read_parquet(f) for f in glob.glob('jobs/raw/*.parquet'))
d = d[d.vertical == LANE]
hit = d.get('reason', pd.Series('', index=d.index)).fillna('').str.startswith('banned')
print(f'{int(hit.sum())}/{len(d)} rows dropped by the ban list')
print('DROPPED:'); print(d.loc[hit, 'title'].value_counts().head(15).to_string())"
```

A ban word removed brings rows back as their boards and searches are next
checked: the store keeps only rows that passed the gate.

### 3.4 Start scoring, judges in the background

Follow `/score`'s steps from this session (a subagent cannot spawn judges):

1. `uv run score start`. Exit 1: report the error and skip to 3.5. A `done`
   line: nothing to judge.
2. For each `judge ...` line, spawn one background `score-judge` agent whose
   prompt is the rest of that line. All in one message.

Never judge a slice yourself, and never read, edit or delete files in
`jobs/scored.staging/`.

### 3.5 The two review questions, one `AskUserQuestion`

- **Question 6** — *"A few things in your resume I had to interpret — did I get
  these right?"* The 3-5 genuinely ambiguous items from step 2.3, each with the
  reading you took. Nothing else goes in this list.
- **Question 7** — *"I guessed anywhere in the US, every job board on, and up to
  4 hours per nightly run. You capped jobs at N years, the judge scores titles
  outside <band> down, and the ban list dropped M of the rows scraped.
  Anything wrong?"*
  Options: `Looks right` · `Narrow the locations` · `Turn a source off` ·
  `Change the level or years cap`.

### 3.6 Apply the answers, then finish scoring

- A source they turn off is `enabled: false` under that source in
  `discovery.yaml`'s `sources` block, and nowhere else.
- A country is one row in the answer bank's `work_authorization.countries`
  (its work-authorization facts), which is also the hard geographic filter on
  `clean.parquet`. Country names only, not every spelling a board might use.
  `discovery.yaml`'s `location_allowlist.countries` only narrows it.
- A new years cap is the lane's `years` table; a new level band is its
  `# Target level:` line and the rubric's seniority tiers.
- Ambiguity corrections go straight into `bullets.md` / `skills_master.md`.

Then wait on the judges and run `/score`'s step 3: `uv run score finish`; on
exit 1 spawn its `judge ...` lines once the same way and run `finish` again;
still exit 1, report its `bad` lines. Then read the shortlist it wrote.

## Step 4 — react to real postings (~3 min, 1 question)

Show ~10 scored rows in one table: title, company, location, fit score,
suggested action, and the judge's one-line reason. Then one `AskUserQuestion`:

- **Question 8** — *"Which of these would you actually apply to?"* Multi-select
  over the ten, plus `None of them`.

Apply what they said, in one pass, then stop. No second round.

- **Terms.** Drop the terms behind a title family they rejected wholesale; add
  the family the keeps share if the current terms missed it. Step 1 only used
  the first two, so the rest of the list is untested — leave it.
- **Rubric feel.** A row they would apply to that scored low, or a high scorer
  they rejected, is one tier-boundary edit in the lane's `rubric.md`. If it takes
  more than one, that is `/tune-vertical` — say so and leave the rubric alone.

```bash
uv run verticals-check
```

If a rubric boundary or the `years` table changed, run `/score --all` and
show the new top rows — otherwise their edit has no visible effect. Term changes need a fresh
scrape, so those only say "next run".

Then run **`/suggest-synonyms`**. The `shortlist/*.md` and `jobs/scored.parquet`
it needs exist for the first time here. It writes a draft file and nothing else;
hand the user the path and move on rather than working through it now.

## Step 5 — your daily loop (~1 min, 0 questions)

Read the background test run first:

```bash
tail -3 logs/onboarding_tests.log
```

Green — one line, and note that `tests/discovery` and
`tests/repo/test_real_config_drift.py` were excluded from that run; `uv run pytest
tests -q` now covers both, since libpostal is installed and the lane is complete.
Not green, or still running: report what failed and do not declare setup done.

```
Every morning        uv run discover      overnight scrape -> jobs/clean.parquet
Then                 /score               judges new rows, writes today's shortlist
Per role you like    /tailor <job_id>     tailored resume
                     /cover-letter <job_id>
Optional             /apply <job_id> [--submit]   fills; submits only on --submit
End of day           /track <job_id> <state>
Weekly               /standup             regenerates pipeline.md
```

Three things that will otherwise confuse them on day two:

- A thin first shortlist is normal. Only postings whose titles match a classifier
  rule enter the pipeline at all.
- `/track` is the only thing that writes state; every other command appends to
  side lists. Nine states, of which `rejected, withdrawn` and `offer` are
  terminal; a move off the allowed list needs `--force --note`. An `applied`
  role with no update for `ghosted_after_days` shows as `ghosted`.
- `/apply` never touches Workday, LinkedIn, Indeed or a company careers page.
  Greenhouse, Lever and Ashby only; the rest stay manual.

Then read `.claude/shared/onboarding_later.md` and give its "Later" menu. Offer
its nightly pipeline only once the daily loop works.

---

# Progress file

Write `profile/.onboarding.md` after every step. Gitignored. Judgment calls only
— anything inferable from the filesystem belongs to step 0a's audit, which is the
authority when the two disagree.

```markdown
# onboarding progress

step_completed: 3               # 0-5; 5 means setup is done
notes:
  - lane: revenue_ops; contexts WID (Widget Corp), SPR (side project)
  - time-limited authorization; the home work-auth row set by the scaffold
  - application_answers.yaml filled; apply browser not installed
  - deferred: /tune-vertical, voice samples, nightly launchd
```
