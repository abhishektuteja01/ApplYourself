"""`browser:` in the answer config picks the driver and the stealth args.

Some boards score the browser before they will let a field work at all: a
plain launch reports `navigator.webdriver: true`, and Lever locks its location
dropdown for a caller it scores as automation. Which driver to use is a
preference, so it lives in config and `src/` only reads it.
"""
import pytest

from src.apply import browser as B


class TestLoadBrowserConfig:
    def _write(self, tmp_path, body):
        p = tmp_path / "application_answers.yaml"
        p.write_text(body, encoding="utf-8")
        return p

    def test_reads_the_driver_and_stealth_flag(self, tmp_path):
        p = self._write(tmp_path, "schema_version: 1\nbrowser:\n"
                                  "  driver: patchright\n  stealth: true\n")
        assert B.load_browser_config(p) == ("patchright", True)

    def test_a_missing_file_falls_back_to_the_stock_driver(self, tmp_path):
        assert B.load_browser_config(tmp_path / "gone.yaml") == (B.DEFAULT_DRIVER, False)

    def test_a_config_with_no_browser_block_keeps_the_old_behaviour(self, tmp_path):
        p = self._write(tmp_path, "schema_version: 1\nidentity:\n  first_name: A\n")
        assert B.load_browser_config(p) == (B.DEFAULT_DRIVER, False)

    def test_an_unknown_driver_is_refused_rather_than_silently_ignored(self, tmp_path):
        p = self._write(tmp_path, "schema_version: 1\nbrowser:\n  driver: selenium\n")
        with pytest.raises(B.BrowserConfigError, match="browser.driver"):
            B.load_browser_config(p)

    def test_a_non_mapping_browser_block_is_refused(self, tmp_path):
        p = self._write(tmp_path, "schema_version: 1\nbrowser: patchright\n")
        with pytest.raises(B.BrowserConfigError, match="must be a mapping"):
            B.load_browser_config(p)

    def test_the_driver_name_is_case_and_space_insensitive(self, tmp_path):
        p = self._write(tmp_path, "schema_version: 1\nbrowser:\n  driver: '  Patchright '\n")
        assert B.load_browser_config(p)[0] == "patchright"


class TestLaunchArgs:
    """The stealth args are what turn navigator.webdriver from true to false —
    measured, not assumed."""

    class FakeChromium:
        def __init__(self):
            self.kwargs = None

        def launch_persistent_context(self, **kwargs):
            self.kwargs = kwargs
            return "context"

    class FakeP:
        def __init__(self, chromium):
            self.chromium = chromium

    def test_stealth_drops_the_automation_tells(self):
        chromium = self.FakeChromium()
        B.launch(self.FakeP(chromium), stealth=True)
        assert "--disable-blink-features=AutomationControlled" in chromium.kwargs["args"]
        assert "--enable-automation" in chromium.kwargs["ignore_default_args"]
        assert chromium.kwargs["no_viewport"] is True

    def test_without_stealth_the_launch_is_unchanged(self):
        chromium = self.FakeChromium()
        B.launch(self.FakeP(chromium), stealth=False)
        assert "args" not in chromium.kwargs
        assert "ignore_default_args" not in chromium.kwargs
        assert "no_viewport" not in chromium.kwargs

    def test_the_real_profile_is_never_used(self):
        """A launch pointed at the user's own Chrome profile would expose every
        logged-in cookie."""
        chromium = self.FakeChromium()
        B.launch(self.FakeP(chromium), stealth=True)
        assert chromium.kwargs["user_data_dir"].endswith(".apply_profile")
        assert chromium.kwargs["channel"] == "chrome"


class TestAnswerConfigAcceptsTheBlock:
    def test_browser_is_an_allowed_top_level_key(self):
        """The answer loader refuses unknown top-level keys, so the real
        config would fail to load without this."""
        from src.apply.answers import TOP_LEVEL_KEYS
        assert "browser" in TOP_LEVEL_KEYS
