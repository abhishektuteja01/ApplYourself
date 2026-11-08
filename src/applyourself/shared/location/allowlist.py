"""Whether a location is inside the allowlist, and the form clean.parquet shows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from applyourself.shared.countries import CC_TO_COUNTRY
from applyourself.shared.location.parser import (
    LIST_SEPARATOR_RE,
    Reading,
    allowed_codes,
    read_location,
)
from applyourself.shared.location.places import CODE2, DOUBT_CC

if TYPE_CHECKING:
    from applyourself.discovery.config import DiscoveryConfig


@dataclass(frozen=True)
class Allowed:
    keep: bool
    # The display form written to clean.parquet: "Austin, TX", "Remote".
    location: str
    # True when the location is one country for sure. A resolved location wins
    # a job_id's representative row.
    resolved: bool


def structured_codes(country: Any) -> frozenset[str]:
    """The ISO codes in a source's own country field ("US", "IE,US")."""
    if not isinstance(country, str):
        return frozenset()
    return frozenset(
        CODE2[c] for c in (p.strip().upper() for p in country.split(",")) if c in CODE2
    )


class Allowlist:
    """`location_allowlist` plus the `abroad` lane rules, normalized once."""

    def __init__(self, cfg: DiscoveryConfig) -> None:
        self.allowed = allowed_codes(cfg.location_allowlist.effective_countries())
        self.abroad = cfg.abroad_lanes()

    def check(self, raw: Any, vertical: str = "", country: Any = "") -> Allowed:
        """`country`: the source's own country codes, which settle what the text leaves open."""
        text = raw.strip() if isinstance(raw, str) else ""
        r = read_location(text, self.allowed)
        given = structured_codes(country)
        if given:
            # The text keeps it only by naming an allowed country or a region outright,
            # or by being one place in an allowed country ("New York, NY").
            keep = bool(given & (self.allowed | DOUBT_CC)) or r.named_allowed or r.doubt
            keep = keep or r.sure in self.allowed
            certain = next(iter(given)) if len(given) == 1 else ""
        else:
            keep, certain = r.keep, r.certain
        keep = keep or not self.allowed
        if not keep:
            return Allowed(False, text, bool(certain))
        lanes = self.abroad.get(CC_TO_COUNTRY.get(certain, "").lower())
        if lanes is not None and vertical and vertical not in lanes:
            return Allowed(False, text, True)
        if r.place and (not given or r.place in given):
            return Allowed(True, _shown(text, r), True)
        if len(given) == 1 and not text:
            return Allowed(True, CC_TO_COUNTRY[certain], True)
        # "Remote" only when no part reads a place: "Remote - Paris" keeps its text.
        shown = "Remote" if r.remote and not r.countries else text
        return Allowed(True, shown, len(given) == 1)


def _shown(text: str, r: Reading) -> str:
    """A US place reads "City, ST"; anywhere else keeps its country, so
    "Chennai, TN" never reads as Tennessee."""
    city, state, country = r.city, r.state, CC_TO_COUNTRY[r.place]
    us = country == "United States"
    if city and state and us:
        canon = f"{city}, {state}"
    elif city:
        canon = f"{city}, {country}"
    elif state and us:
        if r.loose or state in CODE2:
            return text  # "Taylor, TX": a town the tables lack; "IN, US" is no country
        canon = state
    elif (
        len([p for p in LIST_SEPARATOR_RE.split(text) if p.strip() and "remote" not in p.lower()])
        > 1
    ):
        return text
    else:
        canon = country
    return f"Remote - {canon}" if r.remote else canon
