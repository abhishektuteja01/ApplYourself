"""`config-check` — load and validate every profile/ config file, one OK or
ERROR line per file. Exits 1 on any error. Read-only (R7: no LLM)."""

from __future__ import annotations

from collections.abc import Callable

import yaml

from src.shared import paths


def _verticals() -> None:
    from src.shared.verticals import load_verticals

    load_verticals()


def _scoring() -> None:
    from src.score_filter import load_scoring

    load_scoring()


def _discovery() -> None:
    from src.discovery.config import load_config

    problems = load_config().validate()
    if problems:
        raise ValueError("; ".join(problems))


def _automation() -> None:
    from src.shared.config_schema import load_automation

    load_automation(paths.PROFILE / "automation.yaml")


def _contacts() -> None:
    from src.contacts import referrals_by_company

    referrals_by_company(paths.PROFILE / "contacts.yaml")


def _answers() -> None:
    from src.apply.answers import load_answers

    load_answers()


def _resume_sources() -> None:
    from src import resume_model
    from src.shared.verticals import load_verticals

    problems = resume_model.check_sources(resume_model.load_sources(), load_verticals())
    if problems:
        raise ValueError("; ".join(problems))


# (file, required, check). An optional file that is missing is skipped.
CHECKS: tuple[tuple[str, bool, Callable[[], None]], ...] = (
    ("verticals.yaml", True, _verticals),
    ("scoring.yaml", True, _scoring),
    ("discovery.yaml", False, _discovery),
    ("automation.yaml", False, _automation),
    ("contacts.yaml", False, _contacts),
    ("application_answers.yaml", True, _answers),
    ("resume.yaml + bullets.md + skills_master.md", True, _resume_sources),
)


def main(argv: list[str] | None = None) -> int:
    from src.apply.answers import AnswersError

    errors = (OSError, ValueError, KeyError, TypeError, yaml.YAMLError, AnswersError)
    failed = 0
    for name, required, check in CHECKS:
        first = paths.PROFILE / name.split(" ")[0]
        if not required and not first.exists():
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
