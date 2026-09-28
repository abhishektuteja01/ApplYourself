# TEMPLATE — copy to profile/preferences.md and edit.
#
# Plain guidance the judge and the writing commands read, not a strict filter.
# Your work-authorization facts live in one place,
# profile/application_answers.yaml's work_authorization.countries: /score and
# /apply both read them there. The hard drop list is profile/scoring.yaml.

## Work authorization

Keep ONE of these three and delete the rest. It is prose for the judge; the
facts are your home row in application_answers.yaml's
`work_authorization.countries`.

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

- No visa needed: <countries> (a row with `requires_sponsorship: false` in
  application_answers.yaml's `work_authorization.countries`).
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
