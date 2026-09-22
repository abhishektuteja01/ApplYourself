"""A per-run request trace for one crawl lane.

Every request attempt `ats_http` makes on the lane's thread, plus whatever
crawl records the lane adds, goes to one JSON-lines file, and the counts come
back as run-report lines. Its job is to show whether the pacing is what earns
a 429 or a Cloudflare block: which request was blocked first, and the request
rate over the minute before it.
"""
from __future__ import annotations

import json
import logging
import time
from collections import deque
from pathlib import Path

log = logging.getLogger(__name__)

# Statuses that mean the provider pushed back, as against a dead posting.
BLOCK_STATUSES = frozenset({403, 429})
RATE_WINDOW_SECONDS = 60


class CrawlTrace:
    def __init__(self, path: Path | None):
        self.path = path
        self._fh = None
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = open(path, "a", encoding="utf-8", buffering=1)
            except OSError as e:
                log.warning("crawl trace disabled, cannot open %s: %s", path, e)
        self.t0 = time.time()
        self.requests = 0
        self.by_status: dict[str, int] = {}
        self.retries = 0
        self.request_secs = 0.0
        self.sleep_secs = 0.0
        self.first_block: dict | None = None
        self._recent: deque[float] = deque()

    def record(self, kind: str, **fields) -> None:
        if self._fh is None:
            return
        line = {"t": round(time.time() - self.t0, 3), "kind": kind, **fields}
        try:
            self._fh.write(json.dumps(line, default=str) + "\n")
        except OSError as e:
            log.warning("crawl trace write failed, disabling: %s", e)
            self._fh = None

    def on_http(self, event: dict) -> None:
        """The `ats_http.observe` callback."""
        now = time.time()
        self.requests += 1
        self.request_secs += event.get("secs") or 0.0
        if (event.get("attempt") or 1) > 1:
            self.retries += 1
        status = event.get("status")
        key = str(status) if status is not None else "net_error"
        self.by_status[key] = self.by_status.get(key, 0) + 1

        self._recent.append(now)
        while self._recent and now - self._recent[0] > RATE_WINDOW_SECONDS:
            self._recent.popleft()
        rate = len(self._recent)

        blocked = status in BLOCK_STATUSES or (status is not None and 500 <= status < 600)
        if blocked and self.first_block is None:
            self.first_block = {"status": status, "request": self.requests,
                                "at_secs": round(now - self.t0, 1),
                                "per_min": rate, "url": event.get("url")}
        self.record("http", status=status, attempt=event.get("attempt"),
                    secs=round(event.get("secs") or 0.0, 3),
                    method=event.get("method"), url=event.get("url"),
                    retry_after=event.get("retry_after"),
                    cf_mitigated=event.get("cf_mitigated"),
                    error=event.get("error"), per_min=rate)

    def add_sleep(self, secs: float) -> None:
        self.sleep_secs += secs

    def summary_lines(self) -> list[str]:
        statuses = " | ".join(f"{k}: {v}" for k, v in sorted(self.by_status.items()))
        avg = self.request_secs / self.requests if self.requests else 0.0
        lines = [
            f"HTTP: {self.requests} requests | {statuses or 'none'} "
            f"| retries: {self.retries}",
            f"HTTP time: {self.request_secs:.0f}s waiting on replies "
            f"(avg {avg:.2f}s) | {self.sleep_secs:.0f}s pacing sleep",
        ]
        fb = self.first_block
        lines.append(
            "First pushback: none" if fb is None else
            f"First pushback: HTTP {fb['status']} at request #{fb['request']}, "
            f"{fb['at_secs']}s in, {fb['per_min']} requests in the prior minute")
        if self.path is not None and self._fh is not None:
            lines.append(f"Trace: `{self.path.name}`")
        return lines

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
