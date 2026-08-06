---
description: Generate an outreach draft (recruiter / referral / alumni) for a specific job_id. Per-channel OPT disclosure and length defaults. Fully linted with outreach_only banned phrases. HARD-REFUSES if profile/voice_samples.md is missing or empty.
model: sonnet
effort: medium
allowed-tools:
  - Bash
  - Read
  - Write
argument-hint: <job_id> <channel> [--to "..."] [--via "..."]
---

# /outreach — draft message in the user's voice

Three channels in v1: `recruiter`, `referral`, `alumni`.
**NO cold-DM hiring managers.** NO drip campaigns. NO auto-send.
Draft only -- the user sends.

---

## Step 1 — HARD-REFUSE prereqs (no silent generic-voice fallback)

```bash
# Every path below is repo-relative, and src/ resolves its own paths from
# the repo root — so anchor the shell there too.
cd "$(git rev-parse --show-toplevel)" || { echo "ERROR: not inside the repo."; exit 1; }
# voice_samples.md is the load-bearing precondition. If it's missing or
# empty, the model would otherwise fabricate a "generic voice", which
# is explicitly forbidden.
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

# --to is REQUIRED for referral/alumni: both address a named person. Without
# this guard the draft goes to nobody, lands at ..._unknown.md, and carries a
# literal to_name='<recipient name>'.
case "$CHANNEL" in
    referral|alumni)
        case "$*" in
            *--to*) ;;
            *) echo "ERROR: --to \"<name>\" is required for channel '$CHANNEL' — a $CHANNEL message is addressed to a person."; exit 1 ;;
        esac
        ;;
esac

test -f "pipeline/${JOB_ID}/state.yaml" || {
    echo "ERROR: pipeline/${JOB_ID}/state.yaml missing -- run /track ${JOB_ID} saved first."
    exit 1
}
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
- `pipeline/${JOB_ID}/state.yaml` (company, title, sponsorship_label,
  fit_score, existing outreach[])
- `jobs/scored.parquet` + `jobs/clean.parquet` row (reasoning, keywords,
  full JD body)
- `applications/<entry>/trace.md` where `<entry>` is `state.yaml.tailored_dirs[]`'s
  last entry (already vertical-prefixed, e.g. `<vertical>/2026-06-17_acme_..._a1b2c3d4`)
  IF that list is non-empty (so the outreach knows which bullets your
  resume leads with)
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
- `profile/de_ai_rules.yaml` -- both `banned_phrases` and `outreach_only`

## Step 3 — generate the draft per channel rubric (LOCKED)

### recruiter

LinkedIn DM:
- **≤ 90 words**, NO subject line
- OPT disclosure: **ONLY if asked**. Cold disclosure kills the funnel.

Email:
- **≤ 150 words**, short subject line (≤ 8 words)
- OPT disclosure: **ONLY if asked**

Pick the medium by the first rule that applies:
1. `--via "email"` was passed -> Email.
2. The `profile/contacts.yaml` entry for this recipient has
   `channel: email` -> Email.
3. Otherwise -> LinkedIn DM.

That is the whole rule. Reachability is not a judgment call to make here.

### referral

- **≤ 120 words**
- Warm contact you know; asking them to refer you internally
- **OPT disclosure: light, honest, upfront mention** so the contact can
  check internally before spending social capital
- Direct ask: "would you be open to forwarding my resume to <X>?"

### alumni

- **≤ 100 words**
- **LEAD with the shared affiliation** -- a school or employer the recipient
  and the user have in common, taken from the entry's `shared_affiliation` in
  `profile/contacts.yaml`, the `--via` context, or a `source:` field in
  `profile/bullets.md`
- **Ask for a 15-minute call -- NOT a referral up front**
- **OPT disclosure: light, honest, upfront mention**

### Output file structure

Write to `pipeline/<job_id>/outreach/<YYYY-MM-DD>_<channel>_<recipient-slug>.md`
— today's date, the `$CHANNEL` arg, and the recipient's name lowercased with
non-alphanumerics collapsed to `-` (`Priya Raman` -> `priya-raman`). Call this
path DRAFT_PATH below; Step 5 records the filename in `state.yaml`:

```markdown
---
job_id: <id>
channel: <c>
to: <recipient name>
contact_id: <id from contacts.yaml or null>
medium: linkedin | email
generated_at: <ISO ts>
status: draft
---

# Subject

<short subject line; OMIT this section entirely for LinkedIn DM>

# Body

<the message, in user's voice per voice_samples.md, within length limit>

# Why this approach

<2-3 sentences: which voice cue you mirrored, which JD signal you
referenced, why this OPT-disclosure choice for this channel.
The user deletes this section before sending.>
```

Filename slug rule for `RECIPIENT_SLUG`: lowercase the `--to` value,
collapse non-alphanumerics to `-`, strip leading/trailing `-`. If no
`--to` was given (rare; recruiter cold-DM), use `unknown`.

## Step 4 — LINT the draft (outreach context)

Outreach is fresh-generation text -- **NO bullets.md exemption applies.**

```bash
uv run python <<'PYEOF'
import json
from pathlib import Path
from src.lint import fix_mechanical, find_phrase_violations, load_de_ai_rules

# Substitute the literal path of the draft written in Step 3 — bash
# variables do not persist between Bash calls.
draft_path = Path("pipeline/<job_id>/outreach/<date>_<channel>_<recipient-slug>.md")
draft = draft_path.read_text()
rules = load_de_ai_rules()
fixed, subs = fix_mechanical(draft, rules)
draft_path.write_text(fixed)
# outreach context: enforces banned_phrases + outreach_only + no_emoji + no_exclamation
violations = find_phrase_violations(fixed, context="outreach", rules=rules)
print(json.dumps({"mech_subs": len(subs), "violations": violations}, indent=2))
PYEOF
```

If any violation:
- Rewrite the offending line using a phrasing that is (a) in the user's
  voice per `voice_samples.md` and (b) not in any banned list, AND not
  in `outreach_only`.
- Re-save and re-lint per `.claude/shared/lint_loop.md` (read it: it holds the
  attempt cap and the hard-refuse rule).
- If still failing after 5: refuse with the specific line + the phrase
  that won't clean, and tell the user to either tweak the draft by hand
  or expand `voice_samples.md` with an alternate phrasing.

**Never silently let a banned phrase ship.**

## Step 5 — register the draft in state.yaml.outreach[]

```bash
# Substitute the literal job_id/channel/recipient/filename/medium values —
# bash variables do not persist between Bash calls.
uv run python -c "
from pathlib import Path
from src.state_io import state_path_for, append_outreach_draft
p = state_path_for(Path('pipeline'), '<job_id>')
data = append_outreach_draft(
    p,
    channel='<channel>',
    to_name='<recipient name>',
    draft_file='<date>_<channel>_<recipient-slug>.md',
    contact_id='<id from contacts.yaml>',   # omit the kwarg if the recipient has no entry
    medium='<linkedin | email>',            # omit the kwarg if no medium applies
)
print(f'OK: outreach[] now has {len(data[\"outreach\"])} entry/entries')
"
```

## Step 6 — runtime assertions

- [ ] Draft file exists at DRAFT_PATH (defined in Step 3)
- [ ] Final lint pass returns zero violations in outreach context
- [ ] Word count is within the channel limit
- [ ] state.yaml.outreach[] has a new `{channel, to, status: draft, draft_file}` entry,
      carrying `contact_id` when the recipient came from `contacts.yaml`
- [ ] If channel is `recruiter`, the draft does NOT volunteer OPT status
- [ ] If channel is `referral` or `alumni`, the draft DOES surface OPT
      lightly and upfront

If any assertion fails, do NOT report success.

## Step 7 — report

Tell the user, filling every `<...>` from what you actually wrote:
```
Outreach drafted: <DRAFT_PATH>
  - channel: <channel>   to: <recipient name>   medium: <linkedin | email>
  - word count: <n> (channel limit: <the limit for this channel>)
  - lint: 0 violations
  - "Why this approach" footer is at the bottom — DELETE before sending.

Send manually, then:
  /track outreach-sent <job_id> --channel <channel> --to "<recipient name>"
```
