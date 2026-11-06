"""Every data path resolves from one root at call time, so one fixture isolates
every test from the real pipeline/, jobs/, applications/ and profile/."""

import ast

from src.shared import paths

READ_IN_PLACE = {"SCORING", "DE_AI_RULES", "VERTICALS_DIR"}


def test_no_test_resolves_a_data_path_inside_the_repo(tmp_path):
    for name in paths._LAYOUT:
        p = getattr(paths, name)
        if name in READ_IN_PLACE:
            assert p.is_relative_to(paths.REPO_ROOT / "profile"), name
        else:
            assert not p.is_relative_to(paths.REPO_ROOT), name
            if name not in paths._overrides:
                assert p.is_relative_to(tmp_path), name


def _data_path_refs(node: ast.AST) -> list[str]:
    return [
        f"paths.{n.attr}"
        for n in ast.walk(node)
        if isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id == "paths"
        and (n.attr in paths._LAYOUT or n.attr == "root")
    ]


def test_no_module_copies_a_data_path_at_import():
    """A module-level `X = paths.CLEAN` or a `def f(p=paths.CLEAN)` default is
    bound once, at import, and would keep pointing at the real repo in tests."""
    found = []
    for folder in ("src", "scripts"):
        for f in (paths.REPO_ROOT / folder).rglob("*.py"):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                    found += [f"{f.name}:{node.lineno} {r}" for r in _data_path_refs(node.value)]
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for d in [*node.args.defaults, *node.args.kw_defaults]:
                        if d is not None:
                            found += [f"{f.name}:{node.lineno} {r}" for r in _data_path_refs(d)]
    assert found == []
