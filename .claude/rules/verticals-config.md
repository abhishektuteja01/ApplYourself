---
paths:
  - "src/applyourself/shared/verticals.py"
  - "src/applyourself/resume_model.py"
  - "profile/verticals.yaml"
  - "profile/verticals/*/lane.yaml"
  - "profile/resume.yaml"
  - "profile/verticals.example.yaml"
  - "tests/fixtures/**"
---

# Verticals: the config spine

A "vertical" is a job lane. `src/applyourself/shared/verticals.py` is the single source of truth loader.

- Config lives in `profile/verticals.yaml` (gitignored user data: `lanes:` order,
  `defaults:`, `classifier_rules`, `title_variants`) and one `profile/verticals/<name>/` dir per
  lane holding `lane.yaml` (its block), `rubric.md`, `tailoring.md` and an
  optional `examples.yaml` (the judge's calibration examples). The
  loader merges each `lane.yaml` over `defaults:`: a default list follows the
  lane's own, `score:` merges key by key, any other key the lane sets replaces
  the default's. `years`, `skill_weights` and `focus` (the old judge's) are
  still accepted and read by nothing; `config migrate` removes them. A lane is
  loaded only when `lanes:` lists it. `lane.yaml` sits in `verticals/` beside
  whichever verticals file names it, so the example and the fixture carry their
  own.
- A classifier rule is a `pattern` regex, a `{match, require_any}` pair (match
  a term or a list) or a `terms` list (`keep: true` beats a ban); the last two
  match loosely with `title_variants`. The title ban list is `defaults:
  title_exclude_terms`. A lane's own `title_include_terms` /
  `title_strong_keep_terms` / `title_exclude_terms` are still read (include
  and strong-keep count as fits for the lane's own search rows, strong-keep
  beats a ban). `uv run config migrate` moves them out: multi-word terms into
  `terms` rules, most words first (strong-keep ones `keep: true`), each lane's
  single words into one `{match, require_any, search_is_context: true}` rule
  needing another of its words; every exclude term all lanes share into
  `defaults:` (none when `defaults:` already has a list). It replays
  jobs/clean.parquet and jobs/raw/, reading the config before and after with
  today's matcher, refuses on any title that would drop or change lane, lists
  the titles that pass only after, and says so when there is nothing to
  replay. Add `title_variants` before migrating: a variants group counts as
  one word.
- **One source per resume fact** (`src/applyourself/resume_model.py`): `profile/resume.yaml`
  holds name, contact, education and each context's header plus per-lane
  `{min, max, order}`; `bullets.md` holds claims and `default_for`;
  `skills_master.md` holds skills and `show`; a lane's `resume:` block holds only
  shape (sections, bullet budget, skill lines, baseline summary). Things say
  where they appear, so deleting one is one edit, and `verticals-check`
  (`check_sources`) names every pointer that stops resolving. `tailoring.md` is
  prose only: ids there are rejected.
- `/score`'s judge grades against a digest of every bullet and skill
  (`score_packet.digest`), not a lane resume; `/tailor` starts from the lane's
  baseline resume.
- The loader is **strict**: every vertical block must have all current required
  keys or it raises `ValueError`. Because `tests/conftest.py` injects the config
  via an autouse fixture, a malformed block errors the *entire* test suite.
- One fixture, `tests/fixtures/verticals.yaml` plus
  `tests/fixtures/verticals/<lane>/lane.yaml`, serves the whole suite. A schema
  change updates it in the same change.
- Templates for onboarding a new vertical: `profile/verticals.example.yaml` and
  `profile/verticals/example_*/{lane.yaml, rubric.md, tailoring.md}` (three: primary, secondary, tertiary — the
  fixtures' `default_vertical` is tertiary). Use `/new-vertical` to add a lane,
  `/tune-vertical` to sharpen one.

> The fixture is **synthetic** — three fictional verticals
> (`example_primary/secondary/tertiary`), no real search terms or rubrics.
> The real config is covered separately by `tests/repo/test_real_config_drift.py`,
> which skips when `profile/verticals.yaml` is absent. Keep real strategy out of
> the fixtures; add real-config assertions to the drift test instead,
> structurally (never pin a real term — it is committed).
