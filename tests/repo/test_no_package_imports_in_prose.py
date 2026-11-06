"""Command prose and shell scripts reach the package only through CLI entry points,
so a module rename cannot break them silently."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMPORT = re.compile(r"\bfrom applyourself\b|\bimport applyourself\b")


def test_no_package_imports_in_claude_or_scripts():
    files = [
        p
        for d in (".claude", "scripts")
        for ext in ("*.md", "*.sh")
        for p in (ROOT / d).rglob(ext)
        if "worktrees" not in p.relative_to(ROOT).parts
    ]
    hits = [
        f"{p.relative_to(ROOT)}:{n}"
        for p in files
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if IMPORT.search(line)
    ]
    assert files and not hits, hits
