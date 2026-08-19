---
paths:
  - "profile/**"
---

# New-user templates (`profile/*.example.*`)

Every `profile/` input a command or module reads must be either committed outright
or shipped as a `.example` template beside it.
`tests/test_profile_templates.py` derives that list by scanning every place a
`profile/` path can be referenced — command prose (all of `.claude/**/*.md`,
including rules files), `src/` modules, scripts, hooks, CI config (see its
`_source_groups()` for the current set) — so wiring in a new profile file fails the
suite until it has a template. Excluded: committed defaults
(`de_ai_rules.yaml`, `sponsorship_rules.yaml`), the `example_*` lane dirs, and
dotfiles (runtime state a command writes).

Template content lives in the fictional widget/gizmo/sprocket/cog world of the
`example_*` lanes, and the ids must resolve: the `SKILL-*` ids those lanes name in
their Skills layouts must exist in `profile/skills_master.example.md`, and every
`evidence:` reference must point at a bullet in `profile/bullets.example.md`. Tests
enforce both directions.

`profile/*.example.docx` are the only tracked binaries, allowlisted **by name** in
`pii_scan.sh` (never by glob) with `tests/test_example_templates.py` standing in
for the text scan the gate skips. They are hand-authored in Word; every save stamps
the editor's name into the document metadata, so run
`scripts/scrub_example_templates.py` afterwards. `--check` exits 1 if either file
still needs it.

`uv run onboard-scaffold` copies each template to its real name, skipping any file
that already exists; `/onboarding` is the five-step pass that fills them in.
