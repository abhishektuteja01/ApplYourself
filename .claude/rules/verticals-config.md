---
paths:
  - "src/verticals.py"
  - "src/resume_model.py"
  - "profile/verticals.yaml"
  - "profile/resume.yaml"
  - "profile/verticals.example.yaml"
  - "tests/fixtures/**"
  - "tests/discovery/fixtures/**"
---

# Verticals: the config spine

A "vertical" is a job lane. `src/verticals.py` is the single source of truth loader.

- Config lives in `profile/verticals.yaml` (gitignored user data) with a matching
  `profile/verticals/<name>/{rubric.md, tailoring.md}` dir per vertical.
- **One source per resume fact** (`src/resume_model.py`): `profile/resume.yaml`
  holds name, contact, education and each context's header plus per-lane
  `{min, max, order}`; `bullets.md` holds claims and `default_for`;
  `skills_master.md` holds skills and `show`; a lane's `resume:` block holds only
  shape (sections, bullet budget, skill lines, baseline summary). Things say
  where they appear, so deleting one is one edit, and `verticals-check`
  (`check_sources`) names every pointer that stops resolving. `tailoring.md` is
  prose only: ids there are rejected.
- `/tailor` and `/score` build from the same assembler, so the judge scores the
  lane's baseline resume and `/tailor` starts from it.
- The loader is **strict**: every vertical block must have all current required
  keys or it raises `ValueError`. Because `tests/conftest.py` injects the config
  via an autouse fixture, a malformed block errors the *entire* test suite.
- **Two fixture mirrors must stay byte-identical to each other** for tests to
  pass: `tests/fixtures/verticals.yaml` and `tests/discovery/fixtures/verticals.yaml`.
  Any schema change must be mirrored into both in the same change.
  `TestFixtureMirrors` enforces it, and
  `.claude/hooks/fixture_mirrors.sh` fails immediately at edit time.
- Templates for onboarding a new vertical: `profile/verticals.example.yaml` and
  `profile/verticals/example_*/` (three: primary, secondary, tertiary — the
  fixtures' `default_vertical` is tertiary). Use `/new-vertical` to add a lane,
  `/tune-vertical` to sharpen one.

> The two fixture files are **synthetic** — three fictional verticals
> (`example_primary/secondary/tertiary`), no real search terms or skill weights.
> The real config is covered separately by `tests/test_real_config_drift.py`,
> which skips when `profile/verticals.yaml` is absent. Keep real strategy out of
> the fixtures; add real-config assertions to the drift test instead,
> structurally (never pin a real term — it is committed).

Note: `apply_cli.py` reads `profile/verticals.yaml` raw for an `apply:` block the
`Vertical` dataclass does not model, so `set_config()` injection does not reach
that path.
