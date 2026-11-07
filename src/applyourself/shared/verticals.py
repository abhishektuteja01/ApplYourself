"""Vertical registry loader — the single source of truth for vertical
definitions.

Deterministic config loading only (no LLM calls in src/).
`profile/verticals.yaml` is gitignored user data: the lane order (`lanes:`),
`defaults:` and `classifier_rules`. Each lane's block is
`verticals/<lane>/lane.yaml` beside that file, merged over `defaults:`. A
missing file FAILS LOUD with a pointer at the committed template — never a
silent empty fallback.

Consumers (discovery, cleaning, score_filter, score_store) must call
`get_config()` inside function bodies — never at module level, never
captured into module constants — so test injection via `set_config()`
always wins regardless of import order.

CLI: `uv run python -m applyourself.shared.verticals` validates the config, checks each
vertical's `profile/verticals/<name>/{rubric.md, tailoring.md}` exist and its
`resume:` block is set, runs the resume source check, and prints
`verticals=<names> default=<name>`. Slash commands use it as their
one-line prerequisite check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from applyourself.shared import paths, title_terms
from applyourself.shared.config_schema import Lane, VerticalsFile, validate
from applyourself.shared.logs import setup_logging

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
Variants = title_terms.Variants
_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class LaneResume:
    """A lane's resume shape (verticals.yaml `resume:`). No ids: bullets say
    which lanes pick them by default, skills say which line they show on."""

    sections: tuple[str, ...]  # a permutation of summary/work/projects/education/skills
    bullets: tuple[int, int]  # total (min, max)
    skill_lines: tuple[str, ...]  # line labels, in order
    summary: str  # the baseline summary


_RESUME_SECTIONS = {"summary", "work", "projects", "education", "skills"}


def _parse_lane_resume(raw: Any, path: Path, where: str) -> LaneResume:
    if not isinstance(raw, dict):
        raise _fail(path, f"{where} must be a mapping")
    sections = _str_list(_require(raw, "sections", path, where), path, f"{where}.sections")
    if sorted(sections) != sorted(_RESUME_SECTIONS):
        raise _fail(path, f"{where}.sections must list each of {sorted(_RESUME_SECTIONS)} once")
    bullets = _require(raw, "bullets", path, where)
    if not (
        isinstance(bullets, list)
        and len(bullets) == 2
        and all(type(x) is int for x in bullets)
        and 1 <= bullets[0] <= bullets[1]
    ):
        raise _fail(path, f"{where}.bullets must be [min, max]")
    skill_lines = _str_list(_require(raw, "skill_lines", path, where), path, f"{where}.skill_lines")
    summary = _require(raw, "summary", path, where)
    if not isinstance(summary, str) or not summary.strip():
        raise _fail(path, f"{where}.summary must be a nonempty string")
    return LaneResume(
        sections=sections,
        bullets=(bullets[0], bullets[1]),
        skill_lines=skill_lines,
        summary=" ".join(summary.split()),
    )


LEVELS = ("entry", "mid", "senior", "lead")
# Used where neither the lane nor `defaults:` sets a `score:` key.
SCORE_DEFAULTS: dict[str, Any] = {
    "level": {"entry": 0, "mid": 5, "senior": 15, "lead": 25},
    "years": {3: 5, 4: 10, 5: 15, 6: 20, 7: 25, 8: 30, 9: 35},
    "years_zero_at": 10,
}


@dataclass(frozen=True)
class ScoreRules:
    """A lane's `score:` block: what /score takes off the judge's fit. The
    score is fit minus the larger of the level and the years penalty, and 0
    when the JD asks `years_zero_at` years or more. `cutoff` is the sheet and
    retention line; None reads scoring.yaml's older `cutoffs.sheet`."""

    level: dict[str, int]
    years: dict[int, int]
    years_zero_at: int
    cutoff: int | None = None

    def penalty(self, level: str, years: int) -> int:
        """The larger of the level's penalty and that of the largest `years`
        key at or below `years`."""
        keys = [k for k in self.years if k <= years]
        return max(self.level[level], self.years[max(keys)] if keys else 0)


def _score_int(value: Any, path: Path, where: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise _fail(path, f"{where} must be a whole number {low}-{high}")
    return value


def _parse_score(raw: Any, path: Path, where: str) -> ScoreRules:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _fail(path, f"{where} must be a mapping")
    got = {**SCORE_DEFAULTS, **raw}
    level = got["level"]
    if not isinstance(level, dict) or sorted(level) != sorted(LEVELS):
        raise _fail(path, f"{where}.level must set each of {', '.join(LEVELS)}")
    years = got["years"]
    if not isinstance(years, dict) or not all(type(k) is int and k >= 0 for k in years):
        raise _fail(path, f"{where}.years must map whole years to a penalty")
    cutoff = got.get("cutoff")
    return ScoreRules(
        level={k: _score_int(level[k], path, f"{where}.level.{k}", 0, 100) for k in LEVELS},
        years={
            k: _score_int(v, path, f"{where}.years.{k}", 0, 100) for k, v in sorted(years.items())
        },
        years_zero_at=_score_int(got["years_zero_at"], path, f"{where}.years_zero_at", 1, 50),
        cutoff=None if cutoff is None else _score_int(cutoff, path, f"{where}.cutoff", 0, 100),
    )


@dataclass(frozen=True)
class Vertical:
    name: str
    display_name: str
    # The lane's one search list, for every searching source. `linkedin_terms`
    # and `abroad_terms` are optional overrides; absent, they are search_terms.
    search_terms: tuple[str, ...]
    linkedin_terms: tuple[str, ...]
    # The title ban list, `defaults:` merged in. The lane's own include and
    # strong-keep lists are legacy (`config migrate` moves them out).
    title_exclude_terms: tuple[str, ...]
    title_include_terms: tuple[str, ...]
    title_strong_keep_terms: tuple[str, ...]
    resume: LaneResume | None = None
    # The terms a jobspy lane's `abroad` block queries per country.
    abroad_terms: tuple[str, ...] = ()
    # `drop_phrases` is still read but drops nothing.
    drop_phrases: tuple[str, ...] = ()
    score: ScoreRules = field(default_factory=lambda: _parse_score({}, Path(), "score"))


@dataclass(frozen=True)
class VerticalsConfig:
    default_vertical: str
    verticals: dict[str, Vertical]  # insertion order == config order
    classifier_rules: tuple[tuple[str, ClassifierRule], ...]
    # Groups of words a title may spell either way, for every loose term match.
    title_variants: Variants = ()

    @property
    def names(self) -> tuple[str, ...]:
        """Vertical names in config order (first = primary)."""
        return tuple(self.verticals)


class ClassifierRule(Protocol):
    """What the title classifier needs from a rule. Every kind answers the
    same question, so none has to imitate another's return type. `hinted`:
    the title came from a search for this rule's lane."""

    def matches(self, title: str, hinted: bool = False) -> bool: ...


class RegexRule:
    """A plain `pattern:` string, compiled case-insensitively."""

    def __init__(self, pattern: re.Pattern[str]):
        self.pattern = pattern

    def matches(self, title: str, hinted: bool = False) -> bool:
        return self.pattern.search(title) is not None

    def __repr__(self) -> str:
        return f"RegexRule({self.pattern.pattern!r})"


class CompoundRule:
    """A `pattern: {match, require_any}` block: a match term (one, or any of
    a list) must appear, and so must one of require_any somewhere else in the
    title, each matched loosely (`title_terms`; a short last require word may
    run on in a compound, "LLM" in "LLMOps"). With `search_is_context`, on a
    title a search for this lane found the match term alone is enough."""

    def __init__(
        self,
        match_str: str | tuple[str, ...],
        require_any: tuple[str, ...],
        variants: Variants = (),
        search_is_context: bool = False,
    ):
        self.match = (match_str,) if isinstance(match_str, str) else tuple(match_str)
        self.require_any = require_any
        self.variants = variants
        self.search_is_context = search_is_context

    def matches(self, title: str, hinted: bool = False) -> bool:
        text = title_terms.normalize(title)
        must = title_terms.compile_terms(self.match, self.variants)
        hits = [(m.span(), m.group(0).lower()) for m in must.finditer(text)] if must else []
        if not hits or (hinted and self.search_is_context):
            return bool(hits)
        anyof = title_terms.compile_terms(self.require_any, self.variants, open_end=True)
        if anyof is None:
            return False
        # A require hit counts beside a match hit it does not overlap and that
        # reads differently: "AI ML Engineer" passes, "ML ML" and "Agents" alone do not.
        return any(
            (m.end() <= a or m.start() >= b) and m.group(0).lower() != word
            for m in anyof.finditer(text)
            for (a, b), word in hits
        )

    def __repr__(self) -> str:
        match = self.match[0] if len(self.match) == 1 else list(self.match)
        return f"CompoundRule({match!r}, require_any={list(self.require_any)!r})"


class TermsRule:
    """A `terms: [...]` rule: any one term, matched loosely (`title_terms`).
    With `keep: true` a title it matches passes even when it holds a ban word."""

    def __init__(self, terms: tuple[str, ...], variants: Variants = (), keep: bool = False):
        self.terms = terms
        self.variants = variants
        self.keep = keep

    def matches(self, title: str, hinted: bool = False) -> bool:
        rx = title_terms.compile_terms(self.terms, self.variants)
        return bool(rx and rx.search(title_terms.normalize(title)))

    def __repr__(self) -> str:
        return f"TermsRule({list(self.terms)!r}{', keep=True' if self.keep else ''})"


def _fail(path: Path, message: str) -> ValueError:
    return ValueError(f"{path}: {message}")


def _require(raw: dict[str, Any], key: str, path: Path, where: str) -> Any:
    if key not in raw:
        raise _fail(path, f"{where} is missing required key {key!r}")
    return raw[key]


def _str_list(value: Any, path: Path, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(t, str) and t for t in value):
        raise _fail(path, f"{where} must be a list of nonempty strings")
    return tuple(value)


def _parse_vertical(name: str, raw: dict[str, Any], path: Path) -> Vertical:
    where = f"verticals.{name}"
    if not _NAME_RE.match(name):
        raise _fail(path, f"vertical name {name!r} must match ^[a-z][a-z0-9_]*$")
    if not isinstance(raw, dict):
        raise _fail(path, f"{where} must be a mapping")
    search_terms = _str_list(
        _require(raw, "search_terms", path, where), path, f"{where}.search_terms"
    )
    if not search_terms:
        raise _fail(path, f"{where}.search_terms must be nonempty")
    linkedin_terms, abroad_terms = (
        _str_list(raw[key], path, f"{where}.{key}") if key in raw else search_terms
        for key in ("linkedin_terms", "abroad_terms")
    )
    if not linkedin_terms:
        raise _fail(path, f"{where}.linkedin_terms is empty: leave it out to use search_terms")

    drop_phrases = _str_list(raw.get("drop_phrases", []), path, f"{where}.drop_phrases")
    # `years`, `skill_weights` and `focus` are the old judge's: read, never used.
    score = _parse_score(raw.get("score"), path, f"{where}.score")

    # The title lists (title_gate.lane_for). Blank terms are rejected, not
    # skipped: a blank ban term would match every title.
    title_lists = {}
    for key in ("title_exclude_terms", "title_include_terms", "title_strong_keep_terms"):
        terms = raw.get(key, [])
        if not isinstance(terms, list) or not all(isinstance(t, str) for t in terms):
            raise _fail(path, f"{where}.{key} must be a list of strings")
        if any(not t.strip() for t in terms):
            raise _fail(path, f"{where}.{key} contains a blank term")
        title_lists[key] = terms
    exclude_terms = title_lists["title_exclude_terms"]
    include_terms = title_lists["title_include_terms"]
    strong_keep_terms = title_lists["title_strong_keep_terms"]

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
        score=score,
    )


def lane_file(config: Path, name: str) -> Path:
    """Where lane `name` of the verticals.yaml at `config` lives."""
    return config.parent / "verticals" / name / "lane.yaml"


def merge_defaults(defaults: dict[str, Any], lane: dict[str, Any]) -> dict[str, Any]:
    """lane over defaults: a list is the lane's then the default's; `score`
    merges key by key; any other key the lane sets replaces the default's."""
    out = dict(defaults)
    for key, value in lane.items():
        base = out.get(key)
        if isinstance(base, list) and isinstance(value, list):
            out[key] = value + base
        elif key == "score" and isinstance(base, dict) and isinstance(value, dict):
            out[key] = {**base, **value}
        else:
            out[key] = value
    return out


def load_verticals(path: Path | None = None) -> VerticalsConfig:
    """Parse and validate the vertical registry. Raises FileNotFoundError
    (with a pointer at the committed template) if the file is missing, and
    ValueError on any schema violation. Never falls back silently."""
    p = path or paths.VERTICALS_CONFIG
    if not p.exists():
        raise FileNotFoundError(
            f"{p} not found — verticals are user data. Copy "
            f"{paths.example('verticals.example.yaml')} to {paths.VERTICALS_CONFIG} (and the "
            f"profile/verticals/example_* dirs to profile/verticals/<name>/) "
            f"and edit."
        )
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise _fail(p, "top level must be a mapping")
    if data.get("schema_version") == 1 and "verticals" in data:
        raise _fail(
            p,
            "schema_version 1 holds every lane inline; each lane moved to "
            "verticals/<lane>/lane.yaml beside this file. Run "
            "`uv run config migrate --dry-run`, then --apply",
        )
    if data.get("schema_version") != _SCHEMA_VERSION:
        raise _fail(p, f"schema_version must be {_SCHEMA_VERSION}")
    validate(VerticalsFile, data, p)

    names = _str_list(_require(data, "lanes", p, "top level"), p, "lanes")
    if not names:
        raise _fail(p, "lanes must be a nonempty list")
    if len(set(names)) != len(names):
        raise _fail(p, "lanes lists a lane twice")
    defaults = data.get("defaults") or {}
    lanes: dict[str, tuple[Any, Path]] = {}
    for name in names:
        if not _NAME_RE.match(name):
            raise _fail(p, f"vertical name {name!r} must match ^[a-z][a-z0-9_]*$")
        lp = lane_file(p, name)
        if not lp.is_file():
            raise _fail(p, f"lanes: {name} has no {lp}")
        raw = yaml.safe_load(lp.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise _fail(lp, "top level must be a mapping")
        validate(Lane, raw, lp)
        lanes[name] = (merge_defaults(defaults, raw), lp)
    return build(data, lanes, p)


def build(data: dict[str, Any], lanes: dict[str, tuple[Any, Path]], p: Path) -> VerticalsConfig:
    """The config from the top-level file's data and each lane's merged
    block, with the file it came from."""
    verticals = {name: _parse_vertical(name, raw, lp) for name, (raw, lp) in lanes.items()}

    default_vertical = _require(data, "default_vertical", p, "top level")
    if default_vertical not in verticals:
        raise _fail(p, f"default_vertical {default_vertical!r} is not a configured vertical")

    variants = _variants(data.get("title_variants"), p)
    raw_rules = _require(data, "classifier_rules", p, "top level")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise _fail(p, "classifier_rules must be a nonempty ordered list")
    rules: list[tuple[str, ClassifierRule]] = []
    for i, rule in enumerate(raw_rules):
        where = f"classifier_rules[{i}]"
        if not isinstance(rule, dict):
            raise _fail(p, f"{where} must be a mapping")
        vertical = _require(rule, "vertical", p, where)
        if vertical not in verticals:
            raise _fail(p, f"{where}.vertical {vertical!r} is not a configured vertical")
        if ("pattern" in rule) == ("terms" in rule):
            raise _fail(p, f"{where} needs exactly one of `pattern` or `terms`")
        if "terms" in rule:
            terms = _str_list(rule["terms"], p, f"{where}.terms")
            if not terms or any(not t.strip() for t in terms):
                raise _fail(p, f"{where}.terms must be a nonempty list of nonblank terms")
            keep = rule.get("keep", False)
            if type(keep) is not bool:
                raise _fail(p, f"{where}.keep must be true or false")
            rules.append((vertical, TermsRule(terms, variants, keep)))
            continue
        if "keep" in rule:
            raise _fail(p, f"{where}.keep goes only on a `terms` rule")
        pattern = rule["pattern"]
        if isinstance(pattern, dict):
            match_str = _require(pattern, "match", p, f"{where}.pattern")
            if isinstance(match_str, list):
                match_str = _str_list(match_str, p, f"{where}.pattern.match")
            if not match_str or (
                any(not m.strip() for m in match_str)
                if isinstance(match_str, tuple)
                else not isinstance(match_str, str) or not match_str.strip()
            ):
                raise _fail(p, f"{where}.pattern.match must be a nonblank string or list")
            require_any = _str_list(
                pattern.get("require_any", []), p, f"{where}.pattern.require_any"
            )
            if not require_any:
                raise _fail(p, f"{where}.pattern.require_any must be nonempty")
            context = pattern.get("search_is_context", False)
            if type(context) is not bool:
                raise _fail(p, f"{where}.pattern.search_is_context must be true or false")
            rules.append((vertical, CompoundRule(match_str, tuple(require_any), variants, context)))
        else:
            try:
                compiled = re.compile(pattern, re.IGNORECASE)
            except (re.error, TypeError) as exc:
                raise _fail(p, f"{where}.pattern does not compile: {exc}") from exc
            rules.append((vertical, RegexRule(compiled)))

    return VerticalsConfig(
        default_vertical=default_vertical,
        verticals=verticals,
        classifier_rules=tuple(rules),
        title_variants=variants,
    )


def _variants(raw: Any, p: Path) -> Variants:
    """`title_variants`: groups of two or more ways to spell one title word."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _fail(p, "title_variants must be a list of groups")
    out = []
    for i, group in enumerate(raw):
        terms = _str_list(group, p, f"title_variants[{i}]")
        if len(terms) < 2 or any(not t.strip() for t in terms):
            raise _fail(p, f"title_variants[{i}] must list two or more nonblank spellings")
        out.append(terms)
    return tuple(out)


_config: VerticalsConfig | None = None


def get_config() -> VerticalsConfig:
    """Lazy singleton over profile/verticals.yaml. Tests inject via set_config."""
    global _config
    if _config is None:
        _config = load_verticals()
    return _config


def set_config(cfg: VerticalsConfig | None) -> None:
    """Inject a config (tests), or None to reset to the lazy default."""
    global _config
    _config = cfg


def main() -> int:
    setup_logging()
    try:
        cfg = load_verticals()
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1
    missing = [
        str(paths.VERTICALS_DIR / name / fname)
        for name in cfg.names
        for fname in ("rubric.md", "tailoring.md")
        if not (paths.VERTICALS_DIR / name / fname).is_file()
    ]
    if missing:
        print("ERROR: missing per-vertical prose files:")
        for m in missing:
            print(f"  {m}")
        return 1
    no_shape = [n for n, v in cfg.verticals.items() if v.resume is None]
    if no_shape:
        print("ERROR: lane(s) with no `resume:` block in profile/verticals/<lane>/lane.yaml:")
        for n in no_shape:
            print(f"  {n}")
        return 1
    from applyourself import resume_model

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
