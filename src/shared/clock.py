"""The one local clock. Naive local time, so stored stamps keep their format."""

from __future__ import annotations

from datetime import date, datetime


def now() -> datetime:
    return datetime.now()  # noqa: DTZ005 - local wall clock; stamps on disk carry no offset


def today() -> date:
    return now().date()
