"""The learned answer store — `profile/.apply_learned.jsonl`.

`profile/application_answers.yaml` holds what the user *stated*: their
identity, their work-authorization status, their real preferences. It is small,
hand-written, commented, and read by a person when something looks wrong.

This file holds what `/apply` has *learned* from forms it has already seen:
that one board words a question a way no configured keyword covers, that
another spells an option "Acme Careers Site", that a keyword which normally
means an acknowledgment means the opposite on one particular label. That
knowledge grows with every form and nobody wants to read it — so it lives
apart, in a format built for appending rather than for reading.

Why append-only JSONL rather than editing the YAML in place:

- The YAML holds legal claims sent under the user's name. Nothing automated
  should rewrite that file, and now nothing does.
- A bad line can be deleted. A bad in-place edit of a commented YAML file
  cannot be un-made without a backup.
- Every record says which board and which job taught it, so a wrong answer is
  traceable to the form that produced it.
- No new dependency: preserving comments through a YAML round-trip needs one,
  and `pytest` here runs with `filterwarnings = ["error"]`.

The dot in the filename is load-bearing. `tests/test_profile_templates.py`
exempts dotfiles under `profile/` from the "every profile input ships an
`.example`" rule, on the grounds that they are runtime state a command writes
rather than a user input. Without the dot, that test demands a committed
template for a file whose whole point is to be machine-written.

**R7.** Nothing here calls an LLM. The `/apply` session decides *what* is worth
learning and hands it over; this module validates and appends. Same split as
`/track` owning `state.yaml` through `src/state_io.py`: judgment in the command
session, the write in `src/`.

Four record kinds, all facts, never drafted prose:

    {"kind": "wording", "group": "how did you hear",
     "wording": "where did you find out about this role", ...}
        Another way a board words a question the group already answers. Folded
        into that rule's `match:` list.

    {"kind": "option", "group": "how did you hear",
     "option": "careers site", "mode": "contains", ...}
        A spelling of an answer some board offers. Appended to that rule's
        candidate list, after everything configured, so a stated preference
        always wins.

    {"kind": "veto", "group": "ai policy",
     "wording": "did you use ai to prepare this application", ...}
        This label must NOT match this group. The one record kind that removes
        an answer instead of adding one, and the only way to fix a keyword that
        matches a question it gets backwards.

    {"kind": "answer", "wording": "have you deployed production-grade applications",
     "answer": ["Yes"], ...}
        A question no group covers, with its own answer. Becomes a rule at the
        end of the chain, so it can never shadow a configured one.

Every record also carries `job_id`, `board` and `learned` (an ISO date) for the
audit trail. Unknown keys are ignored rather than fatal: this file is a
convenience, and a stale record must never be able to block a submission.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from src import paths

PROFILE = paths.PROFILE
DEFAULT_LEARNED_PATH = PROFILE / ".apply_learned.jsonl"

KINDS = ("wording", "option", "veto", "answer")

# Same floor the YAML loader puts on a `match:` keyword: "C++" normalizes to
# "c", and a one-character substring matches most labels on any board.
MIN_WORDING_LENGTH = 3


class LearnedError(Exception):
    """A record this module refuses to write. Never raised while reading."""


@dataclass(frozen=True)
class Record:
    kind: str
    wording: str = ""
    group: str = ""
    option: str = ""
    answer: tuple[str, ...] = ()
    mode: str = ""
    job_id: str = ""
    board: str = ""
    learned: str = ""
    note: str = ""

    def as_json(self) -> dict:
        out = {"kind": self.kind}
        for key in ("group", "wording", "option", "mode", "note",
                    "job_id", "board", "learned"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.answer:
            out["answer"] = list(self.answer)
        return out


def read_records(path: Path | None = None) -> tuple[list[Record], list[str]]:
    """`(records, warnings)`. Never raises.

    A malformed line is skipped and named. The alternative — refusing to load —
    would let one bad line block every future run, and the store is a
    convenience layered on a config file that is complete without it.
    """
    p = Path(path) if path is not None else DEFAULT_LEARNED_PATH
    if not p.exists():
        return [], []

    records: list[Record] = []
    warnings: list[str] = []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [], [f"{p}: unreadable ({exc})"]

    for n, line in enumerate(lines, start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            warnings.append(f"{p}:{n}: not JSON ({exc.msg}) — skipped")
            continue
        if not isinstance(raw, dict):
            warnings.append(f"{p}:{n}: not a JSON object — skipped")
            continue
        kind = raw.get("kind")
        if kind not in KINDS:
            warnings.append(f"{p}:{n}: unknown kind {kind!r} — skipped")
            continue
        answer = raw.get("answer") or ()
        if isinstance(answer, str):
            answer = (answer,)
        records.append(Record(
            kind=kind,
            wording=_text(raw.get("wording")),
            group=_text(raw.get("group")),
            option=_text(raw.get("option")),
            answer=tuple(str(a) for a in answer),
            mode=_text(raw.get("mode")),
            job_id=_text(raw.get("job_id")),
            board=_text(raw.get("board")),
            learned=_text(raw.get("learned")),
            note=_text(raw.get("note")),
        ))
    return records, warnings


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def group_of(rule: dict) -> str:
    """The name a record uses to point at one rule: its first `match:` keyword,
    else its first `exact:` label.

    Not a new `id:` field, for two reasons. The YAML loader rejects unknown
    per-rule keys, so an `id:` would be a schema change to a file shared by
    hundreds of tests; and the loader's own cross-rule overlap check already
    guarantees no two rules can claim the same keyword, which is exactly the
    uniqueness an identifier needs.
    """
    for key in ("match", "exact"):
        values = rule.get(key) or []
        if isinstance(values, list) and values:
            return str(values[0]).strip().casefold()
    return ""


def fold(raw_rules: list, records: list[Record]) -> tuple[list, list[tuple[str, str]], list[str]]:
    """Merge learned records into the raw `rules:` list from the YAML.

    Returns `(rules, vetoes, warnings)`. `rules` goes on to the YAML loader's
    own `_parse_rules`, so every validation the hand-written file gets applies
    to the merged result too — including the overlap check, which is why a
    learned wording is absorbed *into* the rule it belongs to rather than
    appended as a new rule of its own. A new rule whose keyword is a
    superstring of an existing one is precisely what that check rejects.

    Ordering rules, both load-bearing:
    - a learned wording joins its group's `match:` list at the end, so it can
      never take precedence over a keyword the user wrote;
    - a learned option joins its group's candidate list at the end, so a
      configured preference is always tried first;
    - a learned standalone `answer` becomes a rule appended after every
      configured rule, so it can only fire where nothing else matched.
    """
    rules = [dict(r) for r in raw_rules if isinstance(r, dict)]
    vetoes: list[tuple[str, str]] = []
    warnings: list[str] = []
    appended: list[dict] = []

    # Two passes, so the store is order-independent: an `answer` record creates
    # a group, and a `wording` or `option` record may then point at it whatever
    # order the lines happen to sit in. A single pass made a record's fate
    # depend on which line came first in an append-only file, which is the one
    # thing an append-only file cannot promise.
    for record in records:
        if record.kind != "answer":
            continue
        if not record.wording or not record.answer:
            warnings.append("answer: needs both wording and answer — skipped")
            continue
        rule = {"match": [record.wording], "answer": list(record.answer)}
        if record.mode:
            rule["mode"] = record.mode
        appended.append(rule)

    by_group = {group_of(r): r for r in [*rules, *appended] if group_of(r)}

    for record in records:
        if record.kind == "answer":
            continue
        if record.kind == "veto":
            if not record.group or not record.wording:
                warnings.append("veto: needs both group and wording — skipped")
                continue
            vetoes.append((record.group.casefold(), record.wording.casefold()))
            continue

        target = by_group.get(record.group.casefold())
        if target is None:
            warnings.append(
                f"{record.kind}: no rule owns group {record.group!r} — skipped "
                f"(the group is a rule's first match: keyword)"
            )
            continue

        if record.kind == "wording":
            if not record.wording:
                warnings.append("wording: empty — skipped")
                continue
            match = list(target.get("match") or [])
            if record.wording.casefold() not in {m.casefold() for m in match}:
                match.append(record.wording)
                target["match"] = match
        elif record.kind == "option":
            if not record.option:
                warnings.append("option: empty — skipped")
                continue
            answer = target.get("answer")
            answer = [answer] if isinstance(answer, str) else list(answer or [])
            if record.option.casefold() not in {a.casefold() for a in answer}:
                answer.append(record.option)
                target["answer"] = answer
            if record.mode:
                target["mode"] = record.mode

    return rules + appended, vetoes, warnings


def append(record: Record, path: Path | None = None) -> Path:
    """Append one validated record. The only writer of this file."""
    p = Path(path) if path is not None else DEFAULT_LEARNED_PATH
    validate(record)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record.as_json(), ensure_ascii=False) + "\n")
    return p


def validate(record: Record) -> None:
    """Everything this module refuses to write, independent of the config it
    will be folded into. The caller runs the real check — a full `load_answers`
    over the merged result — because only that can see overlap and work-auth
    violations."""
    if record.kind not in KINDS:
        raise LearnedError(f"unknown kind {record.kind!r}, want one of {list(KINDS)}")

    if record.kind in ("wording", "veto", "answer") and not record.wording:
        raise LearnedError(f"{record.kind}: needs a wording")
    if record.kind in ("wording", "veto", "option") and not record.group:
        raise LearnedError(f"{record.kind}: needs a group")
    if record.kind == "option" and not record.option:
        raise LearnedError("option: needs an option string")
    if record.kind == "answer" and not record.answer:
        raise LearnedError("answer: needs at least one answer candidate")

    if record.wording and len(record.wording) < MIN_WORDING_LENGTH:
        raise LearnedError(
            f"wording {record.wording!r} is shorter than {MIN_WORDING_LENGTH} "
            f"characters — it would match nearly every label"
        )
    if record.mode and record.mode not in ("exact", "contains"):
        raise LearnedError(f"mode {record.mode!r} is not 'exact' or 'contains'")


def new_record(kind: str, **kw) -> Record:
    """A record stamped with today's date."""
    kw.setdefault("learned", date.today().isoformat())
    answer = kw.pop("answer", ())
    if isinstance(answer, str):
        answer = (answer,)
    return Record(kind=kind, answer=tuple(answer), **kw)
