"""`config-check` — load and validate every profile/ config file, one OK or
ERROR line per file. Exits 1 on any error. Read-only (R7: no LLM)."""

from __future__ import annotations

from collections.abc import Callable

import yaml

from applyourself.shared import paths
from applyourself.shared.logs import setup_logging


def _verticals() -> None:
    from applyourself.score_packet import load_examples
    from applyourself.shared.verticals import load_verticals

    for lane in load_verticals().names:
        load_examples(lane)


def _scoring() -> None:
    from applyourself.score_filter import load_scoring

    load_scoring()


def _discovery() -> None:
    from applyourself.discovery.config import load_config

    problems = load_config().validate()
    if problems:
        raise ValueError("; ".join(problems))


def _settings() -> None:
    from applyourself.shared.config_schema import load_settings

    load_settings(paths.SETTINGS)


def _contacts() -> None:
    from applyourself.contacts import referrals_by_company

    referrals_by_company(paths.PROFILE / "contacts.yaml")


def _answers() -> None:
    from applyourself.apply.bank_schema import load_answers

    load_answers()


def _resume_sources() -> None:
    from applyourself import resume_model
    from applyourself.shared.verticals import load_verticals

    problems = resume_model.check_sources(resume_model.load_sources(), load_verticals())
    if problems:
        raise ValueError("; ".join(problems))


def _stories() -> None:
    from applyourself.resume_model import parse_entries

    path = paths.STORIES
    parse_entries(path.read_text(encoding="utf-8"), path.name)


# (file, required, check). An optional file that is missing is skipped.
CHECKS: tuple[tuple[str, bool, Callable[[], None]], ...] = (
    ("verticals.yaml", True, _verticals),
    ("scoring.yaml", True, _scoring),
    ("discovery.yaml", False, _discovery),
    ("settings.yaml", False, _settings),
    ("contacts.yaml", False, _contacts),
    ("application_answers.yaml", True, _answers),
    ("resume.yaml + bullets.md + skills_master.md", True, _resume_sources),
    ("stories.md", False, _stories),
)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    from applyourself.apply.bank_schema import AnswersError
    from applyourself.shared.config_schema import moved_hint

    errors = (OSError, ValueError, KeyError, TypeError, yaml.YAMLError, AnswersError)
    failed = 0
    for name, required, check in CHECKS:
        first = paths.PROFILE / name.split(" ")[0]
        if not required and not first.exists() and moved_hint(first) is None:
            print(f"skip   {name} (not found)")
            continue
        try:
            check()
        except errors as exc:
            failed += 1
            print(f"ERROR  {name}: {exc}")
            continue
        print(f"OK     {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
