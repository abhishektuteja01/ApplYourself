"""Shared fixtures. Tests never read the gitignored profile/verticals.yaml —
the committed tests/fixtures/verticals.yaml (three synthetic verticals:
example_primary/secondary/tertiary) is injected into the src.shared.verticals
singleton for every test. Coverage of the real config lives in
tests/test_real_config_drift.py, which skips when that file is absent."""

from pathlib import Path

import pytest

from src.shared import verticals

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "verticals.yaml"
REAL_CONFIG = Path(__file__).resolve().parent.parent / "profile" / "verticals.yaml"


# Tests that load libpostal (~2 GB each process) share one xdist worker.
_POSTAL_PATHS = (
    "tests/discovery/",
    "tests/shared/test_location.py",
    "tests/test_optional_dependencies.py",
    "tests/test_onboard_scaffold.py",
    "tests/apply/test_apply_cli.py",
    "tests/e2e/",
)


def pytest_collection_modifyitems(config, items):
    root = Path(str(config.rootpath))
    for item in items:
        rel = Path(str(item.path)).relative_to(root).as_posix()
        if rel.startswith(_POSTAL_PATHS):
            item.add_marker(pytest.mark.xdist_group("postal"))


def pytest_terminal_summary(terminalreporter):
    """Not pytest_report_header: the header is suppressed by -q, and
    `uv run pytest tests -q` is the documented command."""
    if not REAL_CONFIG.is_file():
        terminalreporter.write_line(
            "WARNING: profile/verticals.yaml absent — test_real_config_drift.py "
            "skipped; the live config is unchecked.",
            yellow=True,
        )


@pytest.fixture(autouse=True)
def cfg():
    config = verticals.load_verticals(FIXTURE_PATH)
    verticals.set_config(config)
    yield config
    verticals.set_config(None)


@pytest.fixture(autouse=True)
def _isolate_root(tmp_path, tmp_path_factory, monkeypatch):
    """Every data path resolves under tmp_path, so no test reads or writes the
    real pipeline/, jobs/, applications/ or profile/. Resume sources and scoring
    are copies of the committed examples. To point one name elsewhere use
    `monkeypatch.setitem(paths._overrides, NAME, path)`, never setattr on paths."""
    import shutil

    from src.shared import paths

    for name in set(vars(paths)) & set(paths._LAYOUT):
        delattr(paths, name)
    profile = paths.REPO_ROOT / "profile"
    monkeypatch.setattr(paths, "_root", tmp_path)
    copies = tmp_path_factory.mktemp("examples")
    for name in ("resume.example.yaml", "bullets.example.md", "skills_master.example.md"):
        shutil.copyfile(profile / name, copies / name)
    # Copies of what a test may write; read-only committed files in place.
    monkeypatch.setattr(
        paths,
        "_overrides",
        {
            "RESUME": copies / "resume.example.yaml",
            "BULLETS": copies / "bullets.example.md",
            "SKILLS": copies / "skills_master.example.md",
            "SCORING": profile / "scoring.example.yaml",
            "DE_AI_RULES": profile / "de_ai_rules.yaml",
            "VERTICALS_DIR": profile / "verticals",
        },
    )
    yield
    leaked = sorted(set(vars(paths)) & set(paths._LAYOUT))
    assert not leaked, f"setattr on paths.{leaked} leaks; setitem paths._overrides instead"


@pytest.fixture(autouse=True)
def _no_git_hook_env(monkeypatch):
    """A commit hook runs this suite with GIT_DIR and GIT_INDEX_FILE set, and a
    test that makes a temp repo would then write to the real one (it set
    core.bare=true once). Every git call a test makes starts clean."""
    import os

    for name in [n for n in os.environ if n.startswith("GIT_")]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def clear_location_cache():
    """parse_location is lru_cached, so a test that patches COUNTRY_NAMES /
    CITIES_BY_NAME would otherwise see (or leak) another test's entries."""
    from src.shared.location.parser import parse_location

    parse_location.cache_clear()
    yield
    parse_location.cache_clear()
