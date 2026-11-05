"""Whether a location is inside the allowlist, and the countries a title restricts the hire to."""
from __future__ import annotations

import re
from dataclasses import dataclass

from src.shared.location.parser import _SEPARATOR_RE, parse_location
from src.shared.location.places import (
    CC_TO_COUNTRY,
    REGION_TOKEN_COUNTRIES,
    _fold,
    canonical_country,
)

# ---------------------------------------------------------------------
# The allowlist decision, one row at a time
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class Allowed:
    keep: bool
    # The display form written to clean.parquet: "Austin, TX", "Remote".
    location: str
    # False when the keep is only "nothing parsed" or an ambiguity that
    # overlaps the allowlist. A resolved location wins a job_id's
    # representative row.
    resolved: bool


class Allowlist:
    """`location_allowlist` plus the `abroad` lane rules, normalized once."""

    def __init__(self, cfg):
        self.countries = {c.lower() for c in cfg.location_allowlist.effective_countries()}
        self.abroad = cfg.abroad_lanes()

    def check(self, raw, vertical: str = "", title: str = "") -> Allowed:
        got = self._check_location(raw, vertical)
        limit = residency_countries(title)
        if got.keep and limit and self.countries and not {c.lower() for c in limit} & self.countries:
            # "AI Engineer (Remote - ES/UK only)": the title names where the
            # hire must live, and none of it is allowed.
            return Allowed(False, got.location, True)
        return got

    def _check_location(self, raw, vertical: str) -> Allowed:
        text = str(raw).strip() if isinstance(raw, str) else ""
        parsed = parse_location(text)

        if not (parsed.country or parsed.state or parsed.city or parsed.candidate_countries):
            return Allowed(True, "Remote" if parsed.remote else text, False)

        if parsed.candidate_countries:
            overlap = {c.lower() for c in parsed.candidate_countries} & self.countries
            if not self.countries or overlap:
                return Allowed(True, "Remote" if parsed.remote else text, False)
            return Allowed(False, text, False)

        country = parsed.country.lower()
        if self.countries and country not in self.countries:
            return Allowed(False, text, True)
        if country in self.abroad and vertical and vertical not in self.abroad[country]:
            return Allowed(False, text, True)

        # A US place reads "City, ST"; anywhere else keeps its country, so
        # "Chennai, TN" can never read as Tennessee. A multi-site listing
        # keeps its own text.
        us = parsed.country == "United States"
        if parsed.city and parsed.state and us:
            canon = f"{parsed.city}, {parsed.state}"
        elif parsed.city:
            canon = f"{parsed.city}, {parsed.country}"
        elif parsed.state and us:
            canon = parsed.state
        elif len([p for p in _SEPARATOR_RE.split(text) if p.strip() and "remote" not in p.lower()]) > 1:
            return Allowed(True, text, True)
        else:
            canon = parsed.country
        if parsed.remote:
            canon = f"Remote - {canon}" if canon else "Remote"
        return Allowed(True, canon, True)


# A residency qualifier in a title: a country list right before "only" or
# "based" ("(Remote - ES/UK only)", "(UK Based)"). Nothing looser: a bare
# country word in a title is too often something else ("CAD", "Columbia, MD").
_RESIDENCY_RE = re.compile(r"\b(?:only|based)\b", re.IGNORECASE)
_RESIDENCY_CUT_RE = re.compile(r"[(\[|:–—]|\s-\s")
_RESIDENCY_SPLIT_RE = re.compile(r"\s*(?:/|,|&|\bor\b|\band\b)\s*", re.IGNORECASE)
_TITLE_NOISE_RE = re.compile(r"\b(?:remote|hybrid|on-?site|fully|100%)\b", re.IGNORECASE)
_CODE_ALIASES = {"UK": "GB", "EU": ""}


def _residency_token(token: str) -> set[str]:
    token = token.strip(" .-")
    if not token:
        return set()
    if len(token) == 2 and token.isupper():
        if token == "EU":
            return set(REGION_TOKEN_COUNTRIES["europe"])
        name = CC_TO_COUNTRY.get(_CODE_ALIASES.get(token, token))
        return {name} if name else set()
    region = REGION_TOKEN_COUNTRIES.get(_fold(token))
    if region:
        return set(region)
    name = canonical_country(token)
    return {name} if name else set()


def residency_countries(title: str) -> set[str]:
    """Countries a title restricts the hire to, or an empty set. Every token
    in the qualifier must name a country or region, or the title says
    something else ("US Citizens only", "W2 only") and is left alone."""
    out: set[str] = set()
    for m in _RESIDENCY_RE.finditer(title or ""):
        head = title[:m.start()]
        cut = list(_RESIDENCY_CUT_RE.finditer(head))
        chunk = _TITLE_NOISE_RE.sub(" ", head[cut[-1].end():] if cut else head)
        tokens = [t for t in _RESIDENCY_SPLIT_RE.split(chunk) if t.strip(" .-")]
        found = [_residency_token(t) for t in tokens]
        if tokens and all(found):
            for f in found:
                out |= f
    return out
