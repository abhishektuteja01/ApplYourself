"""`config migrate` — move a profile/ written for an older layout onto the
current one. `--dry-run` prints the diff, `--apply` writes it. Idempotent: a
migrated profile has nothing left to move.

Text edits keep a YAML file's comments; the answer bank is rewritten through
`bank.write`, which validates it and keeps the old file in `.state/bank_history/`.
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

from src.shared.files import atomic_write_text

BANK = "application_answers.yaml"
SCORING = "scoring.yaml"
DISCOVERY = "discovery.yaml"
VERTICALS = "verticals.yaml"
RESUME = "resume.yaml"
SETTINGS = "settings.yaml"
OLD_SETTINGS = "automation.yaml"


@dataclass
class Profile:
    """Every file a step may change, as text, plus what the steps found."""

    root: Path
    texts: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    renamed: dict[str, str] = field(default_factory=dict)
    """new name -> old name: the new file's text starts from the old one's."""
    moves: list[tuple[str, str]] = field(default_factory=list)
    """(from, to), files or folders moved as they are, before any text is written."""
    deletes: list[str] = field(default_factory=list)

    def text(self, name: str) -> str | None:
        if name not in self.texts:
            p = self.root / self.renamed.get(name, name)
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
    from src.shared.countries import canonical_country

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
    from src.shared.countries import canonical_country

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
    from src.shared.countries import canonical_country

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


def _resume_facts(p: Profile) -> None:
    """Bank schema 2 -> 3: identity, education and employment keys that equal what
    resume.yaml gives are dropped (read from it now); any that differ stay, listed."""
    from src.apply import resume_facts

    bank = p.data(BANK)
    if not isinstance(bank, dict) or bank.get("schema_version") != 2:
        return
    resume = p.data(RESUME)
    if not isinstance(resume, dict):
        p.problems.append(f"{RESUME}: needed to move the bank to schema 3")
        return
    for key, facts in (
        ("identity", resume_facts.identity),
        ("education", resume_facts.education),
        ("employment", resume_facts.employment),
    ):
        block = bank.get(key)
        if not isinstance(block, dict):
            continue
        derived = facts(resume)
        for k in list(block):
            if k not in derived:
                continue
            same = (
                block[k] == derived[k]
                if isinstance(derived[k], bool)
                else not isinstance(block[k], bool) and str(block[k]) == str(derived[k])
            )
            if same:
                del block[k]
            else:
                p.notes.append(
                    f"{BANK}: {key}.{k} kept: {block[k]!r} (resume.yaml gives {derived[k]!r})"
                )
    bank["schema_version"] = 3
    p.set_bank(bank)
    p.notes.append(f"{BANK}: schema_version 3; identity, education and employment read {RESUME}")


def _settings(p: Profile) -> None:
    """automation.yaml -> settings.yaml, and the bank's `browser:` block into it."""
    if (p.root / OLD_SETTINGS).is_file():
        if (p.root / SETTINGS).is_file():
            p.problems.append(
                f"{OLD_SETTINGS} and {SETTINGS} both exist: move what {OLD_SETTINGS} "
                f"still holds into {SETTINGS} by hand, then delete {OLD_SETTINGS}"
            )
            return
        p.renamed[SETTINGS] = OLD_SETTINGS
        p.texts[SETTINGS] = p.text(SETTINGS) or ""
        p.notes.append(f"{OLD_SETTINGS} -> {SETTINGS}")
    bank = p.data(BANK)
    if not isinstance(bank, dict) or "browser" not in bank:
        return
    have = (p.data(SETTINGS) or {}).get("browser")
    if have is not None and have != bank["browser"]:
        p.problems.append(
            f"{BANK} browser: {bank['browser']} differs from {SETTINGS} browser: {have}; "
            f"keep one in {SETTINGS} and delete the other"
        )
        return
    if have is None:
        text = (p.text(SETTINGS) or "").rstrip("\n")
        block = yaml.safe_dump({"browser": bank["browser"]}, sort_keys=False)
        p.texts[SETTINGS] = (
            (text + "\n\n" if text else "")
            + "# Which browser drives the /apply form. patchright | playwright.\n"
            + block
        )
    del bank["browser"]
    p.set_bank(bank)
    p.notes.append(f"{BANK} browser: -> {SETTINGS}")


STATE = ".state"
# What the tools write, (old place, place under .state/).
STATE_MOVES = (
    (".bank_history", "bank_history"),
    (".synonym_history", "synonym_history"),
    ("synonyms_declined.yaml", "synonyms_declined.yaml"),
)
DE_AI = "de_ai_rules.yaml"


def _state(p: Profile) -> None:
    """Tool-written files -> .state/; a de_ai_rules.yaml equal to the shipped
    default goes (data/defaults/ holds it), a different one stays as the override."""
    from src.shared import paths

    for old, new in STATE_MOVES:
        src, dst = p.root / old, p.root / STATE / new
        if not src.exists():
            continue
        if dst.is_file():
            p.problems.append(f"{old} and {STATE}/{new} both exist: keep one by hand")
            continue
        p.moves.append((old, f"{STATE}/{new}"))
        p.notes.append(f"{old} -> {STATE}/{new}")
    rules = p.text(DE_AI)
    if rules is None:
        return
    if rules == paths.default(DE_AI).read_text(encoding="utf-8"):
        p.deletes.append(DE_AI)
        p.notes.append(f"{DE_AI} removed: the same as data/defaults/{DE_AI}")
    else:
        p.notes.append(f"{DE_AI} kept: it differs from data/defaults/{DE_AI} and replaces it")


# Lane keys `defaults:` may take over when every lane holds the same value.
LIFTABLE = ("years",)


def _dedent(lines: list[str], n: int) -> list[str]:
    return [ln[min(n, len(ln) - len(ln.lstrip(" "))) :] for ln in lines]


def _lanes_text(text: str) -> tuple[int, int, list[tuple[str, list[str], list[str]]]] | None:
    """(start, end, [(lane, lead comment lines, body lines)]) of the top-level
    `verticals:` block in text's lines, or None when its shape is not the usual one."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if re.match(r"verticals\s*:\s*$", ln)), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and (
        lines[end][:1] in (" ", "\t") or not lines[end].strip() or lines[end].startswith("#")
    ):
        end += 1
    while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
        end -= 1
    keys = [i for i in range(start + 1, end) if re.match(r"  [a-z][a-z0-9_]*:\s*$", lines[i])]
    if not keys:
        return None
    leads = []
    for k in keys:
        s = k
        while s > start + 1 and lines[s - 1].lstrip().startswith("#") and lines[s - 1][:3] != "   ":
            s -= 1
        leads.append(s)
    if any(ln.strip() for ln in lines[start + 1 : leads[0]]):
        return None
    out = []
    for n, k in enumerate(keys):
        stop = leads[n + 1] if n + 1 < len(keys) else end
        body = lines[k + 1 : stop]
        while body and not body[-1].strip():
            body.pop()
        if any(
            ln.strip() and not ln.startswith("    ") and not ln.lstrip().startswith("#")
            for ln in body
        ):
            return None
        out.append(
            (lines[k].strip().rstrip(":"), _dedent(lines[leads[n] : k], 2), _dedent(body, 4))
        )
    return start, end, out


def _split_lanes(p: Profile) -> None:
    """verticals.yaml schema 1 -> 2: each lane block to verticals/<lane>/lane.yaml,
    `lanes:` keeps their order, and a LIFTABLE key every lane holds alike goes to
    `defaults:`. Refused unless the loaded config stays exactly the same."""
    import tempfile

    from src.shared import verticals as V

    data = p.data(VERTICALS)
    if not isinstance(data, dict) or data.get("schema_version") != 1 or "verticals" not in data:
        return
    text = p.text(VERTICALS) or ""
    found = _lanes_text(text)
    if found is None:
        p.problems.append(f"{VERTICALS}: could not find each lane block; split it by hand")
        return
    start, end, blocks = found
    raw = data["verticals"]
    names = [name for name, _, _ in blocks]
    if names != list(raw):
        p.problems.append(f"{VERTICALS}: lanes read {names}, the file holds {list(raw)}")
        return
    lifted = {
        key: raw[names[0]][key]
        for key in LIFTABLE
        if len(names) > 1
        and all(key in raw[n] for n in names)
        and all(raw[n][key] == raw[names[0]][key] for n in names)
    }
    lines = text.splitlines(keepends=True)
    block = [
        "# Shortlist order (first = primary). Each lane is verticals/<lane>/lane.yaml.\n",
        f"lanes: [{', '.join(names)}]\n",
    ]
    if lifted:
        block += [
            "\n",
            "# Under every lane's lane.yaml: a lane's list comes first, then the list here;\n",
            "# any other key a lane sets replaces the one here.\n",
            "defaults:\n",
            *(
                "  " + ln
                for ln in yaml.safe_dump(
                    lifted, default_flow_style=None, sort_keys=False, allow_unicode=True
                ).splitlines(keepends=True)
            ),
        ]
    tail = lines[end:]
    while tail and not tail[0].strip():
        tail.pop(0)
    top = "".join(lines[:start] + block + (["\n"] if tail else []) + tail)
    top = re.sub(r"(?m)^schema_version:\s*1\b", "schema_version: 2", top, count=1)
    lane_texts = {}
    for name, lead, body in blocks:
        out = "".join(
            [
                f"# Lane `{name}`: listed in {VERTICALS} `lanes:`, merged over its `defaults:`.\n",
                *lead,
                *body,
            ]
        )
        for key in lifted:
            out = drop_top_key(out, key)
        rel = f"verticals/{name}/lane.yaml"
        if (p.root / rel).exists():
            p.problems.append(f"{rel} already exists; merge it with {VERTICALS} by hand")
            return
        lane_texts[rel] = out
    try:
        old = V.build(data, {n: (raw[n], p.root / VERTICALS) for n in names}, p.root / VERTICALS)
        with tempfile.TemporaryDirectory() as tmp:
            for rel, body_text in {VERTICALS: top, **lane_texts}.items():
                (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / rel).write_text(body_text, encoding="utf-8")
            new = V.load_verticals(Path(tmp) / VERTICALS)
    except ValueError as exc:
        p.problems.append(f"{VERTICALS}: does not load: {exc}")
        return
    if _shape(old) != _shape(new):
        p.problems.append(f"{VERTICALS}: the split would change the loaded config; split by hand")
        return
    p.texts[VERTICALS] = top
    p.texts.update(lane_texts)
    p.notes.append(
        f"{VERTICALS}: lanes {names} -> verticals/<lane>/lane.yaml"
        + (f"; defaults: {sorted(lifted)}" if lifted else "")
    )


def _shape(cfg: Any) -> tuple:
    """Everything a loaded config holds, comparable with ==."""
    return (
        cfg.default_vertical,
        tuple((n, v) for n, v in cfg.verticals.items()),
        tuple((v, repr(r)) for v, r in cfg.classifier_rules),
    )


STEPS: tuple[Callable[[Profile], None], ...] = (
    _languages,
    _abroad,
    _salary,
    _resume_facts,
    _settings,
    _state,
    _split_lanes,
)


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
        src = p.root / p.renamed.get(name, name)
        old = src.read_text(encoding="utf-8") if src.is_file() else ""
        if new != old or name in p.renamed:
            out[name] = (old, new)
    return out


def diff(p: Profile) -> str:
    """A unified diff per changed file; a new file is named with its size."""
    return "".join(
        f"+++ new file b/{name} ({new.count(chr(10))} lines)\n"
        if not old and name not in p.renamed
        else "".join(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                f"a/{p.renamed.get(name, name)}",
                f"b/{name}",
            )
        )
        for name, (old, new) in sorted(changed(p).items())
    )


def _move(src: Path, dst: Path) -> None:
    """src onto dst; a folder's files join whatever dst already holds."""
    if src.is_dir():
        for child in sorted(src.iterdir()):
            _move(child, dst / child.name)
        src.rmdir()
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)


def write(p: Profile) -> list[str]:
    """Move, then write every changed file, the bank last through bank.write,
    then delete."""
    from src.apply import bank as bank_writer
    from src.shared.parquet_io import locked

    done = []
    for old, new in p.moves:
        _move(p.root / old, p.root / new)
        done.append(new)
    todo = changed(p)
    for name, (_, new) in sorted(todo.items()):
        if name == BANK:
            continue
        path = p.root / name
        atomic_write_text(path, new, mkdir=True)
        done.append(name)
        if name in p.renamed:
            (p.root / p.renamed[name]).unlink()
    if BANK in todo:
        path = p.root / BANK
        with locked(path):
            bank_writer.write(yaml.safe_load(todo[BANK][1]), path)
        done.append(BANK)
    for name in p.deletes:
        (p.root / name).unlink()
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
    if not (todo or p.moves or p.deletes):
        print("nothing to migrate")
        return 0
    if BANK in todo and re.search(r"^\s*#", todo[BANK][0], re.MULTILINE):
        print(
            f"note   {BANK} is rewritten without its comments; the old file stays in .state/bank_history/"
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
