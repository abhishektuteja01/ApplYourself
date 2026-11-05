"""`synonyms` — the deterministic side of /suggest-synonyms.

The session proposes; this code checks and writes. Nothing here calls an LLM.

  synonyms keywords         top JD keywords of the roles with no resume yet (JSON)
  synonyms apply [--file F] add the proposals' synonyms, record what was declined,
                            queue the unlock questions
  synonyms undo T PHRASE    take one added synonym back out
  synonyms approve K ...    a declined keyword becomes a permanent "do not add"
  synonyms answer ID yes|no close an unlock question

A proposals file (`jobs/synonyms_proposals.yaml`):

  ready:    [{target: B-DEL-01 | SKILL-SQL, phrase: "...", keyword: "..."}]
  declined: [{keyword: "...", reason: "..."}]
  unlock:   [{kind: bullet | skill, thing: "...", context: "...", via: "..."}]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import yaml

from src.shared import paths
from src.shared.config_keys import load_automation
from src.resume_model import parse_entries

BULLETS = paths.PROFILE / "bullets.md"
SKILLS = paths.PROFILE / "skills_master.md"
AUTOMATION = paths.PROFILE / "automation.yaml"
DECLINED = paths.PROFILE / "synonyms_declined.yaml"
HISTORY = paths.PROFILE / ".synonym_history"
PROPOSALS = paths.JOBS / "synonyms_proposals.yaml"
LOG = paths.JOBS / "synonyms_log.jsonl"
OPEN = paths.JOBS / "synonyms_open.yaml"
SCORED = paths.SCORED
CLEAN = paths.CLEAN

HISTORY_KEEP = 20
MAX_WORDS = 8
TOP_KEYWORDS = 40


class SynonymsError(ValueError):
    """A problem the user can act on; printed as one ERROR line."""


def _config() -> dict:
    try:
        data = load_automation(AUTOMATION)
    except ValueError as exc:
        raise SynonymsError(str(exc)) from exc
    for key in ("auto_add_synonyms", "unlock_max_open"):
        if key not in data:
            raise SynonymsError(f"profile/automation.yaml has no `{key}`")
    return data


def _norm(text: str) -> str:
    return " ".join(str(text).casefold().split())


def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_job_ids() -> list[str]:
    from src.apply import sheet

    return sheet.job_ids("New")


def _read_yaml(path: Path, default):
    if not path.is_file():
        return default
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return default if data is None else data


def _write_yaml(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- keywords

def keywords() -> dict:
    """The top keywords of the roles with no resume yet, minus every keyword
    already declined."""
    ids = _new_job_ids()
    skip = {_norm(d["keyword"]) for d in _read_yaml(DECLINED, [])}
    if not ids or not SCORED.exists():
        return {"job_ids": ids, "keywords": []}
    scored = pd.read_parquet(SCORED)
    scored = scored[scored["job_id"].isin(ids)]
    company = {}
    if CLEAN.exists():
        clean = pd.read_parquet(CLEAN, columns=["job_id", "company"])
        company = dict(zip(clean["job_id"], clean["company"]))
    seen: dict[str, dict] = {}
    freq: Counter = Counter()
    for row in scored.sort_values("fit_score", ascending=False).itertuples():
        raw = row.keywords_to_mirror
        items = list(raw) if hasattr(raw, "__iter__") and not isinstance(raw, str) else [raw]
        for kw in items:
            kw = str(kw or "").strip()
            if not kw or _norm(kw) in skip:
                continue
            freq[_norm(kw)] += 1
            seen.setdefault(_norm(kw), {"keyword": kw, "job_id": row.job_id,
                                        "company": company.get(row.job_id, ""),
                                        "fit": float(row.fit_score)})
    top = [{**seen[k], "frequency": n} for k, n in freq.most_common(TOP_KEYWORDS)]
    return {"job_ids": ids, "keywords": top}


# ------------------------------------------------------------------- apply

def _block(lines: list[str], entry: str) -> tuple[int, int]:
    """[start, end) of an entry's lines, header included."""
    start = next((i for i, ln in enumerate(lines) if ln.strip() == f"## {entry}"), -1)
    if start < 0:
        raise SynonymsError(f"{entry} is not in the file")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
               len(lines))
    return start, end


def _add_phrases(text: str, entry: str, phrases: list[str]) -> str:
    lines = text.split("\n")
    start, end = _block(lines, entry)
    for i in range(start + 1, end):
        if lines[i].startswith("allowable_synonyms:"):
            current = yaml.safe_load(lines[i].partition(":")[2].strip()) or []
            lines[i] = "allowable_synonyms: " + json.dumps(
                [*current, *phrases], ensure_ascii=False)
            return "\n".join(lines)
    last = max(i for i in range(start, end) if lines[i].strip())
    lines.insert(last + 1, "allowable_synonyms: " + json.dumps(phrases, ensure_ascii=False))
    return "\n".join(lines)


def _remove_phrase(text: str, entry: str, phrase: str) -> tuple[str, bool]:
    lines = text.split("\n")
    start, end = _block(lines, entry)
    for i in range(start + 1, end):
        if lines[i].startswith("allowable_synonyms:"):
            current = yaml.safe_load(lines[i].partition(":")[2].strip()) or []
            kept = [p for p in current if _norm(p) != _norm(phrase)]
            if len(kept) == len(current):
                return text, False
            lines[i] = "allowable_synonyms: " + json.dumps(kept, ensure_ascii=False)
            return "\n".join(lines), True
    return text, False


def _backup(path: Path) -> None:
    HISTORY.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    shutil.copy2(path, HISTORY / f"{path.name}.{stamp}")
    old = sorted(HISTORY.glob(f"{path.name}.*"))
    for f in old[:-HISTORY_KEEP]:
        f.unlink()


def _save(path: Path, text: str, entries_after: dict) -> None:
    """Write `text` only if it still parses to the entries we expect."""
    if parse_entries(text) != entries_after:
        raise SynonymsError(f"{path.name}: the edit did not round-trip; nothing written")
    _backup(path)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _screen(item: dict, sources: dict[str, dict], batch: set) -> str:
    """"" when the proposal may be added, else why not."""
    target = str(item.get("target") or "")
    phrase = " ".join(str(item.get("phrase") or "").split())
    file_key = "bullets" if target.startswith("B-") else "skills"
    entry = sources[file_key].get(target)
    if entry is None:
        return f"{target or '(no target)'} does not exist"
    if not phrase:
        return "empty phrase"
    if len(phrase.split()) > MAX_WORDS:
        return f"more than {MAX_WORDS} words"
    own = [entry.get("canonical") if file_key == "bullets" else entry.get("name"),
           *(entry.get("allowable_synonyms") or [])]
    if _norm(phrase) in {_norm(o) for o in own if o}:
        return "already there"
    if (target, _norm(phrase)) in batch:
        return "proposed twice"
    return ""


def apply(proposals_path: Path | None = None) -> list[str]:
    cfg = _config()
    path = proposals_path or PROPOSALS
    if not path.is_file():
        raise SynonymsError(f"{path.name} not found")
    data = _read_yaml(path, {}) or {}
    out: list[str] = []
    today = _today()

    sources = {"bullets": parse_entries(BULLETS.read_text(encoding="utf-8")),
               "skills": parse_entries(SKILLS.read_text(encoding="utf-8"))}
    added: list[dict] = []
    if cfg["auto_add_synonyms"]:
        batch: set = set()
        per_target: dict[str, list[str]] = {}
        for item in data.get("ready") or []:
            why = _screen(item, sources, batch)
            if why:
                out.append(f"skipped {item.get('target')} {item.get('phrase')!r}: {why}")
                continue
            phrase = " ".join(str(item["phrase"]).split())
            batch.add((item["target"], _norm(phrase)))
            per_target.setdefault(item["target"], []).append(phrase)
            added.append({"date": today, "at": _now(), "target": item["target"], "phrase": phrase,
                          "keyword": str(item.get("keyword") or "")})
        for file_key, file_path in (("bullets", BULLETS), ("skills", SKILLS)):
            text = file_path.read_text(encoding="utf-8")
            expect = {k: dict(v) for k, v in sources[file_key].items()}
            touched = False
            for target, phrases in per_target.items():
                if target not in expect:
                    continue
                text = _add_phrases(text, target, phrases)
                expect[target]["allowable_synonyms"] = [
                    *(expect[target].get("allowable_synonyms") or []), *phrases]
                touched = True
            if touched:
                _save(file_path, text, expect)
        if added:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            with LOG.open("a", encoding="utf-8") as f:
                for row in added:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
    else:
        out.append("auto_add_synonyms is off: nothing added")

    declined = _read_yaml(DECLINED, [])
    known = {_norm(d["keyword"]) for d in declined}
    new_declined = 0
    for item in data.get("declined") or []:
        kw = str(item.get("keyword") or "").strip()
        if kw and _norm(kw) not in known:
            known.add(_norm(kw))
            declined.append({"keyword": kw, "reason": str(item.get("reason") or ""),
                             "first_seen": today, "at": _now(), "approved": False})
            new_declined += 1
    if new_declined:
        _write_yaml(DECLINED, declined)

    state = _read_yaml(OPEN, {}) or {}
    open_items, closed = state.get("open") or [], state.get("closed") or []
    asked = {(i["kind"], _norm(i["thing"])) for i in [*open_items, *closed]}
    queued = 0
    for item in data.get("unlock") or []:
        key = (str(item.get("kind") or ""), _norm(item.get("thing") or ""))
        if item.get("kind") not in ("bullet", "skill") or not key[1] or key in asked:
            continue
        if len(open_items) >= int(cfg["unlock_max_open"]):
            out.append("unlock queue full: the rest wait for the next night")
            break
        asked.add(key)
        open_items.append({"id": f"U{today.replace('-', '')}-{len(open_items) + len(closed) + 1}",
                           "kind": item["kind"], "thing": str(item["thing"]),
                           "context": str(item.get("context") or ""),
                           "via": str(item.get("via") or ""), "asked": today})
        queued += 1
    if queued:
        _write_yaml(OPEN, {"open": open_items, "closed": closed})

    path.unlink()
    out.insert(0, f"added={len(added)} declined={new_declined} unlock={queued}")
    return out


# -------------------------------------------------------- undo, approve, answer

def undo(target: str, phrase: str) -> str:
    file_path = BULLETS if target.startswith("B-") else SKILLS
    text = file_path.read_text(encoding="utf-8")
    new, changed = _remove_phrase(text, target, phrase)
    if not changed:
        return f"{target} has no {phrase!r}"
    expect = parse_entries(text)
    expect[target]["allowable_synonyms"] = [
        p for p in expect[target]["allowable_synonyms"] if _norm(p) != _norm(phrase)]
    _save(file_path, new, expect)
    return f"removed {phrase!r} from {target}"


def approve(words: list[str]) -> list[str]:
    declined = _read_yaml(DECLINED, [])
    out = []
    for w in words:
        hit = [d for d in declined if _norm(d["keyword"]) == _norm(w)]
        for d in hit:
            d["approved"] = True
        out.append(f"approved {w!r}" if hit else f"no declined keyword {w!r}")
    _write_yaml(DECLINED, declined)
    return out


def answer(item_id: str, reply: str) -> str:
    state = _read_yaml(OPEN, {}) or {}
    open_items, closed = state.get("open") or [], state.get("closed") or []
    hit = next((i for i in open_items if i["id"] == item_id), None)
    if hit is None:
        return f"no open question {item_id}"
    open_items.remove(hit)
    closed.append({**hit, "answer": reply, "answered": _today()})
    _write_yaml(OPEN, {"open": open_items, "closed": closed})
    return f"{item_id}: {reply}"


# --------------------------------------------------------------------- cli

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="synonyms", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("keywords")
    a = sub.add_parser("apply")
    a.add_argument("--file", type=Path, default=None)
    u = sub.add_parser("undo")
    u.add_argument("target")
    u.add_argument("phrase")
    p = sub.add_parser("approve")
    p.add_argument("keywords", nargs="+")
    an = sub.add_parser("answer")
    an.add_argument("id")
    an.add_argument("reply", choices=("yes", "no"))
    args = ap.parse_args(argv)
    try:
        if args.cmd == "keywords":
            print(json.dumps(keywords(), indent=2, ensure_ascii=False))
        elif args.cmd == "apply":
            print("\n".join(apply(args.file)))
        elif args.cmd == "undo":
            print(undo(args.target, args.phrase))
        elif args.cmd == "approve":
            print("\n".join(approve(args.keywords)))
        else:
            print(answer(args.id, args.reply))
    except (SynonymsError, OSError, yaml.YAMLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
