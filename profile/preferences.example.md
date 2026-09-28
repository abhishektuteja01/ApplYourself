# TEMPLATE — copy to profile/preferences.md and edit.
#
# Read by /apply. Plain guidance, not a strict filter. /score
# does not read this file: its rules live in profile/scoring.yaml
# (sponsorship_need, visa_free_countries, ineligible), profile/verticals.yaml
# (drop_phrases, years) and profile/discovery.yaml (countries per lane). The
# one exception is the Work authorization section below, which /apply reads
# strictly.

## Work authorization

Keep ONE of these three and delete the rest. Match it in
profile/scoring.yaml's `sponsorship_need` (never / now / later).

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

## Work countries

Optional; delete it if you search only at home.

- No visa needed: <countries> (also `visa_free_countries` in
  profile/scoring.yaml).
- Visa needed: <countries>. A role there counts as `sponsors` only when the JD
  offers sponsorship.

## Preferences

- Open to every vertical in `profile/verticals.yaml`; each is scored on its own
  rubric and ranked only within its own shortlist section.
- Location: <any US location, onsite/hybrid/remote | list your metros | remote
  only>. The hard geographic filter is `location_allowlist` in
  `profile/discovery.yaml`.
- Compensation: <no floor | a floor, and whether it is firm>.
- Deal-breakers: <list them, or "none beyond the authorization gate">. Anything
  you would refuse regardless of fit belongs here.
