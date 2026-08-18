"""Shared browser bootstrap. The one place a browser is constructed.

Two callers: `fill.py` (interactive form fill, real submissions) and
`ashby.py` (a one-off headless read of DOM-only text Ashby's question API
never returns, done during `apply plan` itself — see `ashby.fetch_dom_enrichment`).
Split out so the second caller does not have to import `fill.py` to reach it —
`fill.py` already imports `ashby`, so the reverse import would be a cycle.

`playwright` is an optional dependency (`uv sync --group apply`), and this is
the only module in `src/` that names the driver, so swapping in patchright
later is a one-line change here.
"""
from __future__ import annotations

from src import paths

USER_DATA_DIR = paths.REPO_ROOT / ".apply_profile"


def require_playwright():
    """Import the driver, or explain how to install it. Call-time, so the
    module imports fine without the group and `tests/test_profile_templates.py`'s
    AST walk keeps working."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "ERROR: playwright not installed. Run `uv sync --group apply` "
            "then `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 uv run playwright install chrome`."
        ) from exc
    return sync_playwright


def launch(p, *, headless: bool = False):
    """A separate, empty profile: `channel="chrome"` selects the system Chrome
    binary, not the user's session. Pointing this at the real profile would
    expose every logged-in cookie and is refused by Chrome anyway."""
    return p.chromium.launch_persistent_context(
        user_data_dir=str(USER_DATA_DIR),
        channel="chrome",
        headless=headless,
    )
