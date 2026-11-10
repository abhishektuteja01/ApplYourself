import json
import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


class Group:
    """A locator over a fixed list of elements: count / nth / first."""

    def __init__(self, items=()):
        self.items = list(items)

    def count(self):
        return len(self.items)

    def nth(self, i):
        return self.items[i]

    @property
    def first(self):
        return self.items[0]


class FakePage:
    """The one fake Playwright page. `locate(selector)` answers `locator()`
    (default: the page itself), every selector asked is kept in `selectors`,
    and `on()` handlers in `handlers`. Its text, title and screenshot are what
    the post-submit capture reads."""

    def __init__(
        self,
        locate=None,
        *,
        text="Thank you for applying to Gasket Works.",
        url="https://boards.greenhouse.io/confirmation",
    ):
        self._locate = locate
        self._text, self.url = text, url
        self.selectors: list[str] = []
        self.handlers: list[tuple] = []

    def locator(self, selector):
        self.selectors.append(selector)
        return self if self._locate is None else self._locate(selector)

    def on(self, event, handler):
        self.handlers.append((event, handler))

    @property
    def handler(self):
        """The last handler attached."""
        return self.handlers[-1][1] if self.handlers else None

    def title(self):
        return "Application submitted"

    def inner_text(self, timeout=None):
        return self._text

    def screenshot(self, path=None, full_page=False):
        pathlib.Path(path).write_bytes(b"\x89PNG")


class FakeResponse:
    """A network response as a `page.on("response")` handler sees it."""

    def __init__(self, method, url, status=200):
        self.status, self.url = status, url
        self.request = type("R", (), {"method": method})()


def load_fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def load_html(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


def load_api(name: str) -> dict:
    """A captured JSON API response. Ashby's application form arrives this way
    — the page itself is client-rendered and serves no form at all."""
    import json

    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


# The five boards captured as DOM + API pairs from the same posting.
FORM_FIXTURES = (
    "form_minimal",
    "form_multiselect",
    "form_education",
    "form_demographic",
    "form_employment",
)


@pytest.fixture
def page():
    return load_html


@pytest.fixture
def scan():
    from applyourself.apply.domscan import scan_form

    def _scan(name: str):
        return scan_form(load_html(name))

    return _scan


@pytest.fixture
def merged():
    """A board's DOM and API halves, reconciled — what answers.py resolves."""
    from applyourself.apply.domscan import scan_form
    from applyourself.apply.reconcile import reconcile
    from applyourself.apply.schema import parse_schema

    def _merged(name: str):
        return reconcile(scan_form(load_html(name)), parse_schema(load_fixture(name)))

    return _merged


@pytest.fixture
def tailor_dir(tmp_path):
    """A /tailor output dir holding both artifacts, named the way the real ones
    are (`<Name>_Resume.pdf`)."""
    out = tmp_path / "example_primary" / "2026-08-09_gasket-works_widget-engineer_a1b2c3d4"
    out.mkdir(parents=True)
    for name in (
        "Alex_Example_Resume.pdf",
        "Alex_Example_Cover_Letter.pdf",
        "resume_plan.yaml",
        "jd_snapshot.md",
        "trace.md",
    ):
        (out / name).write_bytes(b"x")
    return out


@pytest.fixture
def answers():
    from applyourself.apply.bank_schema import load_answers

    return load_answers(FIXTURES / "application_answers.yaml")
