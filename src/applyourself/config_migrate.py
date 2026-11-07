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

from applyourself.shared.config_schema import ScoringFile, validate
from applyourself.shared.files import atomic_write_text

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
    # A block list may sit at the key's own indent ("key:\n- a").
    while end < len(lines) and (
        lines[end][:1] in (" ", "\t") or lines[end].startswith("- ") or not lines[end].strip()
    ):
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
    from applyourself.score_filter import LANGUAGES_QUESTION

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
    from applyourself.shared.countries import canonical_country

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
    from applyourself.shared.countries import canonical_country

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
            "# Search these lanes' abroad_terms (absent: their search_terms) on these\n"
            f"# sources in every other country {BANK} work_authorization.countries lists.\n"
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
    from applyourself.shared.countries import canonical_country

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
    from applyourself.apply import resume_facts

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
    from applyourself.shared import paths

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
LIFTABLE = ("years", "score")


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

    from applyourself.shared import verticals as V

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


TITLE_LISTS = ("title_include_terms", "title_strong_keep_terms", "title_exclude_terms")


def _block_end(lines: list[str], start: int) -> int:
    """The line after the top-level block at `start`, less its trailing blanks and comments."""
    end = start + 1
    while end < len(lines) and (
        lines[end][:1] in (" ", "\t", "#") or lines[end].startswith("- ") or not lines[end].strip()
    ):
        end += 1
    while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
        end -= 1
    return end


def _indented(data: Any, pad: str) -> list[str]:
    text = yaml.safe_dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    return [pad + ln for ln in text.splitlines(keepends=True)]


def _migrated_rules(
    names: list[str], keeps: dict[str, list[str]], strong: dict[str, set[str]], variants: Any
) -> list[dict]:
    """The classifier rules a lane's include and strong-keep terms become,
    most specific first: multi-word terms by word count, longest first (a
    strong-keep term in a `keep: true` rule); then, per lane, its single
    words, which pick the lane only beside another of its words. A
    title_variants group counts as one word."""
    from applyourself.shared.title_terms import word_count

    words = {n: {t: word_count(t, variants) for t in keeps.get(n, [])} for n in names}
    out: list[dict] = []
    for k in sorted({c for w in words.values() for c in w.values() if c > 1}, reverse=True):
        for n in names:
            for keep in (True, False):
                terms = [t for t, c in words[n].items() if c == k and (t in strong[n]) == keep]
                if terms:
                    out.append({"vertical": n, "terms": terms, **({"keep": True} if keep else {})})
    for n in names:
        kept = [t for t, c in words[n].items() if c == 1 and t in strong[n]]
        if kept:
            out.append({"vertical": n, "terms": kept, "keep": True})
        single = [t for t, c in words[n].items() if c == 1 and t not in strong[n]]
        if single and len(words[n]) > 1:
            out.append(
                {
                    "vertical": n,
                    "pattern": {
                        "match": single,
                        "require_any": list(words[n]),
                        "search_is_context": True,
                    },
                }
            )
        elif single:
            out.append({"vertical": n, "terms": single})
    return out


def _replay_titles(jobs: Path) -> list[tuple[str, str, str]]:
    """(title, hint, source) of every row in jobs/clean.parquet and jobs/raw/,
    each once: the titles the gate saw lately."""
    import pandas as pd

    from applyourself.discovery.config import JOBSPY_SOURCES

    seen: dict[tuple[str, str, str], None] = {}
    files = [jobs / "clean.parquet", *sorted((jobs / "raw").glob("*.parquet"))]
    for f in files:
        if not f.is_file():
            continue
        d = pd.read_parquet(f, columns=["source", "title", "vertical"])
        raw = f.parent.name == "raw"
        for src, t, v in zip(d["source"], d["title"], d["vertical"], strict=True):
            if not isinstance(t, str):
                continue
            hint = v if isinstance(v, str) and (not raw or src in JOBSPY_SOURCES) else ""
            seen[(t, hint, str(src))] = None
    return list(seen)


def _replay(
    old: Any, new: Any, rows: list[tuple[str, str, str]]
) -> tuple[list[str], dict[str, list[str]]]:
    """Both configs read with today's matcher: each title that passes before
    and drops or changes lane after, and per lane the titles that pass only after."""
    from applyourself.shared.title_gate import lane_for

    bad: list[str] = []
    gained: dict[str, list[str]] = {}
    for t, hint, src in rows:
        a = lane_for(t, hint, src, old)
        b = lane_for(t, hint, src, new)
        if a.passes and (not b.passes or b.lane != a.lane):
            bad.append(
                f"{t!r} ({src}, {hint or 'no hint'}): {a.lane} -> {b.lane or '-'} {b.reason}"
            )
        elif b.passes and not a.passes:
            gained.setdefault(b.lane, []).append(t)
    return bad, gained


def _title_lists(p: Profile) -> None:
    """Lane title lists -> verticals.yaml: a lane's include and strong-keep terms
    become classifier rules (appended last, most specific first; strong-keep
    ones `keep: true`), the exclude terms every lane shares join `defaults:
    title_exclude_terms`, and the lane lists go. Refused when any title in
    jobs/clean.parquet or jobs/raw/ that passes today would drop or change lane."""
    import tempfile

    from applyourself.shared import title_terms
    from applyourself.shared import verticals as V
    from applyourself.shared.title_gate import lane_for

    data = p.data(VERTICALS)
    if not isinstance(data, dict) or data.get("schema_version") != 2:
        return
    names = [n for n in data.get("lanes") or [] if isinstance(n, str)]
    rels = {n: f"verticals/{n}/lane.yaml" for n in names}
    raws = {n: p.data(rels[n]) for n in names}
    raws = {n: r for n, r in raws.items() if isinstance(r, dict)}
    if not any(raws[n].get(k) is not None for n in raws for k in TITLE_LISTS):
        return
    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict) or not isinstance(data.get("classifier_rules"), list):
        p.problems.append(f"{VERTICALS}: no `classifier_rules` list; move the title lists by hand")
        return
    have = [str(t).lower() for t in defaults.get("title_exclude_terms") or []]
    strong = {
        n: {str(t) for t in raws.get(n, {}).get("title_strong_keep_terms") or []} for n in names
    }
    keeps = {
        n: list(
            dict.fromkeys(
                (r.get("title_include_terms") or []) + (r.get("title_strong_keep_terms") or [])
            )
        )
        for n, r in raws.items()
    }
    excludes = [[str(t) for t in raws.get(n, {}).get("title_exclude_terms") or []] for n in names]
    variants = tuple(tuple(g) for g in data.get("title_variants") or [] if isinstance(g, list))
    from applyourself.shared import paths

    jobs = paths.JOBS if p.root.resolve() == paths.PROFILE.resolve() else p.root.parent / "jobs"
    rows = _replay_titles(jobs)
    # Every ban all lanes share moves to defaults (a `keep: true` rule beats
    # it, as strong-keep did); a defaults list already there is the one ban
    # list, so nothing joins it.
    shared = [
        t
        for t in dict.fromkeys(excludes[0] if excludes and not have else [])
        if all(t.lower() in {x.lower() for x in e} for e in excludes)
    ]

    text = p.text(VERTICALS) or ""
    lines = (text if text.endswith("\n") else text + "\n").splitlines(keepends=True)
    rules_at = next(
        (i for i, ln in enumerate(lines) if re.match(r"classifier_rules\s*:\s*$", ln)), None
    )
    if rules_at is None:
        p.problems.append(f"{VERTICALS}: `classifier_rules:` is not a block list; move by hand")
        return
    end = _block_end(lines, rules_at)
    item = next((ln for ln in lines[rules_at + 1 : end] if ln.lstrip().startswith("- ")), "  - ")
    pad = item[: len(item) - len(item.lstrip())]
    rules = _migrated_rules(names, keeps, strong, variants)
    if rules:
        lines[end:end] = [
            f"{pad}# The lanes' old title_include_terms and title_strong_keep_terms, most\n",
            f"{pad}# specific first; a single word needs another of its lane's words.\n",
            *_indented(rules, pad),
        ]
    if shared:
        at = next((i for i, ln in enumerate(lines) if re.match(r"defaults\s*:", ln)), None)
        block = [
            "  # Every lane's title ban list: whole words, any case, as written.\n",
            *_indented({"title_exclude_terms": shared}, "  "),
        ]
        if at is None:
            lanes_at = next(i for i, ln in enumerate(lines) if re.match(r"lanes\s*:", ln))
            after = _block_end(lines, lanes_at)
            lines[after:after] = ["\n", "defaults:\n", *block]
        elif "title_exclude_terms" in defaults:
            d_end = _block_end(lines, at)
            key_at = next(
                (
                    i
                    for i in range(at, d_end)
                    if re.match(r"\s+title_exclude_terms\s*:\s*$", lines[i])
                ),
                None,
            )
            items: list[int] = []
            for i in range(key_at + 1 if key_at is not None else d_end, d_end):
                if re.match(r"\s*- ", lines[i]):
                    items.append(i)
                elif lines[i].strip() and not lines[i].lstrip().startswith("#"):
                    break
            if key_at is None or not items:
                p.problems.append(
                    f"{VERTICALS}: defaults title_exclude_terms is not a block list; move by hand"
                )
                return
            last = items[-1]
            pad = lines[last][: len(lines[last]) - len(lines[last].lstrip())]
            lines[last + 1 : last + 1] = [
                pad + ln.lstrip() for ln in _indented(shared, "") if ln.strip()
            ]
        elif re.match(r"defaults\s*:\s*$", lines[at]):
            d_end = _block_end(lines, at)
            lines[d_end:d_end] = block
        else:
            p.problems.append(f"{VERTICALS}: `defaults:` is not a block mapping; move by hand")
            return
    top = "".join(lines)
    lane_texts = {}
    for n in raws:
        out = p.text(rels[n]) or ""
        for key in TITLE_LISTS:
            out = drop_top_key(out, key)
        lane_texts[rels[n]] = out

    def load(texts: dict[str, str]) -> Any:
        with tempfile.TemporaryDirectory() as tmp:
            for rel, body in texts.items():
                (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / rel).write_text(body, encoding="utf-8")
            return V.load_verticals(Path(tmp) / VERTICALS)

    before = {rel: p.text(rel) or "" for rel in (VERTICALS, *rels.values())}
    try:
        old = load(before)
        new = load({**before, VERTICALS: top, **lane_texts})
    except ValueError as exc:
        p.problems.append(f"{VERTICALS}: does not load after moving the title lists: {exc}")
        return
    bad, gained = _replay(old, new, rows)
    if bad:
        p.problems.append(
            f"{VERTICALS}: under today's matcher, moving the title lists would drop or move "
            f"{len(bad)} title(s) that pass with the lists in place; move them by hand:\n         "
            + "\n         ".join(bad[:50])
        )
        return
    p.texts[VERTICALS] = top
    p.texts.update(lane_texts)
    dropped = {
        n: len([t for t in e if t.lower() not in {x.lower() for x in shared}])
        for n, e in zip(names, excludes, strict=True)
    }
    elsewhere = [
        f"{n}: {t!r} -> {lane_for(t, '', 'greenhouse', new).lane or '-'}"
        for n in names
        for t in keeps.get(n, [])
        if title_terms.word_count(t, variants) > 1 and lane_for(t, "", "greenhouse", new).lane != n
    ]
    p.notes.append(
        f"{VERTICALS}: include/strong-keep terms -> classifier_rules for "
        f"{[n for n in names if keeps.get(n)]}; "
        + (f"defaults title_exclude_terms + {shared}; " if shared else "")
        + f"lane-only exclude terms dropped {dropped}; "
        + (
            f"replayed {len(rows)} titles from {jobs}, both configs under today's matcher: "
            "this step drops or moves none; passing only after it, per lane: "
            + (
                "; ".join(f"{n} {len(ts)} (e.g. {ts[:10]})" for n, ts in sorted(gained.items()))
                or "none"
            )
            if rows
            else f"no titles under {jobs} to replay: lane decisions NOT checked"
        )
        + (
            "; a board title holding one of these terms goes where an earlier rule "
            f"puts it: {elsewhere}"
            if elsewhere
            else ""
        )
    )


# The old judge's lane keys (four axes): read by nothing since scoring v2.
OLD_JUDGE_KEYS = ("years", "skill_weights", "focus")


def _drop_child(text: str, parent: str, key: str) -> str | None:
    """text without `key:` (and the lines indented under it) inside the
    top-level block `parent:`, or None when the block has no such key."""
    lines = (text if text.endswith("\n") else text + "\n").splitlines(keepends=True)
    at = next(
        (i for i, ln in enumerate(lines) if re.match(rf"{re.escape(parent)}\s*:\s*$", ln)), None
    )
    if at is None:
        return None
    end = _block_end(lines, at)
    for i in range(at + 1, end):
        m = re.match(rf"(\s+){re.escape(key)}\s*:", lines[i])
        if not m:
            continue
        j = i + 1
        while j < end and (
            not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > len(m[1])
        ):
            j += 1
        while j > i + 1 and not lines[j - 1].strip():
            j -= 1
        return "".join(lines[:i] + lines[j:])
    return None


def _old_rubric(text: str) -> bool:
    """A lane rubric written for the old judge's four axes."""
    return bool(re.search(r"`(?:title|skills|seniority|domain)` \(0-[23]0\)", text))


def _scoring_v2(p: Profile) -> None:
    """Scoring v2: `defaults:` gets the `score:` rules, with scoring.yaml's
    `cutoffs.sheet` as their cutoff; the old judge's `years`, `skill_weights`
    and `focus` leave `defaults:` and every lane.yaml; `jd_quality.skills_cap`
    goes (a short JD keeps its fit unless a `fit_cap` is set). Refused unless the loaded config
    stays the same apart from `score`, and the sheet line with it."""
    import dataclasses
    import tempfile

    from applyourself.score_filter import CUTOFF_DEFAULT
    from applyourself.shared import verticals as V

    data = p.data(VERTICALS)
    scoring = p.data(SCORING)
    if not isinstance(data, dict) or data.get("schema_version") != 2:
        return
    if not isinstance(scoring, dict):
        return
    names = [n for n in data.get("lanes") or [] if isinstance(n, str)]
    rels = {n: f"verticals/{n}/lane.yaml" for n in names}

    def sub(data: dict[str, Any], key: str) -> dict[str, Any]:
        value = data.get(key)
        return value if isinstance(value, dict) else {}

    defaults, cutoffs = sub(data, "defaults"), sub(scoring, "cutoffs")
    quality = sub(scoring, "jd_quality")
    lane_old = {n: [k for k in OLD_JUDGE_KEYS if k in (p.data(rels[n]) or {})] for n in names}
    todo = (
        "score" not in defaults
        or any(k in defaults for k in OLD_JUDGE_KEYS)
        or any(lane_old.values())
        or "sheet" in cutoffs
        or "skills_cap" in quality
    )
    if not todo:
        return
    sheet = cutoffs.get("sheet")
    top = p.text(VERTICALS) or ""
    for key in OLD_JUDGE_KEYS:
        if key in defaults:
            cut = _drop_child(top, "defaults", key)
            if cut is None:
                p.problems.append(
                    f"{VERTICALS}: could not remove defaults `{key}`; remove it by hand"
                )
                return
            top = cut
    if "score" not in defaults:
        score = {**V.SCORE_DEFAULTS, "cutoff": sheet if type(sheet) is int else CUTOFF_DEFAULT}
        block = [
            "  # /score: fit less the larger of the level and years penalties; 0 from\n",
            "  # years_zero_at years. cutoff: the sheet and retention line. A lane's\n",
            "  # own `score:` keys replace these one by one.\n",
            "  score:\n",
            *(
                "    " + ln
                for ln in yaml.safe_dump(
                    score, default_flow_style=None, sort_keys=False, width=200
                ).splitlines(keepends=True)
            ),
        ]
        lines = (top if top.endswith("\n") else top + "\n").splitlines(keepends=True)
        at = next((i for i, ln in enumerate(lines) if re.match(r"defaults\s*:", ln)), None)
        if at is None:
            lines += ["\n", "defaults:\n", *block]
        elif re.match(r"defaults\s*:\s*$", lines[at]):
            lines[at + 1 : at + 1] = block
        else:
            p.problems.append(
                f"{VERTICALS}: `defaults:` is not a block mapping; add `score:` by hand"
            )
            return
        top = "".join(lines)
    elif "sheet" in cutoffs and "cutoff" not in (defaults.get("score") or {}):
        p.problems.append(
            f"{VERTICALS}: defaults `score:` has no cutoff; set it to cutoffs.sheet ({sheet}) "
            f"and remove `sheet` from {SCORING} by hand"
        )
        return
    lane_texts = {}
    for n, keys in lane_old.items():
        out = p.text(rels[n]) or ""
        for key in keys:
            out = drop_top_key(out, key)
        if keys:
            lane_texts[rels[n]] = out
    stext = p.text(SCORING) or ""
    if "sheet" in cutoffs:
        cut = _drop_child(stext, "cutoffs", "sheet")
        if cut is None:
            p.problems.append(f"{SCORING}: could not remove cutoffs.sheet; remove it by hand")
            return
        stext = cut.replace(
            "# suggested_action from the fit total. The judge may only lower it.\n",
            "# suggested_action from the score. The sheet line is each lane's score.cutoff\n"
            "# (verticals.yaml); keep_long keeps roles in the live view from a lower score.\n",
        )
    cap = quality.get("skills_cap")
    if "skills_cap" in quality:
        lines = stext.splitlines(keepends=True)
        hit = [i for i, ln in enumerate(lines) if re.match(r"\s+skills_cap\s*:", ln)]
        if len(hit) != 1:
            p.problems.append(
                f"{SCORING}: could not remove jd_quality.skills_cap; remove it by hand"
            )
            return
        del lines[hit[0]]
        stext = "".join(lines).replace(
            "# A JD under min_words has its skills axis capped at skills_cap.",
            "# A JD under min_words is tagged [short JD]; an optional fit_cap caps its fit.",
        )

    def load(texts: dict[str, str]) -> Any:
        with tempfile.TemporaryDirectory() as tmp:
            for rel, body in texts.items():
                (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / rel).write_text(body, encoding="utf-8")
            return V.load_verticals(Path(tmp) / VERTICALS)

    before = {rel: p.text(rel) or "" for rel in (VERTICALS, *rels.values())}
    try:
        old = load(before)
        new = load({**before, VERTICALS: top, **lane_texts})
        validate(ScoringFile, yaml.safe_load(stext), p.root / SCORING)
    except ValueError as exc:
        p.problems.append(f"scoring v2: does not load after the move: {exc}")
        return
    same = V.ScoreRules({}, {}, 1)

    def rest(cfg: Any) -> Any:
        return {n: dataclasses.replace(v, score=same) for n, v in cfg.verticals.items()}

    line = {n: v.score.cutoff for n, v in new.verticals.items()}
    was = {
        n: v.score.cutoff if v.score.cutoff is not None else sheet for n, v in old.verticals.items()
    }
    if rest(old) != rest(new) or (type(sheet) is int and line != was):
        p.problems.append("scoring v2: the move would change the loaded config; move by hand")
        return
    p.texts[VERTICALS] = top
    p.texts.update(lane_texts)
    p.texts[SCORING] = stext
    p.notes.append(
        f"{VERTICALS}: defaults score: cutoff {line}; old judge keys removed "
        f"{ {n: k for n, k in lane_old.items() if k} or 'none' }"
        + (f"; {SCORING}: cutoffs.sheet -> score.cutoff" if "sheet" in cutoffs else "")
        + (f"; jd_quality.skills_cap {cap} removed" if "skills_cap" in quality else "")
    )
    for n in names:
        rubric = p.root / "verticals" / n / "rubric.md"
        if rubric.is_file() and _old_rubric(rubric.read_text(encoding="utf-8")):
            p.notes.append(
                f"verticals/{n}/rubric.md grades the old four axes: rewrite it for the "
                "fit bands (keep the old one as rubric.v1.md for `score ab-stage`)"
            )


STEPS: tuple[Callable[[Profile], None], ...] = (
    _languages,
    _abroad,
    _salary,
    _resume_facts,
    _settings,
    _state,
    _split_lanes,
    _title_lists,
    _scoring_v2,
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
    from applyourself.apply import bank as bank_writer
    from applyourself.shared.parquet_io import locked

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
    from applyourself.shared import paths

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
    from applyourself.apply.bank import BankError
    from applyourself.apply.bank_schema import AnswersError

    try:
        written = write(p)
    except (AnswersError, BankError, OSError) as exc:
        print(f"ERROR  {exc}")
        return 1
    for name in written:
        print(f"wrote  {root / name}")
    print("now run: uv run config-check")
    return 0
