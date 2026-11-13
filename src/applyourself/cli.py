"""`ay`: one entry point for every CLI, grouped by slash verb.

`ay <group> [sub] args...` runs the matching module's `main` with only `args...`.
It prints nothing itself on a routed call; `ay` or `ay --help` lists the routes.
"""

import importlib
import inspect
import sys

# Route words -> "module:function". A two-word route wins over its group's own main.
ROUTES: dict[tuple[str, ...], str] = {
    ("discover",): "applyourself.discovery.run:main",
    ("discover", "digest"): "applyourself.discovery.digest:main",
    ("discover", "dupes"): "applyourself.discovery.dedupe:main",
    ("discover", "ids"): "applyourself.discovery.id_table:main",
    ("discover", "check"): "applyourself.discovery.config:main",
    ("ingest",): "applyourself.discovery.ingest_url:main",
    ("score",): "applyourself.score_cli:main",
    ("track",): "applyourself.track_cli:main",
    ("app",): "applyourself.app_cli:main",
    ("synonyms",): "applyourself.synonyms_cli:main",
    ("doctor",): "applyourself.doctor:main",
    ("apply",): "applyourself.apply_cli:main",
    ("apply", "eval"): "applyourself.evals.apply_eval:main",
    ("night",): "applyourself.night:main",
    ("night", "summary"): "applyourself.summary:main",
    ("night", "pdf-broker"): "applyourself.pdf_broker:main",
    ("config",): "applyourself.config_cli:main",
    ("config", "check"): "applyourself.config_check:main",
    ("config", "check-verticals"): "applyourself.shared.verticals:main",
    ("onboard", "scaffold"): "applyourself.onboard_scaffold:main",
    ("onboard", "extract"): "applyourself.profile_extract:main",
}


def _routes_text(group: str | None = None) -> str:
    lines = [" ".join(("ay", *words)) for words in ROUTES if group in (None, words[0])]
    return "usage:\n" + "\n".join(f"  {line} ..." for line in lines)


def main(argv: list[str] | None = None) -> int | None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(_routes_text(), file=sys.stderr)
        return 2
    if argv[0] in ("-h", "--help"):
        print(_routes_text())
        return 0
    words: tuple[str, ...] = tuple(argv[:2])
    if words not in ROUTES:
        words = words[:1]
    if words not in ROUTES:
        if any(w[0] == argv[0] for w in ROUTES):
            if argv[1:2] in (["-h"], ["--help"]):
                print(_routes_text(argv[0]))
                return 0
            print(_routes_text(argv[0]), file=sys.stderr)
        else:
            print(f"ay: unknown command {argv[0]!r}; run `ay --help`", file=sys.stderr)
        return 2
    rest = argv[len(words) :]
    module, func = ROUTES[words].split(":")
    target = getattr(importlib.import_module(module), func)
    sys.argv = [" ".join(("ay", *words)), *rest]
    result: int | None = target(rest) if inspect.signature(target).parameters else target()
    return result
