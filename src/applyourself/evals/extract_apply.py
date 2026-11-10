"""Build the private /apply eval set from the real bank and past runs.

Every case is a label some saved `plan.json` showed, so its kind and options
are the board's own. Sources: the bank's question wordings, `fact_wordings`
and `claims`, confirmed `answers_override.json` files, and the yes rows of
`apply_runs/wordings_review.yaml`. `relabel.yaml` beside the output, when
present, replaces the expectation of every case with a listed label. Writes
`applications/evals/apply/private/cases.jsonl` (gitignored); prints counts only,
never a value."""

from __future__ import annotations

import argparse
import collections
import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import yaml

from applyourself.apply.bank_schema import Answers, load_answers, norm_label
from applyourself.evals.cases import REAL_PERSONA, Case, Expect, dump_case, private_dir
from applyourself.shared import paths

#: Kinds whose answer must be one of the board's options.
CHOICE_KINDS = frozenset(
    {"select", "react_select", "combobox", "radio_group", "checkbox_group", "yesno"}
)
#: Fields the judge never sees (contract C2) plus the structural blocks.
_CLOSED_TIERS = ("A", "A2")
_CLOSED_SECTIONS = ("eeoc", "demographic", "education", "employment")
_PLAN_LISTS = ("fields", "unmapped", "draftable", "skipped")
#: Override sources that mark a written answer, not a fixed one.
_PROSE_SOURCES = ("B-", "S-", "SKILL-", "company_answers", "jd")
_CLAIM_FACTS = ("authorized", "requires_sponsorship")


@dataclass
class PlanField:
    field: dict
    job_country: str


def _option_key(text: str) -> str:
    """An option as the bank keys its claims: casefolded, single-spaced, punctuation kept."""
    text = unicodedata.normalize("NFKC", str(text)).replace("’", "'")
    return " ".join(text.casefold().split())


def _judge_field(raw: dict) -> dict | None:
    """The C2 view of one plan field, or None when the judge never sees it."""
    if raw.get("tier") in _CLOSED_TIERS or raw.get("section") in _CLOSED_SECTIONS:
        return None
    kind = raw.get("kind")
    if not kind or kind == "file":
        return None
    return {
        "label": str(raw.get("label") or ""),
        "kind": str(kind),
        "options": [str(o) for o in raw.get("options") or []],
        "description": str(raw.get("description") or ""),
        "required": bool(raw.get("required")),
        "multi": bool(raw.get("multi")),
    }


def read_plans(root: Path) -> tuple[dict[str, dict[str, PlanField]], int]:
    """Role folder -> field id -> its judge field; and how many plan files were unreadable."""
    by_dir: dict[str, dict[str, PlanField]] = {}
    bad = 0
    for path in sorted(root.glob("*/*/plan.json")):
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            bad += 1
            continue
        if not isinstance(plan, dict):
            bad += 1
            continue
        country = str((plan.get("work_authorization") or {}).get("job_country") or "")
        fields: dict[str, PlanField] = {}
        for name in _PLAN_LISTS:
            for raw in plan.get(name) or []:
                if not isinstance(raw, dict) or not raw.get("id"):
                    continue
                judged = _judge_field(raw)
                if judged is not None and raw["id"] not in fields:
                    fields[str(raw["id"])] = PlanField(judged, country)
        by_dir[str(path.parent)] = fields
    return by_dir, bad


def _by_label(by_dir: dict[str, dict[str, PlanField]]) -> dict[str, list[PlanField]]:
    out: dict[str, list[PlanField]] = collections.defaultdict(list)
    for fields in by_dir.values():
        for pf in fields.values():
            out[norm_label(pf.field["label"])].append(pf)
    return out


class Builder:
    def __init__(self) -> None:
        self.cases: list[Case] = []
        self.seen: set[tuple[str, tuple[str, ...]]] = set()
        self.counts: collections.Counter[str] = collections.Counter()
        self.skipped: collections.Counter[str] = collections.Counter()

    def add(self, group: str, source: str, pf: PlanField, expect: dict) -> None:
        field = pf.field
        if field["kind"] in CHOICE_KINDS and not field["options"]:
            self.skipped[f"{source}: options unknown"] += 1
            return
        key = (norm_label(field["label"]), tuple(_option_key(o) for o in field["options"]))
        if key in self.seen:
            self.skipped[f"{source}: duplicate"] += 1
            return
        self.seen.add(key)
        self.counts[group] += 1
        case_id = f"prv-{source}-{self.counts[group]:03d}"
        self.cases.append(
            Case.model_validate(
                {
                    "id": case_id,
                    "group": group,
                    "persona": REAL_PERSONA,
                    "job_country": pf.job_country,
                    "field": dict(field),
                    "expect": expect,
                }
            )
        )


def _claim_true(claim: dict, answers: Answers, country: str) -> bool:
    place = claim.get("country") or country
    if not place:
        return False
    facts = answers.facts_for(place)
    if claim.get("holds") is False:
        return False
    return all(getattr(facts, k) == claim[k] for k in _CLAIM_FACTS if k in claim)


def from_bank(b: Builder, raw: dict, answers: Answers, labels, by_dir) -> None:
    for qid, spec in (raw.get("questions") or {}).items():
        for wording in (spec or {}).get("wordings") or []:
            matches = labels.get(norm_label(str(wording)))
            if not matches:
                b.skipped["bank: no plan field"] += 1
                continue
            for pf in matches:
                b.add("private_bank", "bank", pf, {"action": "fill", "question": qid})
    for label, spec in (raw.get("fact_wordings") or {}).items():
        matches = labels.get(norm_label(str(label)))
        if not matches:
            b.skipped["fact_wordings: no plan field"] += 1
            continue
        fact = {"fact": spec["fact"], "yes_when": spec["yes_when"]}
        for pf in matches:
            b.add("work_auth_corpus", "factw", pf, {"action": "fill", "fact": fact})
    # A claim is a case only on a board whose full option list a plan saved.
    for fields in by_dir.values():
        for pf in fields.values():
            options = pf.field["options"]
            claimed = [o for o in options if answers.claims.get(_option_key(o))]
            if not claimed:
                continue
            true = [
                o
                for o in claimed
                if _claim_true(answers.claims[_option_key(o)], answers, pf.job_country)
            ]
            if not true:
                b.skipped["claims: no claimed option true"] += 1
                continue
            b.add("sentence_option", "claim", pf, {"action": "fill", "one_of": true})


def _override_expect(entry: dict, pf: PlanField, answers: Answers) -> dict | None:
    if isinstance(entry.get("fact"), dict):
        return {"action": "fill", "fact": dict(entry["fact"])}
    qid = entry.get("question")
    if qid and answers.question(str(qid)) is not None:
        return {"action": "fill", "question": str(qid)}
    value = entry.get("value")
    if value in (None, "", []):
        return None
    source = str(entry.get("source") or "")
    if pf.field["kind"] not in CHOICE_KINDS and source.startswith(_PROSE_SOURCES):
        return {"action": "fill", "prose": True}
    if isinstance(value, list):
        return {"action": "fill", "value": [str(v) for v in value]}
    return {"action": "fill", "value": str(value)}


def from_overrides(b: Builder, root: Path, answers: Answers, labels, by_dir) -> None:
    for path in sorted(root.glob("*/*/answers_override.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            b.skipped["override: unreadable"] += 1
            continue
        if not isinstance(raw, dict) or raw.get("confirmed") is not True:
            b.skipped["override: not confirmed"] += 1
            continue
        fields = by_dir.get(str(path.parent), {})
        for fid, entry in raw.items():
            if fid in ("job_id", "confirmed", "country") or not isinstance(entry, dict):
                continue
            pf = fields.get(fid)
            if pf is None:
                found = labels.get(norm_label(str(entry.get("label") or "")))
                pf = found[0] if found else None
            if pf is None:
                b.skipped["override: no plan field"] += 1
                continue
            if raw.get("country") and not pf.job_country:
                pf = PlanField(pf.field, str(raw["country"]))
            expect = _override_expect(entry, pf, answers)
            if expect is None:
                b.skipped["override: no value"] += 1
                continue
            b.add("private_override", "ovr", pf, expect)


def from_review(b: Builder, path: Path, labels) -> None:
    if not path.is_file():
        return
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for row in data.get("wordings") or []:
        if not isinstance(row, dict) or str(row.get("decision") or "").strip().lower() != "yes":
            continue
        matches = labels.get(norm_label(str(row.get("label") or "")))
        if not matches:
            b.skipped["review: no plan field"] += 1
            continue
        fact = {"fact": str(row.get("fact")), "yes_when": True}
        for pf in matches:
            b.add("work_auth_corpus", "review", pf, {"action": "fill", "fact": fact})


class RelabelError(Exception):
    """A relabel entry is malformed or names a value the field does not offer."""


def relabel(cases: list[Case], path: Path) -> tuple[collections.Counter[str], int]:
    """Replace each case's expectation with the one `path` gives its label.

    `path` is `relabel: [{label, expect}]`; labels match as the bank matches them.
    Returns the changed count per group and how many entries matched no case."""
    changed: collections.Counter[str] = collections.Counter()
    if not path.is_file():
        return changed, 0
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    by_label: dict[str, dict] = {}
    for row in data.get("relabel") or []:
        if (
            not isinstance(row, dict)
            or not row.get("label")
            or not isinstance(row.get("expect"), dict)
        ):
            raise RelabelError(f"{path}: each entry needs label and expect")
        by_label[norm_label(str(row["label"]))] = row["expect"]
    used: set[str] = set()
    for case in cases:
        key = norm_label(case.field.label)
        if key not in by_label:
            continue
        used.add(key)
        expect = Expect.model_validate(by_label[key])
        if case.field.kind in CHOICE_KINDS:
            offered = {_option_key(o) for o in case.field.options}
            value = expect.value
            picked = expect.one_of or (value if isinstance(value, list) else [value or ""])
            missing = [v for v in picked if v and _option_key(v) not in offered]
            if missing:
                raise RelabelError(
                    f"{path}: {case.id}: {len(missing)} value(s) not among its options"
                )
        case.expect = expect
        changed[case.group] += 1
    return changed, len(set(by_label) - used)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bank", type=Path, help="answer bank (default: profile's)")
    parser.add_argument("--out", type=Path, help="cases.jsonl (default: the private eval dir)")
    args = parser.parse_args(argv)

    bank_path = args.bank or paths.ANSWER_BANK
    raw = yaml.safe_load(bank_path.read_text(encoding="utf-8")) or {}
    answers = load_answers(bank_path)
    root = paths.APPLICATIONS
    by_dir, bad = read_plans(root)
    labels = _by_label(by_dir)

    b = Builder()
    # Confirmed overrides first: on a duplicate label, what the user confirmed wins.
    from_overrides(b, root, answers, labels, by_dir)
    from_bank(b, raw, answers, labels, by_dir)
    from_review(b, paths.APPLY_RUNS / "wordings_review.yaml", labels)

    out = args.out or private_dir() / "cases.jsonl"
    changed, unmatched = relabel(b.cases, out.parent / "relabel.yaml")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(dump_case(c) + "\n" for c in b.cases), encoding="utf-8")

    print(f"{len(b.cases)} cases -> {out}")
    print(f"plans: {len(by_dir)} read, {bad} unreadable (skipped)")
    for group, n in sorted(b.counts.items()):
        print(f"  {group}: {n}")
    for group, n in sorted(changed.items()):
        print(f"  relabeled {group}: {n}")
    if unmatched:
        print(f"  relabel entries matching no case: {unmatched}")
    for reason, n in sorted(b.skipped.items()):
        print(f"  skipped {reason}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
