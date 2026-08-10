"""TEMPORARY run instrumentation for the first concurrent discovery runs.

DELETE THIS MODULE AND ITS CALL SITES once a few runs look stable. Find them all
with:

    grep -rn 'trace\\.' src/

Every emitted line is prefixed `TRACE` so the log is greppable the same way:

    grep TRACE logs/discovery_<run_id>.log

Nothing here affects control flow: a call is a log line and nothing else, and
`TRACE = False` silences the lot without touching a call site.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

TRACE = True


def trace(msg: str) -> None:
    if TRACE:
        log.info("TRACE %s", msg)


class Ticker:
    """Periodic progress over a long paced loop: rate, elapsed, eta.

    `every <= 0` disables ticking, so a caller never has to guard the call.
    """

    def __init__(self, label: str, total: int, every: int = 250):
        self.label = label
        self.total = total
        self.every = every
        self.t0 = time.time()

    def _line(self, done: int, extra: dict) -> str:
        elapsed = time.time() - self.t0
        rate = elapsed / done if done else 0.0
        eta = (self.total - done) * rate
        bits = "".join(f" {k}={v}" for k, v in extra.items())
        return (f"{self.label} {done}/{self.total}{bits} "
                f"{rate:.2f}s/item elapsed {elapsed / 60:.1f}m eta {eta / 60:.1f}m")

    def tick(self, done: int, **extra) -> None:
        if not TRACE or self.every <= 0 or done <= 0 or done % self.every:
            return
        trace(self._line(done, extra))

    def finish(self, done: int, **extra) -> None:
        if not TRACE:
            return
        trace(self._line(done, extra) + " DONE")
