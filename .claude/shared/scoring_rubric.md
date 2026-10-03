# Scoring rubric — read by every score-judge

Your packet holds this file, the lane's rubric, the lane's resume and skill
weights. You read facts and judge fit; every rule that follows from a fact
(years limits, where each lane may work, what a sponsorship statement means
for the candidate, the total and the action) is applied after you.

## What to write, per role

- `title` (0-30), `skills` (0-30), `seniority` (0-20), `domain` (0-20): score
  each axis on its own merits from the lane rubric's tiers. Never pick a total
  first. Tiers are anchors: a role between two tiers takes a value between
  them. Score `seniority` by the title level only.
- `years`: the minimum years of experience the JD requires, as an integer.
  Lower bound of a range ("7-10 years" → 7). Ignore preferred or nice-to-have
  sections and company history ("100 years in business"). 0 if none stated.
- `country`: the English name of the country the role is in ("United States",
  "Germany", "United Arab Emirates"). Several allowed countries → join them
  with ";". "" when the JD does not say, or the role is remote anywhere.
- `sponsorship`: a list of every statement the JD makes about sponsorship.
  Read the whole JD; one posting often makes two ("will not sponsor visas or
  participate in STEM OPT" → both `no_sponsorship` and `no_opt_support`).
  - `offers` — the employer offers visa sponsorship or visa support.
  - `no_sponsorship` — the employer will not sponsor ("we do not sponsor",
    "unable to sponsor visas").
  - `no_future_sponsorship` — the candidate must not need sponsorship now or
    in the future.
  - `no_opt_support` — the employer will not support F-1 OPT or STEM OPT
    work (no OPT extension, no training plan, no E-Verify).
  - `status_bar` — only listed statuses may apply (citizen, green card, a
    security clearance, "USC/GC only").
  - `right_to_work` — the candidate must already hold the right to work in
    that country, with no offer of sponsorship.
  - `silent` — nothing decisive; use it alone. Work-authorization boilerplate ("must be
    authorized to work in the US") and a sponsorship question on an
    application form are `silent`.
- `evidence`: each statement's shortest JD phrase, verbatim, joined with
  " ... ". `""` only when `["silent"]`.
- `keywords_to_mirror`: the JD's most important keywords or skills for the
  tailored resume to surface, most important first, at least 2, up to the
  limit at the top of the packet. `[]` only when the JD has no real signal.
- `focus`, `focus_evidence`: only when the packet has a Focus section —
  `true` or `false` answering its question, and the JD phrase that shows a
  `true`, verbatim.
- `reasoning`: a skim tag of at most 15 words naming the strongest fit signal
  and the strongest concern. Append `[staffing agency]` when the poster is a
  placement firm, body shop or IT vendor reselling contract labor. Name a
  language requirement here.
- `action` (optional): `skip` when the title is clearly out of lane despite
  in-lane words in the JD, or the posting is not genuinely applicable (a test
  posting). `manual-review` when a strong role carries a real tension (great
  domain, hard seniority stretch). Otherwise leave it out.

Short JDs get their `skills` cap automatically. Long ones are yours to judge:
a JD that runs long but is generic boilerplate, with no specific module,
process or technical detail, caps `skills` at 12; say "vague JD" in
`reasoning`.
