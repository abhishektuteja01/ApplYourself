"""Vertical registry loader — the single source of truth for vertical
definitions.

Deterministic config loading only (no LLM calls in src/).
`profile/verticals.yaml` is gitignored user data; a missing file FAILS LOUD
with a pointer at the committed template — never a
silent empty fallback, unlike lint rules.

Consumers (discovery, cleaning, scoring_io, score_cli) must call
`get_config()` inside function bodies — never at module level, never
captured into module constants — so test injection via `set_config()`
always wins regardless of import order.

CLI: `uv run python -m src.verticals` validates the config, checks each
vertical's `profile/verticals/<name>/{rubric.md, tailoring.md}` exist, and
prints `verticals=<names> default=<name>`. Slash commands use it as their
one-line prerequisite check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "profile" / "verticals.yaml"
EXAMPLE_CONFIG_PATH = REPO_ROOT / "profile" / "verticals.example.yaml"
VERTICALS_DIR = REPO_ROOT / "profile" / "verticals"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Vertical:
    name: str
    display_name: str
    search_terms: tuple[str, ...]
    linkedin_terms: tuple[str, ...]
    skill_weights: dict  # opaque to src/ — consumed only by LLM judges
    disqualifier_max_years: int
    disqualifier_phrases: tuple[str, ...]
    disqualifier_title_phrases: tuple[str, ...]
    disqualifier_scored_by: str
    reasoning_years: str
    reasoning_phrase: str | None
    reasoning_title: str | None
    title_exclude_terms: tuple[str, ...]
    title_include_terms: tuple[str, ...]
    title_strong_keep_terms: tuple[str, ...]
    resume_file: str  # repo-relative path to the attested resume LLM judges score against (§6.3 / score.md J1)


@dataclass(frozen=True)
class VerticalsConfig:
    default_vertical: str
    verticals: dict[str, Vertical]  # insertion order == config order
    classifier_rules: tuple[tuple[str, re.Pattern], ...]
    out_of_lane_reasoning: str

    @property
    def names(self) -> tuple[str, ...]:
        """Vertical names in config order (first = primary)."""
        return tuple(self.verticals)

    @property
    def valid_verticals(self) -> frozenset[str]:
        """Configured names plus "" (unclassified/out-of-lane)."""
        return frozenset(self.verticals) | {""}


class CompoundRule:
    def __init__(self, match_str: str, require_any: tuple[str, ...]):
        self.match_str = self._normalize(match_str)
        self.require_any = [self._normalize(req) for req in require_any]

    @staticmethod
    def _normalize(text: str) -> str:
        return text.lower().replace("\xa0", " ").replace("-", " ")

    def search(self, text: str):
        text_norm = self._normalize(text)
        if self.match_str not in text_norm:
            return None
        if any(req in text_norm for req in self.require_any):
            return True  # just needs to be truthy for `if pattern.search(title):`
        return None

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

    dq = _require(raw, "disqualifier", path, where)
    if not isinstance(dq, dict):
        raise _fail(path, f"{where}.disqualifier must be a mapping")
    max_years = _require(dq, "max_years", path, f"{where}.disqualifier")
    if not isinstance(max_years, int):
        raise _fail(path, f"{where}.disqualifier.max_years must be an integer")
    phrases = _str_list(dq.get("phrases", []), path, f"{where}.disqualifier.phrases")
    title_phrases = _str_list(dq.get("title_phrases", []), path, f"{where}.disqualifier.title_phrases")
    scored_by = _require(dq, "scored_by", path, f"{where}.disqualifier")
    reasoning_years = _require(dq, "reasoning_years", path, f"{where}.disqualifier")
    reasoning_phrase = dq.get("reasoning_phrase")
    if phrases and not reasoning_phrase:
        raise _fail(path, f"{where}.disqualifier.reasoning_phrase is required when phrases is nonempty")
    reasoning_title = dq.get("reasoning_title")
    if title_phrases and not reasoning_title:
        raise _fail(path, f"{where}.disqualifier.reasoning_title is required when title_phrases is nonempty")
    
    exclude_terms = raw.get("title_exclude_terms", [])
    if not isinstance(exclude_terms, list) or not all(isinstance(t, str) for t in exclude_terms):
        raise _fail(path, f"{where}.title_exclude_terms must be a list of strings")

    # Title include-gate (2026-07-21): opt-in per vertical. When
    # title_include_terms is empty the gate is OFF and apply_title_exclusion
    # falls back to exclusion-only (historical behavior). See cleaning.py.
    include_terms = raw.get("title_include_terms", [])
    if not isinstance(include_terms, list) or not all(isinstance(t, str) for t in include_terms):
        raise _fail(path, f"{where}.title_include_terms must be a list of strings")
    strong_keep_terms = raw.get("title_strong_keep_terms", [])
    if not isinstance(strong_keep_terms, list) or not all(isinstance(t, str) for t in strong_keep_terms):
        raise _fail(path, f"{where}.title_strong_keep_terms must be a list of strings")

    # Per-vertical scoring resume: the attested resume LLM judges read in
    # score.md Step J1 (also the tailoring frozen-section/summary source).
    # Required: a shared default silently scored a lane against another
    # lane's resume. Existence is enforced in main().
    resume_file = raw.get("resume_file")
    if not isinstance(resume_file, str) or not resume_file.strip():
        raise _fail(path, f"{where}.resume_file must be a nonempty string")

    return Vertical(
        name=name,
        display_name=str(_require(raw, "display_name", path, where)),
        search_terms=search_terms,
        linkedin_terms=linkedin_terms,
        skill_weights=raw.get("skill_weights") or {},
        disqualifier_max_years=max_years,
        disqualifier_phrases=phrases,
        disqualifier_title_phrases=title_phrases,
        disqualifier_scored_by=str(scored_by),
        reasoning_years=str(reasoning_years),
        reasoning_phrase=str(reasoning_phrase) if reasoning_phrase else None,
        reasoning_title=str(reasoning_title) if reasoning_title else None,
        title_exclude_terms=tuple(exclude_terms),
        title_include_terms=tuple(include_terms),
        title_strong_keep_terms=tuple(strong_keep_terms),
        resume_file=resume_file.strip(),
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
    data = yaml.safe_load(p.read_text())
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

    out_of_lane = _require(data, "out_of_lane", p, "top level")
    if not isinstance(out_of_lane, dict) or not out_of_lane.get("reasoning"):
        raise _fail(p, "out_of_lane.reasoning is required")

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
            rules.append((vertical, compiled))

    return VerticalsConfig(
        default_vertical=default_vertical,
        verticals=verticals,
        classifier_rules=tuple(rules),
        out_of_lane_reasoning=str(out_of_lane["reasoning"]),
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
    missing_resumes = sorted({
        v.resume_file
        for v in cfg.verticals.values()
        if not (REPO_ROOT / v.resume_file).is_file()
    })
    if missing_resumes:
        print("ERROR: missing per-vertical scoring resume files (score.md J1):")
        for m in missing_resumes:
            print(f"  {m}")
        return 1
    print(f"verticals={','.join(cfg.names)} default={cfg.default_vertical}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
