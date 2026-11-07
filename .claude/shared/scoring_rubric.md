# Scoring rubric — read by every score-judge

Your packet holds this file, the candidate profile digest, the lane (what it
covers and its grade anchors), calibration examples when the lane has them,
and the answer schema. You read facts and grade fit. Everything that follows
from a fact is applied after you: the level and years penalties, where a lane
may work, what a sponsorship statement means for the candidate and the
final score. So never lower `fit` for level, years, country,
sponsorship or salary.

## Per role: facts first, then the grade

Fill the fields in this order. Read the whole posting before the first one.

1. `in_lane`: true when the role's actual work is this lane's work, as the
   lane section draws it. Judge the work and the title, not stray words: a
   title out of lane stays out even when the JD uses the lane's words.
2. `level`, from the title only:
   - `entry`: no level word, I, Junior, Associate, Graduate, New Grad;
   - `mid`: II, Intermediate;
   - `senior`: Senior, Sr, III, and any word the lane section calls senior;
   - `lead`: Staff, Lead, Principal, Manager, Head, Director, VP.
3. `years`: the minimum years of experience the JD requires, as an integer.
   Lower bound of a range ("7-10 years" → 7). Ignore preferred or
   nice-to-have sections and company history ("100 years in business"). 0 if
   none stated.
4. `country`: the English name of the country the role is in ("United
   States", "Germany", "United Arab Emirates"). Several allowed countries →
   join them with ";". "" when the JD does not say, or the role is remote
   anywhere.
5. `sponsorship`: a list of every statement the JD makes about sponsorship.
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
   - `silent` — nothing decisive; use it alone. Work-authorization
     boilerplate ("must be authorized to work in the US") and a sponsorship
     question on an application form are `silent`.
6. `evidence`: each sponsorship statement's shortest JD phrase, verbatim,
   joined with " ... ". `""` only when `["silent"]`.
7. `blocker`: `null`, or a reason of a few words when one of these holds:
   - the JD requires working fluency in a language the digest does not list
     (never when the digest says languages are not listed);
   - a campus or graduate program whose required degree or graduation window
     the digest does not meet;
   - a test, placeholder or fake posting;
   - out of lane despite in-lane words in the JD (set `in_lane` false too).
   Nothing else is a blocker. A stretch is graded in `fit`.
8. `fit` (0-100): how closely the digest matches the work. Use the bands
   below and the lane's anchors.
9. `keywords_to_mirror`: the JD's most important keywords or skills for the
   tailored resume to surface, most important first, at least 2, up to the
   limit below. `[]` only when the JD has no real signal.
10. `reasoning`: at most 15 words naming the strongest fit signal and the
    biggest gap. Append `[staffing agency]` when the poster is a placement
    firm, body shop or IT vendor reselling contract labor.

## Fit bands

Choose the band by its plain meaning first, then a number inside it.

| fit | means |
|---|---|
| 90-100 | Does this exact work today with matching tools: the role's core tasks and its named tools all appear in the digest. |
| 75-89 | Same kind of work; most core tools match; a gap or two that weeks of ramp would close. |
| 60-74 | Adjacent work: the core skill transfers, but key tools or the domain are new. |
| 40-59 | Overlap in parts only; the core of the job is not in the digest. |
| 0-39 | A different job. |

Inside a band, start at its middle. Move up for each core requirement the
digest shows with the same tool, down for each one it lacks. Stay in the band.

## Tie-breaks, so the same posting always gets the same number

- Torn between two bands: take the lower.
- Credit only what the JD states, never a requirement you infer.
- A long but generic JD with no specific tool, process or system is at most
  74; write "vague JD" in `reasoning`.
- Level, years, salary, location, sponsorship and the employer's fame are not
  fit; they are applied after you.
- When the lane has calibration examples, place the role against the nearest
  one before picking the number.
