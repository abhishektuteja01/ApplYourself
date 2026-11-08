"""Whether a location is inside the allowlist."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from applyourself.shared.countries import CC_TO_COUNTRY
from applyourself.shared.location.parser import allowed_codes, read_location
from applyourself.shared.location.places import CODE2, DOUBT_CC

if TYPE_CHECKING:
    from applyourself.discovery.config import DiscoveryConfig


@dataclass(frozen=True)
class Allowed:
    keep: bool
    # What clean.parquet shows: the posting's own text, trimmed.
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
            # The text keeps it only by naming an allowed country or a region
            # outright, or by being one place in an allowed country ("New York, NY").
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
        # The display is the posting's own text: nothing derived, nothing invented.
        return Allowed(True, text, bool(r.sure) or len(given) == 1)
