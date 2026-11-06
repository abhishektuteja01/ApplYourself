# /onboarding — after setup

Read by `/onboarding` at step 5's hand-off. The contract still holds: never run
`sudo`, `launchctl`, `git commit` or `git push`; print them.

## Later, when you want it

Two lines at hand-off. None is an interview, none blocks the daily loop.

- **`/tune-vertical <lane>`** — the deep pass on the lane: search terms, skill
  weights, the title gate, classifier collisions, `drop_phrases`, rubric
  tiers, the resume shape. Run it once a few days of shortlists exist. This
  is the fix when the shortlist stays thin or keeps surfacing the wrong titles.
- **`/suggest-synonyms`** again — step 4 ran it once, against one day of
  postings. It gets better as `shortlist/*.md` accumulates.
- **The `/apply` path** — `application_answers.yaml` is already filled; what is
  missing is the browser:
  `uv run onboard-scaffold --vertical <lane> --work-auth <status> --with-apply`
  installs the apply dependency group and Playwright's Chrome, and skips
  everything already present.
- **The PII gate** — `profile/pii_denylist.txt`, which matters once
  `application_answers.yaml` holds a real email and phone.
  `./scripts/pii_scan.sh` checks tracked files. Required before publishing a
  fork; the README's "Before you push" section is the whole procedure.
- **`/outreach`** — hard-refuses without `profile/voice_samples.md`. Paste in
  real messages they actually sent; polished samples sound like nobody.
  `contacts.yaml` is optional alongside it. `--with-optional` copies both, plus
  the denylist. A board they care about that `data/universe/boards.csv` lacks
  goes in that file as `ats,name,slug` (`name` must match how boards
  spell it, because it feeds `job_id`).
- **Restyling the two Word templates.** The resume body is rebuilt, but headers,
  footers and text boxes survive onto every resume — keep name and contact out of
  the Word header. Restyle the five named paragraph styles without renaming them,
  keep the `Hyperlink` character style, no tables or images. The cover letter is
  preserved instead of rebuilt: every paragraph that is not a `{{TOKEN}}` ships
  verbatim. Style the copies, never the tracked `.example.docx`;
  `uv run python scripts/dev/scrub_example_templates.py` strips a re-save's metadata.
- **A second lane** — `/new-vertical <name>` again, with no mode token: both
  passes run at once, because the bullets already exist.

## Nightly pipeline (macOS only)

Offer this only once the daily loop works. The agent runs
`scripts/nightly_pipeline.sh`: discover, score, synonyms, tailor, apply prep
(never submits), then `summary.md`. It runs headless `claude` sessions, so it
spends their usage. It needs `profile/settings.yaml`: copy
`profile/settings.example.yaml`, then set or delete `summary_email` and set
the per-night caps. Show `pmset -g sched` first — the block **replaces** their wake
schedule wholesale — then print it for them to run. Do not run any of it: it
writes outside the repo, needs `sudo`, and calls `launchctl`. The script derives
the repo from its own location and needs no editing; it deletes logs and run
folders older than `log_keep_days` in `profile/discovery.yaml`.

```bash
LABEL=com.$USER.applyourself.discovery
mkdir -p logs ~/Library/LaunchAgents
sed -e "s|__LABEL__|$LABEL|g" -e "s|__REPO__|$PWD|g" \
    scripts/launchagent.example.plist > ~/Library/LaunchAgents/$LABEL.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/$LABEL.plist
sudo pmset repeat wakeorpoweron MTWRFSU 19:55:00
launchctl print gui/$(id -u)/$LABEL | head -20
```

`mkdir -p logs` is not optional — `launchd` will not create the directory for
`StandardOutPath`, and the job then fails to spawn with no log to say why.
Replacing an existing agent (including one that still runs
`nightly_discovery.sh`, a script that no longer exists): `launchctl bootout
gui/$(id -u)/$LABEL` first; copying the plist alone changes nothing. After the
first night, read `summary.md` and the newest `logs/pipeline_<timestamp>/`.
Four ways an empty morning happens: asleep at 20:00 with no wake scheduled (a
job whose time falls during sleep is skipped, not deferred); idle sleep took the Mac back down before 20:00 (move the wake to
19:59 if `pmset sleep` is under 5 minutes — `caffeinate` is inside the job); shut
down or on battery (`wakeorpoweron` boots to the login window, where the `gui/`
domain is not loaded); or `sudo pmset repeat` replaced an existing schedule.
