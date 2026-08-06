# TEMPLATE — copy to profile/scoring_rubric.md and edit.
#
# Shared scoring contract, included in full in every judge call's packet
# alongside the assigned vertical's rubric.md. Per-vertical tiers and caps live
# in profile/verticals/<vertical>/rubric.md — never here.
#
# Everything below is enforced by src.scoring_io.validate_scores except the
# thresholds in "suggested_action", which are yours to tune.

### Per-row JSON schema (validated — output is REJECTED if any of these fail)

```json
{
  "job_id": "a1b2c3d4",
  "fit_subscores": {"title": 25, "skills": 22, "seniority": 20, "domain": 11},
  "vertical": "example_primary",
  "sponsorship_label": "opt_ok",
  "sponsorship_evidence": "no visa sponsorship",
  "reasoning": "strong widget-assembly fit; senior stretch",
  "keywords_to_mirror": ["widget assembly", "gizmo calibration"],
  "suggested_action": "tailor"
}
```

Hard requirements:
- Do NOT emit `fit_score`. The total is derived from the four subscores by
  `fit_score_from_subscores`. Score each axis on its own merits; never pick a
  total first and distribute it across the axes.
- Axis maxima (locked): title ≤ 30, skills ≤ 30, seniority ≤ 20, domain ≤ 20
  (they sum to 100).
- `job_id` is the row's 8-character id, copied verbatim.
- `vertical` MUST be copied verbatim from the row's precomputed `vertical`
  field (one of the names configured in `profile/verticals.yaml`) — never
  invented or reclassified.
- `sponsorship_label ∈ {sponsors, opt_ok, ineligible, unknown}`.
- `suggested_action ∈ {tailor, skip, manual-review}`.
- `reasoning` is a skim tag of AT MOST 15 words naming the strongest fit signal
  and the strongest concern. Not sentences — a tag.
- `sponsorship_evidence` is the SINGLE SHORTEST decisive JD quote for the
  label. If `sponsorship_label == "ineligible"`, it MUST be a non-empty literal
  quote of the disqualifying JD phrase. Otherwise it is the shortest trigger
  phrase for the label (or `""` for `unknown`).
- `keywords_to_mirror` is a list of 2–3 JD keywords the tailored resume should
  surface. Empty list `[]` only when the row is `ineligible` or the JD carries
  no meaningful signal.
- One record per `job_id`. A duplicate is a validation error.
- Output NO fields beyond this schema (plus optional `flags`).

### Sponsorship labeling — DO THIS FIRST per row, before fit-scoring

Read `profile/sponsorship_rules.yaml`. Precedence:
1. **`ineligible` wins outright** if any phrase in `ineligible:` matches
   (case-insensitive substring). Quote the matching phrase verbatim in
   `sponsorship_evidence`. **Set all four subscores to 0** — ineligible rows
   are not fit-scored.
2. Else if any `opt_ok:` phrase matches → `opt_ok`.
3. Else if any `sponsors:` phrase matches → `sponsors`.
4. Else → `unknown`.

**False-positive guard (BINDING):** phrases in `false_positive_guard:` are
NEVER blockers on their own. They count as no signal; combine with other
phrases as normal. Populate that list with the boilerplate that does not
actually exclude you given your status in `profile/preferences.md` — for
someone already authorized to work, "must be authorized to work in the US" is
boilerplate, not a blocker. Read `preferences.md` for the authorization status
this guard assumes; never infer it from this file.

### Fit-scoring rubric — skip if `sponsorship_label == "ineligible"`

**Read `profile/verticals/<the row's vertical>/rubric.md` IN FULL and apply
ONLY that vertical's tiers, caps, and additional self-check items.** Never mix
tiers across verticals, and never read another vertical's rubric file.

**Shared JD quality gate (applies to the skills axis in every vertical — check
first):** if the JD body is fewer than 300 words OR is generic boilerplate with
no specific process, module or technical detail → cap that axis at 12 and
include "vague JD" in `reasoning`.

### reasoning tags

Append a bracket tag to `reasoning` — it does not count toward the 15-word cap:
- `[staffing agency]` if the posting is from a placement firm rather than the
  employer. List the agencies you keep seeing in your own copy of this file, or
  judge it from the company name.
- `[contract role]` if `employment_type` is `contract`.

### suggested_action — from your subscore total (their sum; not an emitted field)

Tune these three thresholds to your own funnel:
- `tailor` if the total is ≥ 60 AND sponsorship is `sponsors` / `opt_ok` /
  `unknown`.
- `manual-review` if the row is borderline (50–59) or carries an interesting
  tension (strong domain match against a hard seniority penalty).
- `skip` if the total is < 50, or the title is clearly out-of-lane despite
  in-lane vocabulary in the JD body, or the JD shows the posting is not
  genuinely applicable (an explicit test posting, or a location or citizenship
  restriction you cannot meet) regardless of how high the total is.

### Self-check before writing each batch

For every row, confirm the hard requirements hold: no `fit_score` key; each
axis within its maximum; `vertical` copied verbatim; ineligible carries a
quoted JD phrase and four zeroed subscores; `false_positive_guard` phrases
alone never trigger ineligible; the vague-JD cap applied where the JD is under
300 words. Beyond those:
- title axis = 0 on out-of-lane titles even when the JD body uses in-lane
  vocabulary.
- If the JD states a minimum-experience requirement above the vertical's
  configured `disqualifier.max_years` anywhere in the body, this row should
  have been pre-screened out before reaching you. Flag it as a pre-screen bug
  rather than silently capping the seniority axis yourself.

Then run the **Additional self-check items** in that vertical's
`profile/verticals/<vertical>/rubric.md`.

If any row fails, fix it before writing the batch file.
