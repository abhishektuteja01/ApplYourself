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
suite until it has a template. Excluded: optional overrides of a
`data/defaults/` file (`de_ai_rules.yaml`), the `example_*` lane dirs, and
dotfiles and `profile/.state/` (runtime state a command writes).

Template content lives in the fictional widget/gizmo/sprocket/cog world of the
`example_*` lanes, and it must pass the same `check_sources` the real config
does, with every example lane's baseline resume building clean
(`TestExampleSourcesResolve`).

`profile/*.example.docx` are the only tracked binaries, allowlisted **by name** in
`pii_scan.sh` (never by glob) with `tests/test_example_templates.py` standing in
for the text scan the gate skips. They are hand-authored in Word; every save stamps
the editor's name into the document metadata, so run
`scripts/scrub_example_templates.py` afterwards. `--check` exits 1 if either file
still needs it.

`uv run onboard-scaffold` copies each template to its real name, skipping any file
that already exists; `/onboarding` is the five-step pass that fills them in.
