import json
import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def load_html(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


# The five boards captured as DOM + API pairs from the same posting.
FORM_FIXTURES = (
    "form_minimal",
    "form_multiselect",
    "form_education",
    "form_demographic",
    "form_employment",
)


@pytest.fixture
def payload():
    return load_fixture


@pytest.fixture
def page():
    return load_html


@pytest.fixture
def scan():
    from src.apply.domscan import scan_form

    def _scan(name: str):
        return scan_form(load_html(name))

    return _scan


@pytest.fixture
def merged():
    """A board's DOM and API halves, reconciled — what answers.py resolves."""
    from src.apply.domscan import scan_form
    from src.apply.reconcile import reconcile
    from src.apply.schema import parse_schema

    def _merged(name: str):
        return reconcile(scan_form(load_html(name)), parse_schema(load_fixture(name)))

    return _merged


@pytest.fixture
def tailor_dir(tmp_path):
    """A /tailor output dir holding both artifacts, named the way the real ones
    are (`<Name>_Resume.pdf`)."""
    out = tmp_path / "example_primary" / "2026-08-09_gasket-works_widget-engineer_a1b2c3d4"
    out.mkdir(parents=True)
    for name in ("Alex_Example_Resume.pdf", "Alex_Example_Cover_Letter.pdf",
                 "resume.md", "jd_snapshot.md", "trace.md"):
        (out / name).write_bytes(b"x")
    return out


@pytest.fixture
def answers():
    from src.apply.answers import load_answers

    return load_answers(
        FIXTURES / "application_answers.yaml", FIXTURES / "preferences_time_limited.md"
    )
