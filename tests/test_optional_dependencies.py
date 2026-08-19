"""The core install must work with no C toolchain.

`postal` binds to the libpostal C library, is sdist-only, and no Debian/Ubuntu
package ships libpostal — so it lives in the optional `discovery` group and
must not be needed to *import* anything. Only address parsing itself may
require it, and only with a message that says how to fix it.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every module that transitively pulled in postal before the import was made
# lazy: location itself, cleaning, and everything importing cleaning.
DISCOVERY_MODULES = [
    "src.discovery.location",
    "src.discovery.cleaning",
    "src.discovery.orchestrator",
    "src.discovery.inbox",
    "src.discovery.ingest_url",
    "src.discovery.sources.ats.base",
    "src.discovery.sources.ats.workday",
]


def _run_without_postal(tmp_path: Path, code: str) -> subprocess.CompletedProcess:
    """Run `code` in a subprocess where importing postal raises ImportError,
    by shadowing it with a stub package earlier on sys.path."""
    stub = tmp_path / "no_postal"
    (stub / "postal").mkdir(parents=True)
    (stub / "postal" / "__init__.py").write_text(
        'raise ImportError("libpostal absent (test stub)")\n', encoding="utf-8"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(stub), str(REPO_ROOT)])
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
    )


def test_discovery_modules_import_without_postal(tmp_path):
    code = "\n".join(f"import {m}" for m in DISCOVERY_MODULES) + "\nprint('ok')\n"
    proc = _run_without_postal(tmp_path, code)
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_parse_location_without_postal_raises_an_actionable_error(tmp_path):
    proc = _run_without_postal(tmp_path, (
        "from src.discovery.location import parse_location\n"
        "try:\n"
        "    parse_location('Austin, TX')\n"
        "except RuntimeError as exc:\n"
        "    print(exc)\n"
        "else:\n"
        "    raise AssertionError('expected RuntimeError')\n"
    ))
    assert proc.returncode == 0, proc.stderr
    message = proc.stdout
    # Names the library, both install steps, and what still works without it.
    assert "libpostal" in message
    assert "brew install libpostal" in message
    assert "--group discovery" in message
    assert "/score" in message
    # Not a bare ModuleNotFoundError, and not a silent degraded parse.
    assert "ModuleNotFoundError" not in message


def test_postal_is_optional_in_pyproject():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    core = data["project"]["dependencies"]
    assert not any(d.startswith("postal") for d in core), core
    group = data["dependency-groups"]["discovery"]
    assert any(d.startswith("postal") for d in group), group
