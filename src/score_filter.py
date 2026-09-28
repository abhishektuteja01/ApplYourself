"""Every rule /score applies: the drops before judging, the JD trim, the
seniority cap, the country rule and the sponsorship label.

Pure functions, driven by profile/scoring.yaml (shared policy), the answer
bank (sponsorship needs) and profile/verticals.yaml (per-lane `drop_phrases`
and `years`). No LLM (R7). The judge reads facts; it never re-checks a rule.
"""
from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from src import paths
from src import verticals

SCORING_PATH = paths.PROFILE / "scoring.yaml"
_SCHEMA_VERSION = 1


SPONSORSHIP_NEEDS = ("never", "later", "now")
# What a JD says about sponsorship, as the judge reports it. derive() turns it
# into the stored label with sponsorship_label().
SPONSORSHIP_FACTS = ("offers", "no_sponsorship", "no_future_sponsorship",
                     "no_opt_support", "status_bar", "right_to_work", "silent")


@dataclass(frozen=True)
class ScoringConfig:
    sponsorship_need: str
    """Your need in your own country: never, later or now."""
    country_needs: tuple[tuple[str, str], ...]
    """(country, need) for every country the answer bank lists."""
    default_need: str
    """The need everywhere the bank does not list."""
    ineligible: tuple[str, ...]
    no_sponsorship: tuple[str, ...]
    min_words: int
    skills_cap: int
    keywords_max: int
    tailor_min: int
    review_min: int
    trim_headings: tuple[str, ...]
    keep_words: tuple[str, ...]
    top_n: int
    pipeline_states: tuple[str, ...]
    pipeline_show: int
    sponsorship_order: tuple[str, ...]
    note: str


def _phrases(data: dict, key: str, path: Path) -> tuple[str, ...]:
    value = data.get(key) or []
    if not isinstance(value, list) or not all(isinstance(p, str) and p.strip() for p in value):
        raise ValueError(f"{path}: {key} must be a list of nonempty strings")
    return tuple(value)


def _int(block: dict, key: str, where: str, path: Path) -> int:
    value = block.get(key)
    if type(value) is not int or value < 0:
        raise ValueError(f"{path}: {where}.{key} must be a non-negative integer")
    return value


def _need(facts) -> str:
    if facts.authorized and not facts.requires_sponsorship:
        return "never"
    return "later" if facts.authorized else "now"


def sponsorship_needs(bank_path: Path) -> tuple[str, tuple[tuple[str, str], ...], str]:
    """(home need, per-country needs, default need), from the answer bank's
    work-authorization table — the one place those facts are stated."""
    from src.apply.answers import load_answers

    bank = load_answers(bank_path)
    rows = tuple((c, _need(f)) for c, f in bank.countries.items() if c != "default")
    return _need(bank.facts_for(bank.home_country)), rows, _need(bank.countries["default"])


def load_scoring(path: Path | None = None, bank_path: Path | None = None) -> ScoringConfig:
    """Parse and validate profile/scoring.yaml. Missing file or bad key fails loud.

    Your sponsorship need comes from the answer bank (`bank_path`, default
    profile/application_answers.yaml; the example bank for the example scoring
    file), never from scoring.yaml."""
    path = path or SCORING_PATH
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — copy profile/scoring.example.yaml "
                                "to profile/scoring.yaml (/onboarding does this)")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if data.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError(f"{path}: schema_version must be {_SCHEMA_VERSION}")
    if bank_path is None:
        from src.apply import answers as bank_module
        bank_path = (bank_module.EXAMPLE_PATH if path.name.startswith("scoring.example")
                     else bank_module.DEFAULT_PATH)
    home_need, country_needs, default_need = sponsorship_needs(Path(bank_path))
    q, a = data.get("jd_quality") or {}, data.get("action") or {}
    t, s = data.get("trim_jd") or {}, data.get("shortlist") or {}
    cfg = ScoringConfig(
        sponsorship_need=home_need,
        country_needs=country_needs,
        default_need=default_need,
        ineligible=_phrases(data, "ineligible", path),
        no_sponsorship=_phrases(data, "no_sponsorship", path),
        min_words=_int(q, "min_words", "jd_quality", path),
        skills_cap=_int(q, "skills_cap", "jd_quality", path),
        keywords_max=_int(data, "keywords_max", "top level", path),
        tailor_min=_int(a, "tailor_min", "action", path),
        review_min=_int(a, "review_min", "action", path),
        trim_headings=_phrases(t, "headings", path),
        keep_words=_phrases(t, "keep_words", path),
        top_n=_int(s, "top_n", "shortlist", path),
        pipeline_states=_phrases(s, "pipeline_states", path),
        pipeline_show=_int(s, "pipeline_show", "shortlist", path),
        sponsorship_order=_phrases(s, "sponsorship_order", path),
        note=str(s.get("note") or ""),
    )
    if cfg.review_min > cfg.tailor_min:
        raise ValueError(f"{path}: action.review_min must not exceed action.tailor_min")
    return cfg


# ---------------------------------------------------------------------
# Text normalization and phrase matching
# ---------------------------------------------------------------------

_US_RE = re.compile(r"\bu\.\s?s\.(?:\s?a\.)?|\bunited states(?: of america)?\b|\busa\b")


def normalize(text: Any) -> str:
    """Lowercase, drop markdown escapes and bold markers, read "U.S." and
    "United States" as "us", collapse whitespace. "" for non-strings."""
    if not isinstance(text, str):
        return ""
    t = text.lower().replace("\\", "").replace("**", "")
    t = _US_RE.sub("us", t)
    return re.sub(r"\s+", " ", t).strip()


@lru_cache(maxsize=None)
def _phrase_re(phrases: tuple[str, ...]) -> re.Pattern | None:
    if not phrases:
        return None
    alts = sorted({normalize(p) for p in phrases}, key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(" + "|".join(map(re.escape, alts)) + r")(?![a-z0-9])")


def find_phrase(phrases: tuple[str, ...], text: Any) -> str | None:
    """First configured phrase found in text as whole words, else None."""
    rx = _phrase_re(tuple(phrases))
    m = rx.search(normalize(text)) if rx else None
    return m.group(1) if m else None


# ---------------------------------------------------------------------
# JD facts
# ---------------------------------------------------------------------

# "5+ years", "3-5 years", "3 to 5 years" of experience. The captured number
# is the lower bound; "experience" must follow within four words.
_YEARS_RE = re.compile(
    r"(?<![\w.$])(\d{1,2})\s*(\+|\s*(?:-|–|—|to)\s*\d{1,2}\s*\+?)?\s*(?:years?|yrs?)\b"
    r"((?:\s+of)?(?:\s+[a-z][\w/&,-]*){0,4}?\s+experience)?"
)
# Company history, not a requirement: "with over 100 years of experience".
_COMPANY_AGE_RE = re.compile(r"(?:over|more than|nearly|almost|for)\s*$")
# Lines that open a nice-to-have section; years after one are not required.
_PREFERRED_RE = re.compile(
    r"^\W*(?:preferred|nice[- ]to[- ]have|bonus|desired|pluses|good to have)\b[^\n]{0,40}$",
    re.MULTILINE,
)
_MAX_PLAUSIBLE_YEARS = 20


def years_required(jd_text: Any) -> int:
    """Highest minimum-years requirement stated before any Preferred section,
    0 if none."""
    if not isinstance(jd_text, str):
        return 0
    text = jd_text.lower().replace("\\", "").replace("**", "")
    cut = _PREFERRED_RE.search(text)
    if cut:
        text = text[:cut.start()]
    found = []
    for m in _YEARS_RE.finditer(text):
        n = int(m.group(1))
        if n >= _MAX_PLAUSIBLE_YEARS or not m.group(3):
            continue
        before = text[max(0, m.start() - 25):m.start()]
        if _COMPANY_AGE_RE.search(before) or "education" in before:
            continue
        found.append(n)
    return max(found, default=0)


def word_count(jd_text: Any) -> int:
    return len(jd_text.split()) if isinstance(jd_text, str) else 0


_BULLET = ("-", "•", "·", "+", "* ")


def _heading_line(line: str) -> bool:
    """A marked heading: `# X`, `**X**` or `X:`, at most eight words."""
    t = line.strip()
    words = t.strip("#*_ :").split()
    return (0 < len(words) <= 8 and not t.startswith(_BULLET)
            and (t.startswith("#") or (t.startswith("**") and t.rstrip(":").endswith("**"))
                 or t.endswith(":")))


def _blocks(text: str) -> list[tuple[bool, str]]:
    """(starts with a heading, text) blocks: paragraphs, split again at every
    marked heading line. A lone short line with no end punctuation is a
    heading too."""
    out = []
    for par in re.split(r"\n\s*\n", text.strip()):
        lines = par.strip().splitlines()
        if len(lines) == 1 and not _heading_line(lines[0]):
            t = lines[0].strip()
            lone = (0 < len(t.split()) <= 8 and any(ch.isalpha() for ch in t)
                    and not t.startswith(_BULLET)
                    and not t.endswith((".", "!", "?", ",")))
            out.append((lone, t))
            continue
        cur: list[str] = []
        head = False
        for line in lines:
            if _heading_line(line) and cur:
                out.append((head, "\n".join(cur)))
                cur = []
            if not cur:
                head = _heading_line(line)
            cur.append(line)
        if cur:
            out.append((head, "\n".join(cur)))
    return out


def trim_jd(jd_text: Any, cfg: ScoringConfig) -> str:
    """The JD without its boilerplate sections (EEO, benefits, about-us...).
    A section starts at a heading matching trim_jd.headings and runs to the
    next heading. A block holding a keep word always stays."""
    if not isinstance(jd_text, str):
        return ""
    heads = tuple(h.lower() for h in cfg.trim_headings)
    keeps = tuple(k.lower() for k in cfg.keep_words)
    kept, cutting = [], False
    for is_head, block in _blocks(jd_text):
        starts_boiler = block.lstrip("#*_ ").lower().startswith(heads)
        if is_head:
            cutting = starts_boiler
        low = block.lower()
        if (cutting or starts_boiler) and not any(k in low for k in keeps):
            continue
        kept.append(block.strip())
    return "\n\n".join(kept)


def wrap(text: str, width: int = 100) -> str:
    """Hard-wrap each paragraph so no line is long enough to be cut by a reader."""
    return "\n\n".join(
        "\n".join(textwrap.fill(line, width) if line.strip() else "" for line in par.splitlines())
        for par in text.split("\n\n")
    )


# ---------------------------------------------------------------------
# The drop decision
# ---------------------------------------------------------------------

def drop_reason(row: dict, cfg: ScoringConfig) -> tuple[str, str] | None:
    """(code, detail) if the row is dropped before judging, else None.
    Codes, in order: no-lane, ineligible, phrase, years."""
    lane = verticals.get_config().verticals.get(row.get("vertical") or "")
    if lane is None:
        return "no-lane", "title matches no configured lane"
    jd = row.get("jd_text")
    # A bar is sometimes stated only in the title ("... - USC/GC only").
    seen = f"{row.get('title') or ''}\n{jd if isinstance(jd, str) else ''}"
    hit = find_phrase(cfg.ineligible, seen)
    if hit is None and cfg.sponsorship_need == "now":
        hit = find_phrase(cfg.no_sponsorship, seen)
    if hit is not None:
        return "ineligible", hit
    hit = find_phrase(lane.drop_phrases, jd)
    if hit is not None:
        return "phrase", hit
    years = years_required(jd)
    if lane.years and years > max(lane.years):
        return "years", f"{years}+ years required, lane allows {max(lane.years)}"
    return None


def seniority_cap(lane: verticals.Vertical, years: int) -> int | None:
    """The lane's seniority cap for a JD asking `years`, None if uncapped:
    the cap of the largest `years` key at or below it."""
    keys = [k for k in lane.years if k <= years]
    return lane.years[max(keys)] if keys else None


# ---------------------------------------------------------------------
# Rules over the facts a judge reads
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class CountryRule:
    """Where each lane may be scored. `home` countries allow every lane; a
    country in `abroad` allows only its listed lanes; any other is out."""
    home: frozenset[str]
    abroad: dict[str, frozenset[str]]

    @classmethod
    def from_discovery(cls, dcfg) -> "CountryRule":
        abroad = {normalize(c): frozenset(v) for c, v in dcfg.abroad_lanes().items()}
        allowed = {normalize(c) for c in dcfg.location_allowlist.effective_countries()}
        return cls(home=frozenset(allowed - set(abroad)), abroad=abroad)


def countries(value: Any) -> list[str]:
    """The judge's `country` field as normalized names; [] when unknown."""
    if not isinstance(value, str):
        return []
    return [normalize(c) for c in value.split(";") if normalize(c) not in ("", "remote")]


def country_allowed(value: Any, lane: str, rule: CountryRule) -> bool:
    """True when any listed country allows the lane, or none is known."""
    names = countries(value)
    return not names or any(c in rule.home or lane in rule.abroad.get(c, ()) for c in names)


_LABEL_RANK = {"ineligible": 3, "sponsors": 2, "opt_ok": 1, "unknown": 0}


def sponsorship_label(facts: list[str], country: Any, cfg: ScoringConfig) -> str:
    """The stored label for everything the JD says about sponsorship: the
    strictest outcome of any one statement. An offer of sponsorship cancels a
    right-to-work requirement."""
    facts = [f for f in facts if not (f == "right_to_work" and "offers" in facts)]
    return max((_fact_label(f, country, cfg) for f in facts or ["silent"]),
               key=_LABEL_RANK.__getitem__)


def need_in(country: Any, cfg: ScoringConfig) -> str:
    """Your sponsorship need for a role in `country` (the judge's field,
    ";"-separated): the least of the needs of the countries it lists, or your
    home need when none is known."""
    from src.apply.answers import canonical_place

    if not isinstance(country, str):
        return cfg.sponsorship_need
    table = dict(cfg.country_needs)
    names = [canonical_place(c) or c.strip() for c in country.split(";")]
    needs = [table.get(n, cfg.default_need) for n in names
             if n and n.casefold() != "remote"]
    return min(needs, key=SPONSORSHIP_NEEDS.index) if needs else cfg.sponsorship_need


def _fact_label(fact: str, country: Any, cfg: ScoringConfig) -> str:
    """The label for one statement, given your sponsorship need there."""
    need = need_in(country, cfg)
    if fact == "offers":
        return "sponsors"
    if fact == "status_bar":
        return "ineligible"
    if fact == "no_future_sponsorship":
        return "opt_ok" if need == "never" else "ineligible"
    if fact == "no_opt_support":
        return "ineligible" if need == "later" else "opt_ok"
    if fact == "no_sponsorship":
        return "ineligible" if need == "now" else "opt_ok"
    if fact == "right_to_work":
        return "ineligible" if need == "now" else "unknown"
    return "unknown"
