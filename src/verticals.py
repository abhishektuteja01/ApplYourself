"""Vertical registry loader — the single source of truth for vertical
definitions.

Deterministic config loading only (no LLM calls in src/).
`profile/verticals.yaml` is gitignored user data; a missing file FAILS LOUD
with a pointer at the committed template — never a
silent empty fallback, unlike lint rules.

Consumers (discovery, cleaning, score_filter, score_store) must call
`get_config()` inside function bodies — never at module level, never
captured into module constants — so test injection via `set_config()`
always wins regardless of import order.

CLI: `uv run python -m src.verticals` validates the config, checks each
vertical's `profile/verticals/<name>/{rubric.md, tailoring.md}` exist and its
`resume:` block is set, runs the resume source check, and prints
`verticals=<names> default=<name>`. Slash commands use it as their
one-line prerequisite check.
"""

from __future__ import annotations

import re
from typing import Protocol
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from src import paths

REPO_ROOT = paths.REPO_ROOT  # tests repoint this name; keep it a module attr
DEFAULT_CONFIG_PATH = REPO_ROOT / "profile" / "verticals.yaml"
EXAMPLE_CONFIG_PATH = REPO_ROOT / "profile" / "verticals.example.yaml"
VERTICALS_DIR = REPO_ROOT / "profile" / "verticals"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class LaneResume:
    """A lane's resume shape (verticals.yaml `resume:`). No ids: bullets say
    which lanes pick them by default, skills say which line they show on."""
    sections: tuple[str, ...]          # a permutation of summary/work/projects/education/skills
    bullets: tuple[int, int]           # total (min, max)
    skill_lines: tuple[str, ...]       # line labels, in order
    summary: str                       # the baseline summary


_RESUME_SECTIONS = {"summary", "work", "projects", "education", "skills"}


def _parse_lane_resume(raw, path: Path, where: str) -> LaneResume:
    if not isinstance(raw, dict):
        raise _fail(path, f"{where} must be a mapping")
    unknown = set(raw) - {"sections", "bullets", "skill_lines", "summary"}
    if unknown:
        raise _fail(path, f"{where}: unknown keys {sorted(unknown)}")
    sections = _str_list(_require(raw, "sections", path, where), path, f"{where}.sections")
    if sorted(sections) != sorted(_RESUME_SECTIONS):
        raise _fail(path, f"{where}.sections must list each of {sorted(_RESUME_SECTIONS)} once")
    bullets = _require(raw, "bullets", path, where)
    if not (isinstance(bullets, list) and len(bullets) == 2
            and all(type(x) is int for x in bullets) and 1 <= bullets[0] <= bullets[1]):
        raise _fail(path, f"{where}.bullets must be [min, max]")
    skill_lines = _str_list(_require(raw, "skill_lines", path, where), path, f"{where}.skill_lines")
    summary = _require(raw, "summary", path, where)
    if not isinstance(summary, str) or not summary.strip():
        raise _fail(path, f"{where}.summary must be a nonempty string")
    return LaneResume(sections=sections, bullets=(bullets[0], bullets[1]),
                      skill_lines=skill_lines, summary=" ".join(summary.split()))


@dataclass(frozen=True)
class Vertical:
    name: str
    display_name: str
    search_terms: tuple[str, ...]
    linkedin_terms: tuple[str, ...]
    title_exclude_terms: tuple[str, ...]
    title_include_terms: tuple[str, ...]
    title_strong_keep_terms: tuple[str, ...]
    resume: LaneResume | None = None
    # Optional. The terms a jobspy lane's `abroad` block queries per country.
    abroad_terms: tuple[str, ...] = ()
    # /score's lane rules (src/score_filter.py). A JD holding a drop phrase is
    # dropped; `years` maps required years -> seniority cap, and a JD asking
    # more than its largest key is dropped before judging, or forced to skip
    # when the judge reads more.
    drop_phrases: tuple[str, ...] = ()
    years: dict[int, int] = field(default_factory=dict)
    # Judge-only: printed into the lane's scoring packet as-is.
    skill_weights: dict = field(default_factory=dict)


@dataclass(frozen=True)
class VerticalsConfig:
    default_vertical: str
    verticals: dict[str, Vertical]  # insertion order == config order
    classifier_rules: tuple[tuple[str, "ClassifierRule"], ...]

    @property
    def names(self) -> tuple[str, ...]:
        """Vertical names in config order (first = primary)."""
        return tuple(self.verticals)


class ClassifierRule(Protocol):
    """What the title classifier needs from a rule. Both kinds answer the same
    question, so neither has to imitate the other's return type."""

    def matches(self, title: str) -> bool: ...


class RegexRule:
    """A plain `pattern:` string, compiled case-insensitively."""

    def __init__(self, pattern: re.Pattern):
        self.pattern = pattern

    def matches(self, title: str) -> bool:
        return self.pattern.search(title) is not None

    def __repr__(self) -> str:
        return f"RegexRule({self.pattern.pattern!r})"


class CompoundRule:
    """A `pattern: {match, require_any}` block: the match string must appear,
    and so must at least one of require_any. Comparison is on a normalized form
    so "Co-Pilot", "co pilot" and a non-breaking space all read alike."""

    def __init__(self, match_str: str, require_any: tuple[str, ...]):
        self.match_str = self._normalize(match_str)
        self.require_any = [self._normalize(req) for req in require_any]

    @staticmethod
    def _normalize(text: str) -> str:
        return text.lower().replace("\xa0", " ").replace("-", " ")

    def matches(self, title: str) -> bool:
        title_norm = self._normalize(title)
        if self.match_str not in title_norm:
            return False
        return any(req in title_norm for req in self.require_any)

    def __repr__(self) -> str:
        return f"CompoundRule({self.match_str!r}, require_any={self.require_any!r})"

def _fail(path: Path, message: str) -> ValueError:
    return ValueError(f"{path}: {message}")


def _require(raw: dict, key: str, path: Path, where: str):
    if key not in raw:
        raise _fail(path, f"{where} is missing required key {key!r}")
    return raw[key]


def _str_list(value, path: Path, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(t, str) and t for t in value):
        raise _fail(path, f"{where} must be a list of nonempty strings")
    return tuple(value)


def _parse_vertical(name: str, raw: dict, path: Path) -> Vertical:
    where = f"verticals.{name}"
    if not _NAME_RE.match(name):
        raise _fail(path, f"vertical name {name!r} must match ^[a-z][a-z0-9_]*$")
    if not isinstance(raw, dict):
        raise _fail(path, f"{where} must be a mapping")
    search_terms = _str_list(_require(raw, "search_terms", path, where), path, f"{where}.search_terms")
    if not search_terms:
        raise _fail(path, f"{where}.search_terms must be nonempty")
    linkedin_terms = _str_list(_require(raw, "linkedin_terms", path, where), path, f"{where}.linkedin_terms")
    abroad_terms = _str_list(raw.get("abroad_terms", []), path, f"{where}.abroad_terms")

    drop_phrases = _str_list(raw.get("drop_phrases", []), path, f"{where}.drop_phrases")
    years = raw.get("years") or {}
    if not isinstance(years, dict) or not all(
            type(k) is int and type(v) is int and k >= 0 and 0 <= v <= 20
            for k, v in years.items()):
        raise _fail(path, f"{where}.years must map whole years to a seniority cap 0-20")
    skill_weights = raw.get("skill_weights") or {}
    if not isinstance(skill_weights, dict):
        raise _fail(path, f"{where}.skill_weights must be a mapping")

    # Title include-gate: opt-in per vertical. When title_include_terms is
    # empty the gate is OFF and apply_title_exclusion falls back to
    # exclusion-only. See normalize.title_passes.
    # Blank terms are rejected, not skipped: a blank compiles to a pattern
    # that matches every title, which silently empties a whole vertical.
    title_terms = {}
    for key in ("title_exclude_terms", "title_include_terms",
                "title_strong_keep_terms"):
        terms = raw.get(key, [])
        if not isinstance(terms, list) or not all(isinstance(t, str) for t in terms):
            raise _fail(path, f"{where}.{key} must be a list of strings")
        if any(not t.strip() for t in terms):
            raise _fail(path, f"{where}.{key} contains a blank term")
        title_terms[key] = terms
    exclude_terms = title_terms["title_exclude_terms"]
    include_terms = title_terms["title_include_terms"]
    strong_keep_terms = title_terms["title_strong_keep_terms"]

    # Optional to the loader so a lane can scrape before its resume shape
    # exists (/new-vertical pass-a); verticals-check requires it.
    resume = _parse_lane_resume(raw["resume"], path, f"{where}.resume") if "resume" in raw else None

    return Vertical(
        name=name,
        display_name=str(_require(raw, "display_name", path, where)),
        search_terms=search_terms,
        linkedin_terms=linkedin_terms,
        abroad_terms=abroad_terms,
        title_exclude_terms=tuple(exclude_terms),
        title_include_terms=tuple(include_terms),
        title_strong_keep_terms=tuple(strong_keep_terms),
        resume=resume,
        drop_phrases=drop_phrases,
        years=dict(sorted(years.items())),
        skill_weights=skill_weights,
    )


def load_verticals(path: Path | None = None) -> VerticalsConfig:
    """Parse and validate the vertical registry. Raises FileNotFoundError
    (with a pointer at the committed template) if the file is missing, and
    ValueError on any schema violation. Never falls back silently."""
    p = path or DEFAULT_CONFIG_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"{p} not found — verticals are user data. Copy "
            f"{EXAMPLE_CONFIG_PATH} to {DEFAULT_CONFIG_PATH} (and the "
            f"profile/verticals/example_* dirs to profile/verticals/<name>/) "
            f"and edit."
        )
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise _fail(p, "top level must be a mapping")
    if data.get("schema_version") != _SCHEMA_VERSION:
        raise _fail(p, f"schema_version must be {_SCHEMA_VERSION}")

    raw_verticals = _require(data, "verticals", p, "top level")
    if not isinstance(raw_verticals, dict) or not raw_verticals:
        raise _fail(p, "verticals must be a nonempty mapping")
    verticals = {
        name: _parse_vertical(name, raw, p) for name, raw in raw_verticals.items()
    }

    default_vertical = _require(data, "default_vertical", p, "top level")
    if default_vertical not in verticals:
        raise _fail(p, f"default_vertical {default_vertical!r} is not a configured vertical")

    raw_rules = _require(data, "classifier_rules", p, "top level")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise _fail(p, "classifier_rules must be a nonempty ordered list")
    rules = []
    for i, rule in enumerate(raw_rules):
        where = f"classifier_rules[{i}]"
        if not isinstance(rule, dict):
            raise _fail(p, f"{where} must be a mapping")
        vertical = _require(rule, "vertical", p, where)
        if vertical not in verticals:
            raise _fail(p, f"{where}.vertical {vertical!r} is not a configured vertical")
        pattern = _require(rule, "pattern", p, where)
        if isinstance(pattern, dict):
            match_str = _require(pattern, "match", p, f"{where}.pattern")
            if not isinstance(match_str, str):
                raise _fail(p, f"{where}.pattern.match must be a string")
            require_any = _str_list(pattern.get("require_any", []), p, f"{where}.pattern.require_any")
            if not require_any:
                raise _fail(p, f"{where}.pattern.require_any must be nonempty")
            rules.append((vertical, CompoundRule(match_str, tuple(require_any))))
        else:
            try:
                compiled = re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise _fail(p, f"{where}.pattern does not compile: {exc}") from exc
            rules.append((vertical, RegexRule(compiled)))

    return VerticalsConfig(
        default_vertical=default_vertical,
        verticals=verticals,
        classifier_rules=tuple(rules),
    )


_config: VerticalsConfig | None = None


def get_config() -> VerticalsConfig:
    """Lazy singleton over DEFAULT_CONFIG_PATH. Tests inject via set_config."""
    global _config
    if _config is None:
        _config = load_verticals()
    return _config


def set_config(cfg: VerticalsConfig | None) -> None:
    """Inject a config (tests), or None to reset to the lazy default."""
    global _config
    _config = cfg


def main() -> int:
    try:
        cfg = load_verticals()
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1
    missing = [
        str(VERTICALS_DIR / name / fname)
        for name in cfg.names
        for fname in ("rubric.md", "tailoring.md")
        if not (VERTICALS_DIR / name / fname).is_file()
    ]
    if missing:
        print("ERROR: missing per-vertical prose files:")
        for m in missing:
            print(f"  {m}")
        return 1
    no_shape = [n for n, v in cfg.verticals.items() if v.resume is None]
    if no_shape:
        print("ERROR: lane(s) with no `resume:` block in profile/verticals.yaml:")
        for n in no_shape:
            print(f"  {n}")
        return 1
    from src import resume_model
    try:
        problems = resume_model.check_sources(resume_model.load_sources(), cfg)
    except (resume_model.SourceError, ValueError) as exc:
        problems = [str(exc)]
    if problems:
        print(f"ERROR: {len(problems)} problem(s) in the resume sources:")
        for m in problems:
            print(f"  {m}")
        return 1
    print(f"verticals={','.join(cfg.names)} default={cfg.default_vertical}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
