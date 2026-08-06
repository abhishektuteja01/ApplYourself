"""Every file read/write in src/ must name its encoding.

Bare read_text()/write_text()/open() use locale.getencoding(), which is ASCII
on a machine with no locale set (bare container, cron, CI). The configs and
resumes carry em dashes, so the whole CLI raises UnicodeDecodeError there.
This is invisible on a UTF-8 dev box, so grep is the only thing that catches
a regression.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
TEXT_IO_CALLS = {"read_text", "write_text", "open"}


def _source_files():
    return sorted(SRC.rglob("*.py"))


def _unencoded_calls(path: Path):
    """(lineno, call name) for every text-IO call with no encoding= kwarg."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else None)
        if name not in TEXT_IO_CALLS:
            continue
        # "b" in the mode arg means bytes, where encoding is a TypeError.
        mode = node.args[0] if node.args else None
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str) \
                and "b" in mode.value:
            continue
        if any(kw.arg == "encoding" for kw in node.keywords):
            continue
        found.append((node.lineno, name))
    return found


def test_src_is_scanned_at_all():
    """Guards the test itself: a moved src/ would make every case vacuous."""
    assert len(_source_files()) > 10


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: p.name)
def test_every_text_io_call_names_utf8(path):
    offenders = _unencoded_calls(path)
    assert not offenders, (
        f"{path.relative_to(SRC.parent)} has text IO with no encoding=: "
        + ", ".join(f"line {n}: {call}()" for n, call in offenders)
        + " — pass encoding=\"utf-8\"."
    )
