"""Every `uv run` step a slash command tells its session to run is allowed.

A step the session's permissions miss is denied, and a headless night session
fails without asking. Patterns follow Claude Code's Bash rules: `Bash(x *)` and
`Bash(x:*)` match `x` alone or followed by a space, `*` elsewhere matches
anything, and no `*` is an exact match.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMMANDS = sorted((ROOT / ".claude/commands").glob("*.md"))
# The headless night stages: (script, the slash command its prompt runs).
NIGHT = [
    ("scripts/stages/apply_prep.sh", "apply.md"),
    ("scripts/stages/tailor_one.sh", "tailor.md"),
]

FENCE = re.compile(r"^\s*```(\w*)\s*$")
# A numbered step that opens with an inline command: "1. `uv run ay score start`. ..."
STEP = re.compile(r"^\s*\d+\.\s+`(uv run [^`]+)`")
# A prose placeholder stands for one argument the session fills in: a word or a flag.
PLACEHOLDER = re.compile(r"""<[^<>]*>|["']?\$\{?\w+\}?["']?""")
OPTIONAL = re.compile(r"\[([^\[\]]*)\]")
REDIRECT = re.compile(r"\s+\d?>>?\s*(&\d|\S+)|\s+&$")
FILLS = ("x", "--x")


def _allowed_tools(path: Path) -> list[str]:
    """The frontmatter's `allowed-tools:` list (read by line: descriptions are not strict YAML)."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return []
    tools: list[str] = []
    inside = False
    for line in text.split("---\n", 2)[1].splitlines():
        if line.startswith("allowed-tools:"):
            inside = True
        elif inside and line.startswith("  - "):
            tools.append(line[4:].strip())
        else:
            inside = False
    return tools


def _bash_patterns(tools: list[str]) -> list[str]:
    return [t[5:-1] for t in tools if t.startswith("Bash(") and t.endswith(")")]


def _settings_allow() -> list[str]:
    data = json.loads((ROOT / ".claude/settings.json").read_text(encoding="utf-8"))
    return _bash_patterns(data.get("permissions", {}).get("allow", []))


def _stage_tools(script: str) -> list[str]:
    text = (ROOT / script).read_text(encoding="utf-8")
    m = re.search(r"^TOOLS='([^']*)'", text, re.M)
    assert m, f"{script}: no TOOLS='...' line"
    return _bash_patterns(re.findall(r"\w+(?:\([^()]*\))?", m.group(1)))


def _matches(pattern: str, command: str) -> bool:
    if pattern.endswith(":*"):
        pattern = pattern[:-2] + " *"
    if pattern.endswith(" *"):
        rx = ".*".join(map(re.escape, pattern[:-2].split("*"))) + "( .*)?"
    else:
        rx = ".*".join(map(re.escape, pattern.split("*")))
    return re.fullmatch(rx, command, re.S) is not None


def _variants(command: str) -> list[str]:
    """Each way the session may run a step: every [optional] part with and without its
    content, and every placeholder as a word and as a flag."""
    parts = OPTIONAL.split(command)  # literal, group, literal, group, ...
    cmds = [""]
    for i, part in enumerate(parts):
        cmds = [c + part for c in cmds] + ([c for c in cmds] if i % 2 else [])
    return [" ".join(PLACEHOLDER.sub(fill, c).split()) for c in cmds for fill in FILLS]


def _allowed(command: str, patterns: list[str]) -> bool:
    """True when every variant of the step matches some pattern."""
    return all(any(_matches(p, v) for p in patterns) for v in _variants(command))


def _session_steps(path: Path) -> list[str]:
    """The `uv run` commands in a file's bash/sh/plain fenced blocks and numbered steps."""
    out: list[str] = []
    block: str | None = None
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = FENCE.match(line)
        if m and block is None:
            block, lines = m.group(1), []
            continue
        if m and block is not None:
            if block in ("", "bash", "sh", "shell"):
                out.extend(_commands("\n".join(lines).replace("\\\n", " ")))
            block = None
            continue
        if block is not None:
            lines.append(line)
        elif s := STEP.match(line):
            out.extend(_commands(s.group(1)))
    return out


def _commands(script: str) -> list[str]:
    found = []
    for line in script.splitlines():
        line = re.sub(r"\s+#.*$", "", line.strip())
        for seg in re.split(r"&&|\|\||\||;", line):
            seg = REDIRECT.sub("", seg.strip()).strip()
            if seg.startswith(("uv run ", "nohup uv run ")):
                found.append(" ".join(seg.split()))
    return found


@pytest.mark.parametrize("path", COMMANDS, ids=lambda p: p.name)
def test_command_steps_are_allowed(path):
    patterns = _bash_patterns(_allowed_tools(path)) + _settings_allow()
    denied = [c for c in _session_steps(path) if not _allowed(c, patterns)]
    assert not denied, f"{path.name}: not allowed by its allowed-tools or settings.json: {denied}"


@pytest.mark.parametrize(("script", "command"), NIGHT, ids=[c for _, c in NIGHT])
def test_night_session_steps_are_allowed_without_settings(script, command):
    path = ROOT / ".claude/commands" / command
    patterns = _stage_tools(script) + _bash_patterns(_allowed_tools(path))
    steps = _session_steps(path)
    assert steps, f"{command}: no uv run steps found"
    denied = [c for c in steps if not _allowed(c, patterns)]
    assert not denied, f"{script} running /{command[:-3]}: not allowed: {denied}"


@pytest.mark.parametrize(
    ("pattern", "command", "ok"),
    [
        ("uv run ay app *", "uv run ay app dir x", True),
        ("uv run ay app *", "uv run ay app", True),
        ("uv run ay app *", "uv run ay apply run", False),
        ("uv run ay discover", "uv run ay discover --x", False),
        ("uv run ay discover --*", "uv run ay discover --source x", True),
        ("uv run ay discover --*", "uv run ay discover dupes", False),
        ("uv run score:*", "uv run score start", True),
    ],
)
def test_pattern_semantics(pattern, command, ok):
    assert _matches(pattern, command) is ok


@pytest.mark.parametrize(
    ("step", "ok"),
    [
        ("uv run ay discover <mode> --apply", False),
        ("uv run ay discover [dupes --apply]", False),
        ("uv run ay discover [--source <name>]", True),
        ("uv run ay discover", True),
    ],
)
def test_a_step_passes_only_when_every_variant_is_allowed(step, ok):
    assert _allowed(step, ["uv run ay discover --*", "uv run ay discover"]) is ok
