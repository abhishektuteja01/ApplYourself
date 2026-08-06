from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

@dataclass
class SourceResult:
    rows: list[dict]
    report_lines: list[str]
    errors: list[str]

class Source(ABC):
    name: str

    @abstractmethod
    def fetch(self, ctx: Any) -> SourceResult:
        pass
