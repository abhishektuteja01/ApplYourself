"""Convert a schema 1 answer config plus `.apply_learned.jsonl` into one schema 2
answer bank. One-off.

    uv run python scripts/migrate_answer_bank.py [--answers P] [--learned P]
        [--out P] [--start-in-days N]

The old file is kept beside the new one as `<name>.v1.yaml`. Each rule becomes
one question (id = its first keyword); learned wordings join their question,
learned options append to its candidates, vetoes become `not:`, and learned
standalone answers become questions of their own. Work authorization becomes a
per-country table: your home country from `status`, each nationality as a
citizen's row, and a `default` row (not authorized, needs sponsorship) for the
rest. Option strings from `status_option_candidates` become claims that hold in
your home country; review them — a claim that states a fact is stricter.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from src import paths  # noqa: E402
from src.apply import answers as A  # noqa: E402

_SLUG = re.compile(r"[^a-z0-9]+")

STATUS_FACTS = {
    "citizen_or_pr": {"authorized": True, "requires_sponsorship": False, "unrestricted": True},
    "needs_sponsorship_now": {"authorized": False, "requires_sponsorship": True},
    "time_limited": {"authorized": True, "requires_sponsorship": True},
}


def slug(text: str) -> str:
    return _SLUG.sub("_", A.norm_label(text)).strip("_")[:48] or "question"


def _unique(qid: str, taken: set[str]) -> str:
    base, n = qid, 2
    while qid in taken:
        qid = f"{base}_{n}"
        n += 1
    taken.add(qid)
    return qid


def _answer_spec(rule: dict, identity: dict, employment: dict) -> object:
    keywords = [A.norm_label(k) for k in (rule.get("match") or []) + (rule.get("exact") or [])]
    answer = rule.get("answer")
    candidates = answer if isinstance(answer, list) else [answer]
    if any(A._MONEY_DOMAIN.search(k) for k in keywords):
        extras = [c for c in candidates if not re.fullmatch(r"\$?[\d,]+", str(c))]
        return {"computed": "salary", "fallback": extras} if extras else {"computed": "salary"}
    if any(k.startswith(("how did you hear", "where did you hear")) for k in keywords):
        return {"computed": "how_heard", "fallback": candidates}
    if any(k in ("start date", "when can you start", "earliest") for k in keywords):
        return {"computed": "start_date"}
    if len(candidates) == 1:
        for block_name, block in (("identity", identity), ("employment", employment or {})):
            for key, value in block.items():
                if isinstance(value, str) and value == candidates[0] and key != "country":
                    return {"fact": f"{block_name}.{key}"}
    return answer


def convert(old: dict, records: list[dict], start_in_days: int | None) -> dict:
    identity = old.get("identity") or {}
    employment = old.get("employment") or {}
    wa = old.get("work_authorization") or {}

    home = A.canonical_place(identity.get("country", "")) or "United States"
    home_row = dict(STATUS_FACTS[wa.get("status", "needs_sponsorship_now")])
    if wa.get("status") == "time_limited" and wa.get("scope_qualified_answer") == "yes":
        home_row["unrestricted"] = True
    if wa.get("status_label"):
        home_row["status_label"] = wa["status_label"]
    if wa.get("sponsorship_followup_text"):
        home_row["followup"] = wa["sponsorship_followup_text"]
    if start_in_days is not None:
        home_row["start_in_days"] = start_in_days
    countries = {"default": {"authorized": False, "requires_sponsorship": True}, home: home_row}
    nationalities = [n for n in (wa.get("nationality"), wa.get("second_nationality")) if n]
    for n in nationalities:
        place = A.canonical_place(n)
        if place not in countries:
            countries[place] = {"authorized": True, "requires_sponsorship": False,
                                "unrestricted": True, "status_label": "Citizen"}
    new = {
        "schema_version": A.SCHEMA_VERSION,
        "browser": old.get("browser"),
        "identity": identity,
        "education": old.get("education"),
        "employment": old.get("employment"),
        "demographics": old.get("demographics"),
        "work_authorization": {
            "us_person": wa.get("us_person_answer", "park"),
            "nationalities": nationalities,
            "countries": countries,
        },
        "claims": {c: {"holds": True, "country": home}
                   for c in wa.get("status_option_candidates") or []
                   if A._norm_option(c) not in A._BARE_OPTIONS},
        "questions": {},
    }
    new = {k: v for k, v in new.items() if v is not None}

    questions: dict[str, dict] = {}
    by_group: dict[str, str] = {}
    taken: set[str] = set()
    for rule in old.get("rules") or []:
        first = (rule.get("match") or rule.get("exact") or ["question"])[0]
        qid = _unique(slug(first), taken)
        spec: dict = {"answer": _answer_spec(rule, identity, employment)}
        if rule.get("exact"):
            spec["wordings"] = list(rule["exact"])
        if rule.get("match"):
            spec["keywords"] = list(rule["match"])
        if rule.get("mode"):
            spec["mode"] = rule["mode"]
        if rule.get("only_when_required"):
            spec["only_when_required"] = True
        questions[qid] = spec
        for key in (rule.get("match") or []) + (rule.get("exact") or []):
            by_group[A.norm_label(key)] = qid
        by_group[str(first).strip().casefold()] = qid

    def owner(group: str) -> str | None:
        return by_group.get(group.strip().casefold()) or by_group.get(A.norm_label(group))

    def work_auth(text: str) -> bool:
        return bool(A.WORK_AUTHORIZATION_DOMAIN.search(text or "")
                    and not A._NOT_WORK_AUTH.search(text or ""))

    dropped = [r for r in records if work_auth(r.get("wording", ""))]
    records = [r for r in records if not work_auth(r.get("wording", ""))]

    for rec in records:
        if rec.get("kind") != "answer":
            continue
        wording = rec.get("wording", "")
        qid = _unique(slug(wording), taken)
        answer = rec.get("answer") or []
        spec = {"answer": answer[0] if len(answer) == 1 else answer, "keywords": [wording]}
        if rec.get("mode"):
            spec["mode"] = rec["mode"]
        questions[qid] = spec
        by_group[wording.strip().casefold()] = qid
        by_group[A.norm_label(wording)] = qid
        if rec.get("group"):
            by_group.setdefault(rec["group"].strip().casefold(), qid)

    for rec in records:
        kind = rec.get("kind")
        if kind == "answer":
            continue
        qid = owner(rec.get("group", ""))
        if qid is None:
            dropped.append(rec)
            continue
        spec = questions[qid]
        if kind == "wording":
            spec.setdefault("keywords", [])
            if rec["wording"] not in spec["keywords"]:
                spec["keywords"].append(rec["wording"])
        elif kind == "veto":
            spec.setdefault("not", []).append(rec["wording"])
        elif kind == "option":
            answer = spec.get("answer")
            if isinstance(answer, dict):
                fallback = answer.setdefault("fallback", [])
                if rec["option"] not in fallback:
                    fallback.append(rec["option"])
            else:
                candidates = answer if isinstance(answer, list) else [answer]
                if rec["option"] not in candidates:
                    spec["answer"] = [*candidates, rec["option"]]
    new["questions"] = questions
    return new, dropped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--answers", type=Path, default=paths.PROFILE / "application_answers.yaml")
    ap.add_argument("--learned", type=Path, default=paths.PROFILE / ".apply_learned.jsonl")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--start-in-days", type=int)
    args = ap.parse_args()

    old = yaml.safe_load(args.answers.read_text(encoding="utf-8"))
    if old.get("schema_version") == A.SCHEMA_VERSION:
        print(f"{args.answers} is already schema {A.SCHEMA_VERSION}")
        return 0
    records = []
    if args.learned.is_file():
        for line in args.learned.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    new, dropped = convert(old, records, args.start_in_days)
    out = args.out or args.answers
    A.answers_from(new, where="migrated bank")  # refuses to write an invalid bank
    if out == args.answers:
        backup = args.answers.with_suffix(".v1.yaml")
        backup.write_text(args.answers.read_text(encoding="utf-8"), encoding="utf-8")
    out.write_text(yaml.safe_dump(new, sort_keys=False, allow_unicode=True, width=100),
                   encoding="utf-8")
    print(f"wrote {out}: {len(new['questions'])} questions, {len(new['claims'])} claims, "
          f"{len(dropped)} learned record(s) dropped (work authorization, or no owner)")
    for rec in dropped:
        print(f"  dropped: {rec}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
