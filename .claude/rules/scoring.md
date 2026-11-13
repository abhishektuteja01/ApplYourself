---
paths:
  - "src/applyourself/score_cli.py"
  - "src/applyourself/score_filter.py"
  - "src/applyourself/score_store.py"
  - "src/applyourself/score_packet.py"
  - "src/applyourself/score_ab.py"
  - "src/applyourself/shortlist.py"
---

# Scoring architecture (`/score`)

Three steps, one owner per rule:

1. `score start` (Python): `score_filter.drop_reason` drops rows by the hard
   rules (no lane, `profile/scoring.yaml` `ineligible` plus its
   `no_sponsorship` phrases when the home need is `now`). Years go to the
   judge: its count, against the lane's `score:` rules, decides in `derive`;
   the regex's count (`years_required`) only flags a gap. A lane's
   `drop_phrases` and scoring.yaml's `years_margin` are still read but drop
   nothing. A drop is never stored: it is
   checked again every run, and a stored score of a row it now drops is
   deleted. It prints `dropped_<code>` for every drop and `filtered_<code>`
   for those the latest discovery run saw first (the night summary's
   Filtered). Discovery's retention asks the same rule of an unscored row.
   The rest is staged in `jobs/scored.staging/` as one packet per
   lane and slices of at most 10 roles, each row with its `judge_input_hash`
   (`input_hash` of the lane packet without the digest, the employer section
   and the `employer` field (`UNHASHED_FIELDS`: no score reads them), and the
   trimmed JD) and `judge_profile_hash` (the digest's `profile_hash`). The
   packet, always in this order: `.claude/shared/scoring_rubric.md` (fixed fit
   bands, tie-breaks), `.claude/shared/scoring_employer.md`, the candidate digest (`score_packet.digest`: every bullet with
   its tags, every skill with its aliases (no bullet rephrasings), each
   context's span (an open "Present" one up to the staging day), education
   with a "Currently enrolled" line from resume.yaml's education dates, the
   bank's sponsorship needs, citizenship, U.S. person answer and languages,
   rebuilt every run), the lane's
   `rubric.md`, its optional
   `examples.yaml` (`score examples` picks them from past scores by a stable
   rule), and the answer schema. Rows go newest
   first (the store's first_seen; the newest posting's with discovery.yaml
   `retention_from: newest`, over every stored posting, where the view counts
   only postings that pass its gates; this sets order only), so each judge works its newest slices first.
2. `score-judge` agents (the only model step): the facts first (`in_lane`,
   `level` from the title, `years`, `country`, the JD's sponsorship
   statements with literal JD quotes, a `blocker` or null), then one `fit`
   0-100, keywords, a reason of at most 15 words and the `employer` (the
   hiring company the posting names, or null).
3. `score finish` (Python): checks each answer against its slice (an
   `employer` name the title, company line and JD do not show is dropped, as
   if unanswered; a slice staged before the field merges without it), derives the
   sponsorship label (a `no_sponsorship` where the need is `later` takes
   scoring.yaml `no_sponsorship_when_later`, else scoring_defaults.yaml's; a
   JD sentence refusing sponsorship now or in the future,
   `score_filter.future_refusal` over the title and whole JD and stored in
   `judge_facts`, adds `no_future_sponsorship` in place of `silent`; a row
   without the key derives as before, and
   `scripts/dev/backfill_future_refusal.py --dry-run|--apply` fills it once),
   tags, score and action, merges (with the raw answer in
   `judge_answer`), and writes the shortlist. The score is 0 when the role is
   out of lane, ineligible, outside the lane's countries, asks the lane's
   `score.years_zero_at` years or more, or has a blocker; otherwise fit
   (capped at scoring.yaml `jd_quality.fit_cap` for a short JD when set;
   absent, no cap; a short JD is tagged `[short JD]` either way) less the
   larger of the lane's `score.level` and `score.years` penalties.

Each lane's `score.cutoff` (verticals.yaml `defaults:` and lane.yaml, merged
key by key; `score_filter.lane_cutoffs`) is its line for the night's sheet; the
live view keeps a role `high_days` from the lowest lane cutoff, or a lower
`cutoffs.keep_long` (`retention_cutoff`). A lane with no `score.cutoff` reads
scoring.yaml's older `cutoffs.sheet`.

Every reader of scored.parquet goes through `score_store.view()`: it derives
each row holding today's answer (it has `fit`) again under today's rules
(`basis: judged`), keeps an older judge's answer at its stored score (`basis:
v1`) and a row with no raw answer too (`basis: legacy`), and leaves out
`filter:*` rows. Nothing re-judges an old row by itself. Never
`pd.read_parquet` it for a score.

run.json records its answer form (`schema_version` 2, `form`). A run left
open from the old judge is closed by the next `score start`: its answered
slices merge at their old score (`basis: v1`), the rest are staged again;
`score finish` refuses it until then. Shipped defaults (the `score:`
penalties, the fallback cutoff, the example targets) live in
`data/defaults/scoring_defaults.yaml`; a `profile/scoring_defaults.yaml`
replaces it. A lane `score.cutoff` below `cutoffs.review` fails loading.

`score ab-stage --out DIR` stages one sample with the old (v1) packet and the
new one into `DIR/v1`, `DIR/v2` and a repeat subset into `DIR/v2_repeat`;
`score ab-compare DIR` reads the answers. The v1 arm lives only in
`score_ab.py`, read from a lane's old keys and `rubric.v1.md`; it goes once the
A/B is done.

Do not move a mechanical rule back into a rubric, or judgment into Python. A
new hard rule goes in `score_filter.py` plus a `profile/` key; a new derived
field goes in `score_store.derive`.

`/score --all [--vertical V]` re-judges instead of scoring only new rows,
except roles whose state is past `shortlist.pipeline_states`; there is no
separate rescore command. `--stale-only` takes only judged rows whose input
hash changed and prints the count without staging; `--yes` stages them. A
profile edit alone never makes a row stale: it prints an `info` line with how
many rows were judged before the profile last changed. A row whose
JD only reformats the judged one from the same source or moves its sentences
(the same sentences, boilerplate trimmed, `+`/`#` kept; `same_jd_copies`),
such as a repost, is not stale; any added, dropped, changed or swapped word is. A row with
no hash (legacy) is never stale, and a plain `score start` re-judges nothing.
`--job-id` (for `/ingest`) skips every drop except no-lane.
`scripts/dev/drop_filter_rows.py --dry-run|--apply` removes the old `filter:*`
rows once; the next `/score` then re-checks those roles.

scored.parquet is never pruned: a score outlives its role's place in the
view, so a role that comes back is not judged again. Only a role today's hard
rules drop loses its score.

Note: `shortlist.py` imports `apply/detect.py`, which pulls the ATS parsers
into the scoring path at import time.
