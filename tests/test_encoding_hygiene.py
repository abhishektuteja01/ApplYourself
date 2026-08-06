"""Every file read/write must name its encoding.

Bare read_text()/write_text()/open() use locale.getencoding(), which is ASCII
on a machine with no locale set (bare container, cron, CI). The configs and
resumes carry em dashes, so the whole CLI raises UnicodeDecodeError there.
subprocess with text=True decodes the same way. This is invisible on a UTF-8
dev box, so a scan is the only thing that catches a regression.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCANNED_DIRS = ("src", "tests")
TEXT_IO_CALLS = {"read_text", "write_text", "open"}


def _source_files():
    return sorted(p for d in SCANNED_DIRS
                  for p in (REPO_ROOT / d).rglob("*.py"))


def _is_binary_open(node, name):
    """Binary mode makes encoding= a TypeError, so those calls are exempt.
    Only open() takes a mode — write_text's first arg is the content, and
    treating it as a mode exempts every string that happens to contain "b"."""
    if name != "open":
        return False
    candidates = list(node.args) + [kw.value for kw in node.keywords
                                    if kw.arg == "mode"]
    for c in candidates:
        if isinstance(c, ast.Constant) and isinstance(c.value, str) \
                and c.value and set(c.value) <= set("rwaxbt+"):
            return "b" in c.value
    return False


def _is_decoding_subprocess(node, name):
    """subprocess.run(..., text=True) without encoding= decodes via locale."""
    if name not in {"run", "check_output", "Popen"}:
        return False
    return any(kw.arg in {"text", "universal_newlines"}
               and isinstance(kw.value, ast.Constant) and kw.value.value
               for kw in node.keywords)


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
        if name in TEXT_IO_CALLS:
            if _is_binary_open(node, name):
                continue
        elif not _is_decoding_subprocess(node, name):
            continue
        if any(kw.arg == "encoding" for kw in node.keywords):
            continue
        found.append((node.lineno, name))
    return found


def test_the_scan_actually_covers_both_trees():
    """Guards the test itself: a moved dir would make every case vacuous."""
    scanned = _source_files()
    for d in SCANNED_DIRS:
        assert any(p.is_relative_to(REPO_ROOT / d) for p in scanned), d
    assert len(scanned) > 20


def test_the_scan_detects_a_bare_call(tmp_path):
    """Guards the predicate: the write_text content arg was once mistaken for
    an open() mode, so any content containing "b" was silently exempted."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        'p.read_text()\n'
        'p.write_text("brb")\n'
        'p.open("w")\n'
        'subprocess.run(cmd, text=True)\n'
        'p.open("rb")\n'
        'p.read_text(encoding="utf-8")\n',
        encoding="utf-8",
    )
    assert [n for n, _ in _unencoded_calls(sample)] == [1, 2, 3, 4]


@pytest.mark.parametrize("path", _source_files(),
                         ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_every_text_io_call_names_utf8(path):
    offenders = _unencoded_calls(path)
    assert not offenders, (
        f"{path.relative_to(REPO_ROOT)} has text IO with no encoding=: "
        + ", ".join(f"line {n}: {call}()" for n, call in offenders)
        + " — pass encoding=\"utf-8\"."
    )


# ---------------------------------------------------------------------
# Whitespace hygiene. Not encoding, but the same argument: invisible on a dev
# box, and a scan is the only thing that catches a regression.
# ---------------------------------------------------------------------

def test_no_trailing_whitespace_in_python_files():
    """No line ends in whitespace — including indented blank lines, which are
    invisible in an editor and show up as diff noise for the next reader."""
    offenders = []
    for path in _source_files():
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").split("\n"), start=1
        ):
            if line != line.rstrip():
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert not offenders, (
        f"{len(offenders)} line(s) end in whitespace: {offenders[:10]}"
    )
