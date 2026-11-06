"""A stage reaches another stage only through its public names: no module
imports, or reads off an imported module, a `_private` name of another stage."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "applyourself"

# Top-level modules that belong to a stage with a package or siblings.
_STAGE_OF = {
    "applyourself.app_cli": "applyourself.material",
    "applyourself.apply_cli": "applyourself.apply",
    "applyourself.score_cli": "applyourself.score",
    "applyourself.score_store": "applyourself.score",
    "applyourself.score_filter": "applyourself.score",
    "applyourself.shortlist": "applyourself.score",
    "applyourself.track_cli": "applyourself.track",
    "applyourself.state_io": "applyourself.track",
    "applyourself.cleanup": "applyourself.track",
}


def _module(path: Path) -> str:
    parts = path.relative_to(SRC.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _stage(module: str) -> str:
    return _STAGE_OF.get(module) or ".".join(module.split(".")[:2])


def _private(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def _is_module(dotted: str) -> bool:
    p = SRC.parent / Path(*dotted.split("."))
    return p.with_suffix(".py").is_file() or p.is_dir()


def _offences(text: str, module: str) -> list[str]:
    me = _stage(module)
    tree = ast.parse(text)
    aliases: dict[str, str] = {}
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("applyourself"):
            for a in node.names:
                full = f"{node.module}.{a.name}"
                if _is_module(full):
                    aliases[a.asname or a.name] = full
                elif _private(a.name) and _stage(node.module or "") != me:
                    out.append(f"{module}:{node.lineno} imports {full}")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("applyourself") and a.asname:
                    aliases[a.asname] = a.name
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
            and _private(node.attr)
            and _stage(aliases[node.value.id]) != me
        ):
            out.append(f"{module}:{node.lineno} reads {aliases[node.value.id]}.{node.attr}")
    return out


def test_the_check_sees_both_kinds_of_reach_and_allows_its_own_stage():
    text = "from applyourself.apply import sheet\nsheet._rows()\nfrom applyourself.app_cli import _slug\n"
    assert len(_offences(text, "applyourself.summary")) == 2
    assert (
        _offences(
            "from applyourself.material import folders\nfolders._slug('x')\n",
            "applyourself.app_cli",
        )
        == []
    )


def test_no_stage_reaches_another_stages_private_names():
    offences = [
        o
        for p in sorted(SRC.rglob("*.py"))
        for o in _offences(p.read_text(encoding="utf-8"), _module(p))
    ]
    assert offences == []
