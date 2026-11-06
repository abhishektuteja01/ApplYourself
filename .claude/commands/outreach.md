---
description: Generate an outreach draft (recruiter / referral / alumni) for a specific job_id. Per-channel work-authorization disclosure; `uv run app build outreach` checks the length per channel and lints with the outreach-scope rules. HARD-REFUSES if profile/voice_samples.md is missing or empty.
model: sonnet
effort: medium
allowed-tools:
  - Bash
  - Read
  - Write
argument-hint: <job_id> <channel> [--to "..."] [--via "..."]
---

# /outreach — draft message in the user's voice

Three channels: `recruiter`, `referral`, `alumni`.
**NO cold-DM hiring managers.** NO drip campaigns. NO auto-send.
Draft only -- the user sends.

---

## Step 1 — HARD-REFUSE prereqs (no silent generic-voice fallback)

```bash
cd "$(git rev-parse --show-toplevel)" || { echo "ERROR: not inside the repo."; exit 1; }
test -s profile/voice_samples.md || {
    cat <<'ERR'
ERROR: profile/voice_samples.md is missing or empty.
/outreach must not silently fall back to a generic voice.

Fix: author 2-4 real messages you've written previously (PII redacted),
labeled by channel. The file is gitignored.
ERR
    exit 1
}

JOB_ID="$1"
CHANNEL="$2"
test -n "$JOB_ID"  || { echo "ERROR: /outreach requires <job_id> as the first arg."; exit 1; }
test -n "$CHANNEL" || { echo "ERROR: /outreach requires <channel> as the second arg."; exit 1; }

case "$CHANNEL" in
    recruiter|referral|alumni) ;;
    *) echo "ERROR: channel must be recruiter | referral | alumni (got $CHANNEL)"; exit 1 ;;
esac

# --to is required for referral/alumni: both address a named person.
case "$CHANNEL" in
    referral|alumni)
        ARGS=$(cat <<'EOF'
$ARGUMENTS
EOF
)
        case "$ARGS" in
            *--to*) ;;
            *) echo "ERROR: --to \"<name>\" is required for channel '$CHANNEL' — a $CHANNEL message is addressed to a person."; exit 1 ;;
        esac
        ;;
esac

test -f "pipeline/${JOB_ID}/state.yaml" || {
    echo "ERROR: pipeline/${JOB_ID}/state.yaml missing -- run /track ${JOB_ID} saved first."
    exit 1
}
test -f profile/application_answers.yaml || { echo "ERROR: profile/application_answers.yaml missing -- run /onboarding."; exit 1; }
test -f jobs/clean.parquet  || { echo "ERROR: jobs/clean.parquet missing";  exit 1; }
test -f jobs/scored.parquet || { echo "ERROR: jobs/scored.parquet missing"; exit 1; }
```

## Step 2 — parse `--to` / `--via` and load context

Parse `$ARGUMENTS` for:
- `--to "<name>"` -- recipient name (required for referral / alumni;
  optional for recruiter on LinkedIn DM if no specific recruiter known)
- `--via "<context>"` -- why you're reaching out (alumni connection,
  shared past project, mutual contact, etc.). Shapes the lead-in.
  The one exception is the exact value `--via "email"`, which selects the
  transport instead; it is not a reason and does not shape the lead-in.

Read:
- `pipeline/${JOB_ID}/state.yaml` (company, title, existing outreach[])
- `uv run score show ${JOB_ID}` (fit_score, sponsorship, reasoning, keywords) and the
  `jobs/clean.parquet` row (full JD body)
- `<dir>/trace.md`, where `<dir>` is what `uv run app dir <job_id>` prints (so
  the outreach knows which bullets your resume leads with). An empty `dir`
  means no resume is built yet: stop and tell the user to run `/tailor <job_id>`.
- `profile/contacts.yaml` (schema in `profile/contacts.example.yaml`; PII;
  gitignored). If the file doesn't exist, treat as empty and proceed. Select
  entries whose `company` matches the row's company OR whose `tags` overlap the
  row's vertical/domain. From the selected entry use: `id` (recorded as
  `contact_id`), `name`, `role`, `relationship` (which channel rubric applies),
  `channel` (`email` overrides the LinkedIn-DM default), `email`/`linkedin`,
  `shared_affiliation` (what an alumni message leads with), and `notes` (shapes
  the opening; never quoted into the draft).
- `profile/voice_samples.md` -- the voice baseline (NOT templates; fresh
  generation in the user's voice)
- `de_ai_rules.yaml` (`profile/`, else `data/defaults/`) -- every `rules:` entry whose `scope` holds `outreach`
- the work-authorization row for the job's country, which sets the disclosure:

  ```bash
  cd "$(git rev-parse --show-toplevel)" && uv run config work-auth '<the country in state.yaml location>'
  # Leave the country out for a remote or unnamed one: the home country applies.
  ```

### Disclosure

- `requires_sponsorship: false` -> the draft never mentions work
  authorization, on any channel.
- `requires_sponsorship: true` -> the channel below decides whether it
  appears. When it does, it is one light, honest line worded from the row's
  `status_label` and `followup`, and it says sponsorship is needed now when
  `authorized` is false. NO-FAB: no status fact the row does not state.

## Step 3 — generate the draft per channel rubric (LOCKED)

Word limits are `outreach_word_limits` in `profile/settings.yaml`; the build
checks them, so write within the one named for the channel.

### recruiter

LinkedIn DM:
- within `recruiter_linkedin` words, NO subject line
- Disclosure: **only if asked**.

Email:
- within `recruiter_email` words, a short subject line (within `subject` words)
- Disclosure: **only if asked**

Pick the medium by the first rule that applies:
1. `--via "email"` was passed -> Email.
2. The `profile/contacts.yaml` entry for this recipient has
   `channel: email` -> Email.
3. Otherwise -> LinkedIn DM.

That is the whole rule. Reachability is not a judgment call to make here.

### referral

- within `referral` words
- Warm contact you know; asking them to refer you internally
- **Disclosure: upfront**, so the contact can check internally before
  spending social capital
- Direct ask: "would you be open to forwarding my resume to <X>?"

### alumni

- within `alumni` words
- **LEAD with the shared affiliation** -- a school or employer the recipient
  and the user have in common, taken from the entry's `shared_affiliation` in
  `profile/contacts.yaml`, the `--via` context, or an employer or school in
  `profile/resume.yaml`
- **Ask for a 15-minute call -- NOT a referral up front**
- **Disclosure: upfront**

### Write `outreach.yaml`

Write `<dir>/outreach.yaml` (`<dir>` from `uv run app dir <job_id>`).
`uv run app schema outreach` prints every key it may hold:

```yaml
channel: <the $CHANNEL arg>
medium: linkedin | email
to: <recipient name, or null for a recruiter with no name>
contact_id: <id from contacts.yaml, or null>
subject: <email only; leave it out for a LinkedIn DM>
uses: [<bullet ids whose facts the message states>]
body: <the message, in the user's voice per voice_samples.md>
why: <2-3 sentences: which voice cue you mirrored, which JD signal you
  referenced, why this disclosure choice for this channel>
```

NO-FAB: every number in `subject` or `body` must be in a `uses` bullet, a context
header or the JD; the build refuses one that is not.

## Step 4 — build the draft

```bash
cd "$(git rev-parse --show-toplevel)" && uv run app build outreach <job_id>
```

It fixes mechanical slips, then checks the shape, the word limits (`length`),
numbers (`number`), cited ids (`unknown_id`) and every `rules:` entry in
`de_ai_rules.yaml` whose `scope` holds `outreach` (`lint`). On exit 2,
rewrite what each problem names in `outreach.yaml`, in the user's voice and
matched by no rule, and build again per the fix loop in
`.claude/shared/no_fab.md` (read it: it holds the attempt cap). If still failing
after 5: refuse with the specific line and the problem that won't clean, and tell
the user to either tweak the draft by hand or expand `voice_samples.md` with an
alternate phrasing. `warnings` never fail the build; mention them in the report.

**Never silently let a banned phrase ship.**

On exit 0 the build has written the draft to `pipeline/<job_id>/outreach/` (the
path is `draft` in its JSON) and recorded it in `state.yaml`'s `outreach[]`
through `uv run track outreach-draft`. A rebuild rewrites the same day's draft
and never records it twice.

## Step 5 — disclosure check

- [ ] If the row has `requires_sponsorship: false`, the draft never mentions
      work authorization
- [ ] Otherwise: a `recruiter` draft does NOT volunteer it; a `referral` or
      `alumni` draft DOES surface it lightly and upfront

If any assertion fails, do NOT report success.

## Step 6 — report

Tell the user, filling every `<...>` from what the build printed:
```
Outreach drafted: <draft>
  - channel: <channel>   to: <recipient name>   medium: <linkedin | email>
  - word count: <words> (channel limit: <limit>)
  - lint: 0 violations<, warnings: ... when any>
  - "Why this approach" footer is at the bottom — DELETE before sending.

Send manually, then:
  /track outreach-sent <job_id> --channel <channel> --to "<recipient name>"
```
