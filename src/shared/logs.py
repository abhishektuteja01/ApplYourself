"""One logging setup for every CLI: stdout is the result, logging is diagnostics on stderr."""

from __future__ import annotations

import logging
import sys


def setup_logging(level: int = logging.WARNING, fmt: str = "%(message)s") -> None:
    """The default matches Python's last-resort handler. A no-op once the root
    logger has a handler, so a nested `main` keeps the outer CLI's setup."""
    logging.basicConfig(level=level, format=fmt, stream=sys.stderr)
