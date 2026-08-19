# TEMPLATE — copy to profile/preferences.md and edit.
#
# Read by /score (every judge call), /tailor and /apply. Plain guidance, not a
# strict filter: the deterministic filters live in profile/verticals.yaml
# (disqualifiers) and profile/discovery.yaml (location allowlist). The one
# exception is the Work authorization section below, which /apply reads
# strictly.
#
# Keep it short. Every line here is in the packet for every row scored.

## Work authorization

Keep ONE of these three and delete the rest. This is the line
profile/scoring_rubric.md's false-positive guard defers to, so it has to be
unambiguous.

/apply enforces that. Before it fills a form it cross-checks
profile/application_answers.yaml's `work_authorization.status` against this
section, and refuses to run if the two disagree, if no status is stated, or if
more than one is — as in this unedited template. Those questions are answered
under your name on a legal point, so there is no default and no guess.

- **Citizen / permanent resident:** No sponsorship needed, ever. Hard exclude:
  nothing on authorization grounds. Roles requiring an active security
  clearance you do not hold → `ineligible`.
- **Needs sponsorship now:** Requires visa sponsorship from day one. Hard
  exclude: any posting stating it does not sponsor → `ineligible`.
- **Time-limited work authorization (e.g. F-1 OPT, STEM extension):**
  Authorized now for <N> months; sponsorship needed after that. Hard exclude:
  roles requiring citizenship or a clearance → `ineligible`.

## Preferences

- Open to every vertical in `profile/verticals.yaml`; each is scored on its own
  rubric and ranked only within its own shortlist section.
- Location: <any US location, onsite/hybrid/remote | list your metros | remote
  only>. Narrowing here is guidance for judges; the hard geographic filter is
  `location_allowlist` in `profile/discovery.yaml`.
- Compensation: <no floor | a floor, and whether it is firm>.
- Deal-breakers: <list them, or "none beyond the authorization gate">. Anything
  you would refuse regardless of fit belongs here.
