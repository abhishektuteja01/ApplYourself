# example_primary vertical rubric (TEMPLATE)

Copy this dir to `profile/verticals/<your_vertical>/` and rewrite every tier
around YOUR attested experience. Judges read this file IN FULL alongside the
shared scaffold in `profile/scoring_rubric.md` and apply ONLY this vertical's
tiers to rows whose `vertical` matches. Axis names and maxima are fixed by
the scaffold: `title_match` 0-30, `jd_skill_overlap` 0-30, `seniority_fit`
0-20, `domain_bonus` 0-20; integer subscores sum to `fit_score`.

- `title_match` (0-30): three tiers — niche spine (29-30): "Widget Engineer"
  exact-lane titles; general spine (25-28): platform/consultant variants;
  adjacent (15-22): operations/analyst titles; out-of-lane → 0.
- `jd_skill_overlap` (0-30): apply the shared JD-quality gate first, then
  anchor on this vertical's `skill_weights` in `profile/verticals.yaml`:
  weight sum ≥30 → 25-30; 15-29 → 15-24; 5-14 → 5-14; <5 → 0-4. Score only
  what the JD explicitly demands.
- `seniority_fit` (0-20): Analyst/Associate/Junior → 18-20 (target level);
  standalone Consultant/Specialist → 6-10; Senior anything → 6-10;
  Lead/Principal/Manager/Director → 0-6. Explicit years override the title
  heuristic: 3+ yrs → cap 14; 4+ yrs → cap 10 (5+ yrs is pre-screened out
  deterministically and never reaches you).
- `domain_bonus` (0-20): top (16-20): widget-supply-chain signals; mid
  (8-12): generic manufacturing; none → 0.

## Additional self-check items (this vertical only)

- Did any scored row claim widget experience the profile doesn't attest?
