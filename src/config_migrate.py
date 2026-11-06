"""`config migrate` — move a profile/ written for an older layout onto the
current one. `--dry-run` prints the diff, `--apply` writes it. Idempotent: a
migrated profile has nothing left to move.

Text edits keep a YAML file's comments; the answer bank is rewritten through
`bank.write`, which validates it and keeps the old file in `.bank_history/`.
"""

from __future__ import annotations

import difflib
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

BANK = "application_answers.yaml"
SCORING = "scoring.yaml"
DISCOVERY = "discovery.yaml"
VERTICALS = "verticals.yaml"


@dataclass
class Profile:
    """Every file a step may change, as text, plus what the steps found."""

    root: Path
    texts: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def text(self, name: str) -> str | None:
        if name not in self.texts:
            p = self.root / name
            if not p.is_file():
                return None
            self.texts[name] = p.read_text(encoding="utf-8")
        return self.texts[name]

    def data(self, name: str) -> Any:
        text = self.text(name)
        return None if text is None else yaml.safe_load(text)

    def set_bank(self, data: dict) -> None:
        self.texts[BANK] = dump_bank(data)


def dump_bank(data: dict) -> str:
    """The text `bank.write` would write."""
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


def drop_top_key(text: str, key: str) -> str:
    """text without the top-level `key:` block and the comment lines right above it."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if re.match(rf"{re.escape(key)}\s*:", ln)), None)
    if start is None:
        return text
    end = start + 1
    while end < len(lines) and (lines[end][:1] in (" ", "\t") or not lines[end].strip()):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    while start > 0 and lines[start - 1].lstrip().startswith("#"):
        start -= 1
    if start > 0 and not lines[start - 1].strip() and end < len(lines) and not lines[end].strip():
        end += 1
    return "".join(lines[:start] + lines[end:])


# ---- steps, oldest first ----


def _languages(p: Profile) -> None:
    """scoring.yaml `languages` -> the bank's `languages_fluent` question."""
    from src.score_filter import LANGUAGES_QUESTION

    scoring = p.data(SCORING)
    if not isinstance(scoring, dict) or "languages" not in scoring:
        return
    spoken = [str(x) for x in scoring.get("languages") or []]
    bank = p.data(BANK)
    if not isinstance(bank, dict):
        p.problems.append(f"{BANK}: needed to move scoring.yaml `languages`")
        return
    questions = bank.setdefault("questions", {}) or {}
    bank["questions"] = questions
    q = questions.get(LANGUAGES_QUESTION)
    if q is None:
        if spoken:
            questions[LANGUAGES_QUESTION] = {
                "answer": spoken,
                "keywords": ["languages in which you are fluent"],
            }
            p.set_bank(bank)
            p.notes.append(f"{BANK}: questions.{LANGUAGES_QUESTION} = {spoken}")
    else:
        have = q.get("answer")
        have = have if isinstance(have, list) else [have]
        if {str(x) for x in have} != set(spoken):
            p.problems.append(
                f"{SCORING} languages {spoken} differ from {BANK} questions."
                f"{LANGUAGES_QUESTION} {have}: make the bank's answer right, then "
                f"delete `languages` from {SCORING}"
            )
            return
    p.texts[SCORING] = drop_top_key(p.text(SCORING) or "", "languages")


def _flow_end(text: str, i: int) -> int:
    """Where the flow value starting at text[i] ({...}, [...], *alias or a scalar) ends."""
    if text[i] in "{[":
        depth, quote = 0, ""
        for j in range(i, len(text)):
            ch = text[j]
            if quote:
                quote = "" if ch == quote else quote
            elif ch in "\"'":
                quote = ch
            elif ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 0:
                    return j + 1
        return len(text)
    m = re.compile(r"[^,}\]\n#]*").match(text, i)
    return m.end() if m else i


def drop_flow_key(text: str, key: str) -> str:
    """text without every non-top-level `key: <value>` entry, flow or block style."""
    rx = re.compile(rf"(?m)^(?P<lead>[^#\n]*?)(?P<key>\b{re.escape(key)}:[ \t]*)")
    while m := next((m for m in rx.finditer(text) if m["lead"]), None):
        start, i = m.start("key"), m.end()
        if i < len(text) and text[i] == "&":
            i = re.compile(r"&\S+\s*").match(text, i).end()  # type: ignore[union-attr]
        if i >= len(text) or text[i] in "\n#":
            # Block style: the key's line and every line indented past it.
            line_start = text.rfind("\n", 0, start) + 1
            indent = start - line_start
            end = text.find("\n", i)
            end = len(text) if end < 0 else end + 1
            while end < len(text):
                nxt = text.find("\n", end)
                line = text[end : (len(text) if nxt < 0 else nxt + 1)]
                if line.strip() and len(line) - len(line.lstrip()) <= indent:
                    break
                end += len(line)
            while end > i and text[:end].endswith("\n\n"):
                end -= 1
            text = text[:line_start] + text[end:]
            continue
        end = _flow_end(text, i)
        after = re.compile(r"\s*,\s*").match(text, end)
        if after:
            text = text[:start] + text[after.end() :]
        else:
            before = re.search(r",\s*$", text[:start])
            text = text[: before.start() if before else start] + text[end:]
    return text


def _countries_of(bank: dict) -> tuple[str, list[str]] | None:
    """(home, every other country row) of a bank mapping, canonical names."""
    from src.shared.location.places import canonical_country

    try:
        home_raw = str(bank["identity"]["country"])
        rows = list(bank["work_authorization"]["countries"])
    except (KeyError, TypeError):
        return None
    home = canonical_country(home_raw) or home_raw
    others = [canonical_country(str(c)) for c in rows if c != "default"]
    return home, [c for c in dict.fromkeys(others) if c and c != home]


def _abroad(p: Profile) -> None:
    """discovery.yaml `sources.*.abroad` -> one top-level `abroad: {lanes, sources}`,
    its countries the bank's; a `location_allowlist` equal to the bank's goes."""
    from src.shared.location.places import canonical_country

    disc = p.data(DISCOVERY)
    if not isinstance(disc, dict):
        return
    sources = disc.get("sources") or {}
    old = {n: s["abroad"] for n, s in sources.items() if isinstance(s, dict) and "abroad" in s}
    allow = (disc.get("location_allowlist") or {}).get("countries")
    if not old and allow is None:
        return
    found = _countries_of(p.data(BANK) or {})
    if found is None:
        p.problems.append(f"{BANK}: needed to move {DISCOVERY}'s countries")
        return
    home, others = found
    text = p.text(DISCOVERY) or ""
    if old:
        lanes = [tuple((a or {}).get("verticals") or []) for a in old.values()]
        wanted = {canonical_country(c) or c for a in old.values() for c in a.get("countries") or []}
        if len(set(lanes)) != 1:
            p.problems.append(
                f"{DISCOVERY}: sources {sorted(old)} list different abroad verticals; "
                "make them one list by hand first"
            )
            return
        if wanted != set(others):
            p.problems.append(
                f"{DISCOVERY}: abroad countries {sorted(wanted)} differ from the bank's "
                f"other countries {others}: abroad now searches every bank country, so "
                "add or remove bank rows (or abroad countries) until they match"
            )
            return
        text = drop_flow_key(text, "abroad")
        block = (
            "# Search these lanes' abroad_terms on these sources in every other country\n"
            f"# {BANK} work_authorization.countries lists.\n"
            f"abroad: {{lanes: [{', '.join(lanes[0])}], sources: [{', '.join(old)}]}}\n"
        )
        lines = text.splitlines(keepends=True)
        at = next(i for i, ln in enumerate(lines) if ln.startswith("sources:"))
        while at > 0 and lines[at - 1].lstrip().startswith("#"):
            at -= 1
        text = "".join(lines[:at]) + block + "".join(lines[at:])
        p.notes.append(f"{DISCOVERY}: abroad: lanes {list(lanes[0])}, sources {list(old)}")
    if allow is not None:
        if {canonical_country(str(c)) or c for c in allow} == {home, *others}:
            text = drop_top_key(text, "location_allowlist")
            p.notes.append(f"{DISCOVERY}: location_allowlist dropped (the bank's countries)")
        elif old:
            p.notes.append(
                f"{DISCOVERY}: location_allowlist.countries kept: it differs from the bank's "
                f"countries {[home, *others]}, so it still overrides them"
            )
    new = yaml.safe_load(text)
    for s in (new.get("sources") or {}).values():
        if isinstance(s, dict) and "abroad" in s:
            p.problems.append(f"{DISCOVERY}: could not remove sources.*.abroad; edit it by hand")
            return
    p.texts[DISCOVERY] = text


def _salary(p: Profile) -> None:
    """verticals.yaml `<lane>.salary_expectation` -> `salary` on the bank's country rows."""
    from src.shared.location.places import canonical_country

    vert = p.data(VERTICALS)
    lanes_all = (vert or {}).get("verticals") or {}
    old = {
        n: v["salary_expectation"]
        for n, v in lanes_all.items()
        if isinstance(v, dict) and v.get("salary_expectation")
    }
    if not isinstance(vert, dict) or not any(
        isinstance(v, dict) and "salary_expectation" in v for v in lanes_all.values()
    ):
        return
    bank = p.data(BANK)
    found = _countries_of(bank or {})
    if found is None:
        p.problems.append(f"{BANK}: needed to move {VERTICALS}'s salary_expectation")
        return
    home = found[0]
    rows = bank["work_authorization"]["countries"]
    row_of = {canonical_country(str(k)) or str(k): k for k in rows if k != "default"}
    # country -> {lane: (amount, markup)}
    per: dict[str, dict[str, tuple[Any, Any]]] = {}
    for lane, exp in old.items():
        fallback = exp.get("fallback")
        figures = fallback if isinstance(fallback, dict) else {home: fallback}
        markup = exp.get("markup_pct")
        if markup is not None and home != "United States":
            p.problems.append(
                f"{VERTICALS}: {lane}.salary_expectation.markup_pct was a US-only markup; "
                f"set salary.markup_pct and currency on your {home} row by hand"
            )
            return
        for country, amount in figures.items():
            c = canonical_country(str(country)) or str(country)
            if amount is None:
                continue
            if c not in row_of:
                p.problems.append(
                    f"{VERTICALS}: {lane}.salary_expectation names {country}, which has no "
                    f"row in {BANK} work_authorization.countries; add the row first"
                )
                return
            per.setdefault(c, {})[lane] = (amount, markup if c == home else None)
        if markup is not None and home not in figures and home not in per:
            p.problems.append(
                f"{VERTICALS}: {lane}.salary_expectation has a markup but no {home} figure; "
                "the row's salary needs an amount"
            )
            return
    for c, by_lane in per.items():
        if len(set(by_lane.values())) != 1:
            p.problems.append(
                f"{VERTICALS}: lanes {sorted(by_lane)} ask different salaries in {c}; "
                f"one {BANK} row holds one: make them one figure by hand first"
            )
            return
        amount, markup = next(iter(by_lane.values()))
        salary: dict[str, Any] = {"amount": amount}
        if markup is not None:
            salary.update(currency="USD", markup_pct=markup)
        if set(by_lane) != set(lanes_all):
            salary["lanes"] = sorted(by_lane)
        rows[row_of[c]]["salary"] = salary
        p.notes.append(f"{BANK}: work_authorization.countries.{row_of[c]}.salary = {salary}")
    if per:
        p.set_bank(bank)
    text = drop_flow_key(p.text(VERTICALS) or "", "salary_expectation")
    new = yaml.safe_load(text) or {}
    if any("salary_expectation" in (v or {}) for v in (new.get("verticals") or {}).values()):
        p.problems.append(f"{VERTICALS}: could not remove salary_expectation; edit it by hand")
        return
    p.texts[VERTICALS] = text


STEPS: tuple[Callable[[Profile], None], ...] = (_languages, _abroad, _salary)


def plan(root: Path) -> Profile:
    """Run every step on root's files in memory."""
    p = Profile(root)
    for step in STEPS:
        step(p)
    return p


def changed(p: Profile) -> dict[str, tuple[str, str]]:
    """{file: (old text, new text)} for every file a step changed."""
    out = {}
    for name, new in p.texts.items():
        old = (p.root / name).read_text(encoding="utf-8") if (p.root / name).is_file() else ""
        if new != old:
            out[name] = (old, new)
    return out


def diff(p: Profile) -> str:
    return "".join(
        "".join(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                f"a/{name}",
                f"b/{name}",
            )
        )
        for name, (old, new) in sorted(changed(p).items())
    )


def write(p: Profile) -> list[str]:
    """Write every changed file; the bank last, through bank.write."""
    from src.apply import bank as bank_writer
    from src.shared.parquet_io import locked

    done = []
    todo = changed(p)
    for name, (_, new) in sorted(todo.items()):
        if name == BANK:
            continue
        path = p.root / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(new, encoding="utf-8")
        os.replace(tmp, path)
        done.append(name)
    if BANK in todo:
        path = p.root / BANK
        with locked(path):
            bank_writer.write(yaml.safe_load(todo[BANK][1]), path)
        done.append(BANK)
    return done


def main(args: Any) -> int:
    """`config migrate`: args has .profile, .apply."""
    from src.shared import paths

    root = Path(args.profile) if args.profile else paths.PROFILE
    p = plan(root)
    for line in p.notes:
        print(f"move   {line}")
    for line in p.problems:
        print(f"ERROR  {line}")
    if p.problems:
        return 1
    todo = changed(p)
    if not todo:
        print("nothing to migrate")
        return 0
    if BANK in todo and re.search(r"^\s*#", todo[BANK][0], re.MULTILINE):
        print(
            f"note   {BANK} is rewritten without its comments; the old file stays in .bank_history/"
        )
    if not args.apply:
        print(diff(p), end="")
        print("\ndry run: nothing written; --apply writes it")
        return 0
    from src.apply.answers import AnswersError
    from src.apply.bank import BankError

    try:
        written = write(p)
    except (AnswersError, BankError, OSError) as exc:
        print(f"ERROR  {exc}")
        return 1
    for name in written:
        print(f"wrote  {root / name}")
    print("now run: uv run config-check")
    return 0
