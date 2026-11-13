"""`ay` routes: one per old entry point, and each target sees only its own args."""

import importlib
import inspect
import sys
import tomllib
from pathlib import Path

import pytest

from applyourself import cli

ROOT = Path(__file__).resolve().parents[2]


def test_every_entry_point_has_exactly_one_importable_route():
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "scripts"
    ]
    targets = list(cli.ROUTES.values())
    assert scripts.pop("ay") == "applyourself.cli:main"
    assert sorted(targets) == sorted(scripts.values())
    for target in targets:
        module, func = target.split(":")
        assert callable(getattr(importlib.import_module(module), func)), target


def _record(monkeypatch, words):
    """Swap the route's target for a recorder with the real one's signature."""
    module, func = cli.ROUTES[words].split(":")
    mod = importlib.import_module(module)
    calls = []

    def fake(*args):
        calls.append((list(sys.argv), list(args)))
        return 7

    fake.__signature__ = inspect.signature(getattr(mod, func))
    monkeypatch.setattr(mod, func, fake)
    monkeypatch.setattr(sys, "argv", ["ay"])
    return calls


@pytest.mark.parametrize(
    "words",
    [("onboard", "extract"), ("config", "check-verticals"), ("discover", "check")],
)
def test_argv_only_mains_read_the_routed_sys_argv(monkeypatch, words):
    calls = _record(monkeypatch, words)
    assert cli.main([*words, "x.txt", "--flag"]) == 7
    assert calls == [([" ".join(("ay", *words)), "x.txt", "--flag"], [])]


def test_config_check_gets_argv_and_sys_argv(monkeypatch):
    calls = _record(monkeypatch, ("config", "check"))
    assert cli.main(["config", "check"]) == 7
    assert calls == [(["ay config check"], [[]])]


@pytest.mark.parametrize(
    ("argv", "words", "rest"),
    [
        (["track", "standup"], ("track",), ["standup"]),
        (["night", "tailor-pick", "--n", "3"], ("night",), ["tailor-pick", "--n", "3"]),
        (["night", "--only", "summary"], ("night",), ["--only", "summary"]),
        (["night", "summary", "--x"], ("night", "summary"), ["--x"]),
        (["apply", "plan", "X"], ("apply",), ["plan", "X"]),
    ],
)
def test_subwords_reach_the_right_main(monkeypatch, argv, words, rest):
    calls = _record(monkeypatch, words)
    assert cli.main(argv) == 7
    assert calls == [([" ".join(("ay", *words)), *rest], [rest])]


def test_unknown_group_exits_2_on_stderr(capsys):
    assert cli.main(["nope"]) == 2
    out = capsys.readouterr()
    assert out.out == "" and "nope" in out.err
