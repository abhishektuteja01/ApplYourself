# example_primary vertical rubric (TEMPLATE)

Copy this dir to `profile/verticals/<your_vertical>/` and rewrite every tier
around YOUR attested experience. Judges read this file IN FULL alongside the
shared scaffold in `profile/scoring_rubric.md` and apply ONLY this vertical's
tiers to rows whose `vertical` matches. The four axes and their maxima are
fixed by the scaffold; the labels used below are this repo's rubric prose for
them: `title` 0-30, `skills` 0-30, `seniority` 0-20,
`domain` 0-20. Emit integer subscores only, under the scaffold's own
JSON keys (`title`, `skills`, `seniority`, `domain`) — the total is derived.

- `title` (0-30): three tiers — niche spine (29-30): "Widget Engineer"
  exact-lane titles; general spine (25-28): platform/consultant variants;
  adjacent (15-22): operations/analyst titles; out-of-lane → 0.
- `skills` (0-30): anchor on this vertical's `skill_weights` in the
  packet:
  weight sum ≥30 → 25-30; 15-29 → 15-24; 5-14 → 5-14; <5 → 0-4. Score only
  what the JD explicitly demands.
- `seniority` (0-20): Analyst/Associate/Junior → 18-20 (target level);
  standalone Consultant/Specialist → 6-10; Senior anything → 6-10;
  Lead/Principal/Manager/Director → 0-6.
- `domain` (0-20): top (16-20): widget-supply-chain signals; mid
  (8-12): generic manufacturing; none → 0.

## Additional self-check items (this vertical only)

- Did any scored row claim widget experience the profile doesn't attest?
