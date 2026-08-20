"""Shared browser bootstrap. The one place a browser is constructed.

Two callers: `fill.py` (interactive form fill, real submissions) and
`ashby.py` (a one-off headless read of DOM-only text Ashby's question API
never returns, done during `apply plan` itself — see `ashby.fetch_dom_enrichment`).
Split out so the second caller does not have to import `fill.py` to reach it —
`fill.py` already imports `ashby`, so the reverse import would be a cycle.

The driver is a config choice, not a constant: some boards score the browser
before they will let a field work at all, and the stock driver is scored as
automation on sight. `browser.driver` in `profile/application_answers.yaml`
picks between `patchright` (the fork that patches the CDP-side leaks) and
`playwright` (stock). Both are in the `apply` group, so either name resolves.

The YAML is read here directly rather than through `answers.py`: `ashby.py`
constructs a browser during `apply plan` without ever loading an answer file,
and `answers.py` validates a great deal this module does not need.

`playwright`/`patchright` are optional dependencies (`uv sync --group apply`),
and this is the only module in `src/` that names a driver.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from src import paths

USER_DATA_DIR = paths.REPO_ROOT / ".apply_profile"
CONFIG_PATH = paths.PROFILE / "application_answers.yaml"

DRIVERS = ("patchright", "playwright")
DEFAULT_DRIVER = "playwright"

#: Turns navigator.webdriver from true to false. The single most-read
#: automation signal there is, and a plain launch broadcasts it.
STEALTH_ARGS = ("--disable-blink-features=AutomationControlled",)
#: Playwright adds this itself; it is a tell on its own.
STEALTH_DROP_DEFAULT_ARGS = ("--enable-automation",)


class BrowserConfigError(Exception):
    """`browser:` in the answer config names something this module cannot do."""


def load_browser_config(path=None) -> tuple[str, bool]:
    """`(driver, stealth)` from the `browser:` block, with defaults.

    A missing file or a missing block is not an error — the stock driver with
    no stealth is exactly the old behaviour, so a clone that never opted in
    keeps working.
    """
    p = Path(path) if path is not None else CONFIG_PATH
    if not p.exists():
        return DEFAULT_DRIVER, False
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise BrowserConfigError(f"Malformed YAML in {p}: {exc}") from exc
    block = data.get("browser") or {}
    if not isinstance(block, dict):
        raise BrowserConfigError(f"{p}: browser: must be a mapping")
    driver = str(block.get("driver", DEFAULT_DRIVER)).strip().lower()
    if driver not in DRIVERS:
        raise BrowserConfigError(
            f"{p}: browser.driver must be one of {list(DRIVERS)}, got {driver!r}"
        )
    return driver, bool(block.get("stealth", False))


def require_playwright(driver: str | None = None):
    """Import the configured driver's `sync_playwright`, or explain how to
    install it. Call-time, so the module imports fine without the group and
    `tests/test_profile_templates.py`'s AST walk keeps working."""
    name = driver or load_browser_config()[0]
    try:
        if name == "patchright":
            from patchright.sync_api import sync_playwright
        else:
            from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit(
            f"ERROR: {name} not installed. Run `uv sync --group apply` "
            f"then `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 uv run playwright install chrome`."
        ) from exc
    return sync_playwright


def launch(p, *, headless: bool = False, stealth: bool | None = None):
    """A separate, empty profile: `channel="chrome"` selects the system Chrome
    binary, not the user's session. Pointing this at the real profile would
    expose every logged-in cookie and is refused by Chrome anyway.

    `stealth` defaults to the configured value; pass it explicitly to override.
    """
    if stealth is None:
        stealth = load_browser_config()[1]
    kwargs: dict = {}
    if stealth:
        kwargs["args"] = list(STEALTH_ARGS)
        kwargs["ignore_default_args"] = list(STEALTH_DROP_DEFAULT_ARGS)
        # A fixed 1280x720 viewport is itself a signal, and the real window
        # size is what a person would have.
        kwargs["no_viewport"] = True
    return p.chromium.launch_persistent_context(
        user_data_dir=str(USER_DATA_DIR),
        channel="chrome",
        headless=headless,
        **kwargs,
    )
