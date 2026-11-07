"""What every lane is: a name, an optional `prepare`, `run`, an optional `done`.

`run.py` drives every lane the same way: `prepare` on all of them before any
thread starts, `run(crawl, tally)` on its own thread, `done` after the final
store flush.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DROP_SAMPLES = 3


@dataclass
class LaneStats:
    """What one lane saw, for the run report. Every count after `listed` is
    a subset of the one before it."""

    boards_due: int = 0
    boards_checked: int = 0
    boards_ok: int = 0
    boards_empty: int = 0
    boards_404: int = 0
    boards_error: int = 0
    listed: int = 0
    classified: int = 0
    passed: int = 0
    known: int = 0
    new: int = 0
    detail_ok: int = 0
    detail_failed: int = 0
    short_jd: int = 0
    # Stored postings whose API was asked tonight whether they are still up, those
    # tonight's answer held back as gone, and those an earlier night's mark held back.
    liveness_checked: int = 0
    liveness_gone: int = 0
    liveness_gone_known: int = 0
    errors: list[str] = field(default_factory=list)
    truncated: bool = False
    notes: list[str] = field(default_factory=list)
    capped: list[str] = field(default_factory=list)  # queries that hit results_wanted
    drops: dict[str, int] = field(default_factory=dict)  # title-gate drops per reason
    drop_samples: dict[str, list[str]] = field(default_factory=dict)

    def drop(self, reason: str, title: str) -> None:
        """Count one title-gate drop, keeping its first few titles as samples."""
        self.drops[reason] = self.drops.get(reason, 0) + 1
        samples = self.drop_samples.setdefault(reason, [])
        if len(samples) < DROP_SAMPLES and title not in samples:
            samples.append(title)


class Lane:
    name = ""

    def prepare(self, crawl) -> None:
        """Runs before any lane's thread starts."""

    def run(self, crawl, tally=None) -> LaneStats:
        """Find postings into `crawl.batch`. `tally` counts HTTP calls a lane
        makes outside `http_client`."""
        raise NotImplementedError

    def done(self) -> None:
        """Runs after the store flush that saved this lane's postings."""
