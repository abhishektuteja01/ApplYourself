"""The feed's postings whose JD read keeps failing, kept across nights in
`jobs/feed_failed_reads.json` as {posting_key: {"failures": n, "last_try": date}}.
After `max_failures` failed nights a posting's page is not read again until
`retry_days` have passed since its last try, so a fixed page still comes back.
A posting that gets its JD leaves the record; an entry not tried for
`forget_days` is dropped. A posting whose API answered 404 is marked `gone`:
neither its API nor its page is read until the entry is dropped, and an API
answer clears it."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from applyourself.shared.files import atomic_write_text


class FailedReads:
    def __init__(
        self, path: Path, today: date, max_failures: int, retry_days: int, forget_days: int
    ) -> None:
        self.path, self.today = Path(path), today
        self.max_failures, self.retry_days = max_failures, retry_days
        self.forget_days = forget_days
        self.problem = ""
        self.entries: dict[str, dict] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
            for key, e in data.items():
                self.entries[key] = {
                    "failures": int(e["failures"]),
                    "last_try": date.fromisoformat(e["last_try"]),
                    "gone": e.get("gone") is True,
                }
        except (OSError, ValueError, TypeError, KeyError) as e:
            self.entries = {}
            self.problem = f"{self.path.name} unreadable ({type(e).__name__}: {e}); started afresh"

    def skip(self, key: str) -> bool:
        """Whether tonight's page read of this posting is skipped."""
        e = self.entries.get(key)
        return (
            e is not None
            and e["failures"] >= self.max_failures
            and self.today - e["last_try"] < timedelta(days=self.retry_days)
        )

    def gone(self, key: str) -> bool:
        """Whether the posting's API said it is gone, within `forget_days`."""
        e = self.entries.get(key)
        return (
            e is not None
            and e["gone"]
            and self.today - e["last_try"] < timedelta(days=self.forget_days)
        )

    def mark_gone(self, key: str) -> None:
        e = self.entries.get(key)
        failures = e["failures"] if e else 0
        self.entries[key] = {"failures": failures, "last_try": self.today, "gone": True}

    def answered(self, key: str) -> None:
        """The API holds the posting: a gone mark is cleared."""
        if self.entries.get(key, {}).get("gone"):
            del self.entries[key]

    def failed(self, key: str) -> None:
        """One more failed night (a posting listed twice tonight counts once)."""
        e = self.entries.get(key)
        if e is not None and e["last_try"] == self.today:
            return
        self.entries[key] = {
            "failures": (e["failures"] if e else 0) + 1,
            "last_try": self.today,
            "gone": False,
        }

    def read(self, key: str) -> None:
        self.entries.pop(key, None)

    def save(self) -> None:
        cutoff = self.today - timedelta(days=self.forget_days)
        out = {
            k: {
                "failures": e["failures"],
                "last_try": e["last_try"].isoformat(),
                **({"gone": True} if e["gone"] else {}),
            }
            for k, e in sorted(self.entries.items())
            if e["last_try"] >= cutoff
        }
        atomic_write_text(self.path, json.dumps(out, indent=0) + "\n", mkdir=True)
