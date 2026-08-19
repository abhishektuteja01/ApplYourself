---
paths:
  - "scripts/**"
  - ".githooks/**"
  - ".github/**"
---

# The PII gate

The repo is public (`github.com/abhishektuteja01/ApplYourself`).
`scripts/pii_scan.sh` fails on any denylisted string in a tracked file, reading
patterns from gitignored `profile/pii_denylist.txt`. `.githooks/pre-push` runs it
(`git config core.hooksPath .githooks`, once per clone), and
`.claude/hooks/pii_gate.sh` runs it again before any `git commit`.

It reads the git **index**, so it only sees tracked files: run it *after* staging,
and never put a real pattern in a committed file. It exits 2 when the denylist is
missing (fail-closed; `PII_SCAN_ALLOW_MISSING=1` downgrades that to a warning for a
fresh clone), and exits 0 with "no tracked files to scan" when nothing is staged —
a green gate that inspected nothing.

The in-script allowlist matches **exact paths, never globs**: `LICENSE`, the
denylist template, each `data/universe/` CSV, and the two example `.docx` by name.
A new universe CSV is not covered until it is added to that list by name.

The pre-push hook also scans what `pii_scan.sh` structurally cannot: the pushed
commits' **messages, author and committer fields**, plus `user.email` itself. That
metadata lives in no file, so the index scan is blind to it — and GitHub renders an
author email on every commit page.
