"""Shared fixtures. Tests never read the gitignored profile/verticals.yaml —
the committed tests/fixtures/verticals.yaml (three synthetic verticals:
example_primary/secondary/tertiary) is injected into the src.verticals
singleton for every test. Coverage of the real config lives in
tests/test_real_config_drift.py, which skips when that file is absent."""

from pathlib import Path

import pytest

from src import verticals

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "verticals.yaml"
REAL_CONFIG = Path(__file__).resolve().parent.parent / "profile" / "verticals.yaml"


def pytest_terminal_summary(terminalreporter):
    """Not pytest_report_header: the header is suppressed by -q, and
    `uv run pytest tests -q` is the documented command."""
    if not REAL_CONFIG.is_file():
        terminalreporter.write_line(
            "WARNING: profile/verticals.yaml absent — test_real_config_drift.py "
            "skipped; the live config is unchecked.", yellow=True)


@pytest.fixture(autouse=True)
def cfg():
    config = verticals.load_verticals(FIXTURE_PATH)
    verticals.set_config(config)
    yield config
    verticals.set_config(None)
