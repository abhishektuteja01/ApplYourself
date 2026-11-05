"""Which job_id a new posting gets: the same role seen elsewhere keeps its id,
a close call is only recorded as a maybe duplicate.

Also the `discover-dupes` command, which runs the same matcher over ids
already stored.

Deterministic and LLM-free (R7). Reads pipeline/*/state.yaml, never writes it
(R10).
"""
from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz
from rapidfuzz.utils import default_process

from src.discovery import parse
from src.score_filter import load_trim
from src.shared import identity, jd_trim, paths
from src.state_io import load_state_index

TITLE_MIN = 90
# JD scores, word order ignored, boilerplate trimmed: at JD_MERGE two related
# names merge; at JD_MAYBE (related) or JD_MERGE (unrelated) they are only
# recorded as a maybe duplicate.
JD_MERGE = 90
JD_MAYBE = 80
# Shortest name that may match a longer one by its leading words ("ifs").
WORD_PREFIX_MIN = 3
# A one-word slug name ("frostbank") matches a name it starts with when that
# name has at least GLUED_MIN chars and the slug adds at least GLUED_REST.
GLUED_MIN = 4
GLUED_REST = 3
_SQUASH_RE = re.compile(r"[^a-z0-9]")
_SLUG_DIGITS_RE = re.compile(r"(?<=[a-z]{3})\d+$")
_GLUED_THE_RE = re.compile(r"^the(?=[a-z]{4})")
# Two titles that disagree on these tokens are different roles, however close
# the rest of the title and the JD are.
LEVEL_TOKENS = frozenset({
    "i", "ii", "iii", "iv", "intern", "coop", "trainee", "apprentice",
    "junior", "entry", "graduate", "associate", "senior", "staff", "principal",
    "distinguished", "fellow", "lead", "manager", "director", "head", "vp",
    "chief", "president",
})
_LEVEL_SYNONYMS = {
    "jr": "junior", "sr": "senior", "mgr": "manager", "mgmt": "manager",
    "management": "manager", "interns": "intern", "internship": "intern",
    "internships": "intern", "grad": "graduate", "apprenticeship": "apprentice",
    "1": "i", "2": "ii", "3": "iii", "4": "iv", "vice": "vp",
}
_CO_OP_RE = re.compile(r"\bco op\b")


def level_tokens(title_normalized: str) -> frozenset[str]:
    tokens = (_LEVEL_SYNONYMS.get(t, t)
              for t in _CO_OP_RE.sub("coop", title_normalized or "").split())
    return frozenset(LEVEL_TOKENS.intersection(tokens))


# Title words that say where or how, not what: two titles differing only in
# these (or in the rows' own location words) still name one role.
_TITLE_NOISE = frozenset({
    "remote", "hybrid", "onsite", "wfh", "anywhere", "and", "of", "the", "for", "to",
    "a", "an", "with", "in", "at",
})
_WORD_RE = re.compile(r"[a-z0-9]+")


def _title_words(title_normalized: str) -> set[str]:
    return {_LEVEL_SYNONYMS.get(t, t) for t in _CO_OP_RE.sub("coop", title_normalized or "").split()}


def titles_differ(new: dict, old: dict) -> bool:
    """Do the titles disagree on a word that names the role ("Applied AI
    Engineer, Codex" / "..., Enterprise")? Workplace words, one-letter
    tokens and either row's location words do not count."""
    where = set(_WORD_RE.findall(f"{new.get('location_raw', '')} {old.get('location_raw', '')}".lower()))
    diff = _title_words(new["title_normalized"]) ^ _title_words(old["title_normalized"])
    return any(len(t) > 1 and t not in _TITLE_NOISE and t not in where for t in diff)


def squash(company_normalized: str) -> str:
    return _SQUASH_RE.sub("", (company_normalized or "").lower())


def _match_words(company_normalized: str) -> list[str]:
    """The name as compared, never as hashed: a slug's trailing digits
    ("ifs1") and a glued leading "the" ("thehartford") dropped."""
    words = (company_normalized or "").lower().split()
    if words:
        words[-1] = _SLUG_DIGITS_RE.sub("", words[-1])
    if len(words) == 1:
        words[0] = _GLUED_THE_RE.sub("", words[0])
    return [w for w in words if w]


def same_company(a: str, b: str) -> bool:
    """One employer under two spellings: equal once squashed ("red bull" /
    "redbull"), one name the other's leading words ("vouch" / "vouch
    insurance"), or a one-word slug that starts with the other name
    ("frost" / "frostbank"). Near-spellings ("apex" / "apexa") never match."""
    wa, wb = _match_words(a), _match_words(b)
    if not wa or not wb:
        return False
    sa, sb = "".join(wa), "".join(wb)
    if sa == sb:
        return True
    short, long = sorted((wa, wb), key=len)
    if len(short) < len(long) and long[:len(short)] == short and len("".join(short)) >= WORD_PREFIX_MIN:
        return True
    (s, ws), (l, wl) = sorted(((sa, wa), (sb, wb)), key=lambda x: len(x[0]))
    return ((len(ws) == 1 or len(wl) == 1) and len(s) >= GLUED_MIN
            and l.startswith(s) and len(l) - len(s) >= GLUED_REST)


Trim = tuple[tuple[str, ...], tuple[str, ...]]
NO_TRIM: Trim = ((), ())


def jd_key(jd_text: str, trim: Trim = NO_TRIM) -> str:
    """The JD as compared: boilerplate trimmed, then its words lowercased and
    sorted, so two copies that order their sections differently still match."""
    text = jd_text or ""
    if trim[0]:
        trimmed = jd_trim.trim_boilerplate(text, *trim)
        if len(trimmed) >= parse.MIN_JD_CHARS:
            text = trimmed
    return " ".join(sorted(default_process(text).split()))


def _jd_of(rec: dict, trim: Trim) -> str:
    if "_jd_key" not in rec:
        rec["_jd_key"] = jd_key(rec.get("jd_text", ""), trim)
    return rec["_jd_key"]


# Why a pair is only a maybe duplicate.
MAYBE_REASONS = ("other_name", "weak_jd", "other_title")


def same_job_elsewhere(new: dict, old: dict, trim: Trim = NO_TRIM) -> tuple[str, float]:
    """(verdict, JD score): is `old` the same role as `new`, found by another
    board? The verdict is "merge", a MAYBE_REASONS entry (recorded, never
    merged) or "". Never two postings of one board, which lists each of its
    jobs once."""
    if new["board"] and new["board"] == old["board"]:
        return "", 0.0
    if fuzz.WRatio(new["title_normalized"], old["title_normalized"]) < TITLE_MIN:
        return "", 0.0
    if level_tokens(new["title_normalized"]) != level_tokens(old["title_normalized"]):
        return "", 0.0
    named = same_company(new["company_normalized"], old["company_normalized"])
    ja, jb = _jd_of(new, trim), _jd_of(old, trim)
    if not (ja and jb):
        return "", 0.0
    score = fuzz.ratio(ja, jb, score_cutoff=JD_MAYBE if named else JD_MERGE)
    if not score:
        return "", 0.0
    if not named:
        return "other_name", score
    if score < JD_MERGE:
        return "weak_jd", score
    return ("other_title" if titles_differ(new, old) else "merge"), score


class _Index:
    """Lookups over the store for id assignment, kept current as rows are
    added so two new postings in one night agree with each other."""

    def __init__(self, store: pd.DataFrame, tracked: frozenset[str], trim: Trim = NO_TRIM):
        self.tracked, self.trim = tracked, trim
        self.by_key: dict[str, str] = {}
        self.by_pair: dict[str, list[str]] = {}
        self.pair_of_id: dict[str, set[str]] = {}
        # Each id's own title (the pair it hashes from), when the store holds it.
        self.own: dict[str, dict] = {}
        self.by_squash: dict[str, list[dict]] = {}
        self.by_title: dict[str, list[dict]] = {}
        # Tracked first, then oldest, then smallest id: which id a pair keeps
        # when the store holds more than one.
        order = store.assign(_t=~store["job_id"].isin(tracked)).sort_values(
            ["_t", "first_seen", "job_id"], kind="stable")
        for rec in order.to_dict("records"):
            self.add(rec)

    def add(self, rec: dict) -> None:
        jid = rec["job_id"]
        self.by_key.setdefault(rec["posting_key"], jid)
        url_key = identity.posting_key(rec.get("url", ""))
        if url_key:
            self.by_key.setdefault(url_key, jid)
        pair = f"{rec['company_normalized']}|{rec['title_normalized']}"
        ids = self.by_pair.setdefault(pair, [])
        if jid not in ids:
            ids.append(jid)
        self.pair_of_id.setdefault(jid, set()).add(pair)
        if jid not in self.own and identity.compute_job_id(
                rec["company_normalized"], rec["title_normalized"]) == jid:
            self.own[jid] = {"title_normalized": rec["title_normalized"],
                             "location_raw": rec.get("location_raw", "")}
        cand = {k: rec.get(k, "") for k in (
            "job_id", "source", "board", "company_normalized", "title_normalized", "jd_text",
            "location_raw")}
        if "_jd_key" in rec:
            cand["_jd_key"] = rec["_jd_key"]
        s = squash(rec["company_normalized"])
        if s:
            self.by_squash.setdefault(s, []).append(cand)
        if rec["title_normalized"]:
            self.by_title.setdefault(rec["title_normalized"], []).append(cand)

    def candidates(self, row: dict) -> list[dict]:
        s = squash(row["company_normalized"])
        seen, out = set(), []
        pools = [self.by_squash.get(s, []), self.by_title.get(row["title_normalized"], [])]
        if len(s) >= 4:
            pools += [v for k, v in self.by_squash.items()
                      if k != s and len(k) >= 4 and (k.startswith(s) or s.startswith(k))]
        for pool in pools:
            for cand in pool:
                marker = id(cand)
                if marker not in seen:
                    seen.add(marker)
                    out.append(cand)
        return out

    def belongs(self, row: dict, jid: str) -> bool:
        """Does `row`'s title agree with `jid`'s own title? An id merged
        before the store can hold rows of other roles; those never pull a new
        posting in. An id without its own pair in the store is not checked."""
        own = self.own.get(jid)
        return own is None or not titles_differ(row, own)

    def assign(self, row: dict) -> tuple[str, str, dict[str, dict]]:
        """(job_id, rule, maybes) for a posting the store has not seen.
        `maybes` maps each other job_id that looks like the same role, but
        not surely, to its best candidate and JD score."""
        for key in (row["posting_key"], identity.posting_key(row["url"])):
            if key and key in self.by_key:
                return self.by_key[key], "url", {}
        pair = f"{row['company_normalized']}|{row['title_normalized']}"
        held = self.by_pair.get(pair, [])
        for jid in held:
            if self.belongs(row, jid):
                return jid, "pair", {}
        # Ids that hold this pair but name another role: a maybe, never a join.
        maybes = {jid: {"cand": {"job_id": jid, "company_normalized": row["company_normalized"]},
                        "score": 0.0, "reason": "other_title"} for jid in held}
        matches = set()
        for c in self.candidates(row):
            verdict, score = same_job_elsewhere(row, c, self.trim)
            if verdict == "merge" and not self.belongs(row, c["job_id"]):
                verdict = "other_title"
            if verdict == "merge":
                matches.add(c["job_id"])
            elif verdict and score > maybes.get(c["job_id"], {}).get("score", 0):
                maybes[c["job_id"]] = {"cand": c, "score": score, "reason": verdict}
        if matches:
            tracked = sorted(m for m in matches if m in self.tracked)
            jid = (tracked or sorted(matches))[0]
            return jid, "fuzzy", {k: v for k, v in maybes.items() if k not in matches}
        jid = identity.compute_job_id(row["company_normalized"], row["title_normalized"])
        if jid in self.pair_of_id and pair not in self.pair_of_id[jid]:
            return "", "collision", {}
        return jid, "hash", {k: v for k, v in maybes.items() if k != jid}


# ---------------------------------------------------------------------
# discover-dupes: the same matcher over ids already stored
# ---------------------------------------------------------------------

def _rank(r: dict) -> tuple[bool, float]:
    """A merge verdict outranks any maybe; then the higher JD score."""
    return r["reason"] == "backfill_merge", r["jd_score"]


def find_pairs(store: pd.DataFrame, trim: Trim = NO_TRIM,
               date: str = "") -> list[dict]:
    """One record per pair of stored job_ids the matcher calls the same role
    ("backfill_merge") or a maybe ("backfill_" + a MAYBE_REASONS entry), best
    JD score kept."""
    index = _Index(store, frozenset(), trim)
    best: dict[str, dict] = {}
    for row in store.to_dict("records"):
        done: set[str] = set()
        for c in index.candidates(row):
            other = c["job_id"]
            if other == row["job_id"] or other in done:
                continue
            verdict, score = same_job_elsewhere(row, c, trim)
            if not verdict:
                continue
            if verdict == "merge":
                done.add(other)
            reason = f"backfill_{verdict}"
            key = "|".join(sorted((row["job_id"], other)))
            if key in best and _rank(best[key]) >= (verdict == "merge", round(score, 1)):
                continue
            best[key] = {
                "pair": key, "job_id": row["job_id"], "other": other,
                "company": row["company_normalized"], "other_company": c["company_normalized"],
                "title": row["title_normalized"], "jd_score": round(score, 1),
                "reason": reason, "date": date}
    return sorted(best.values(), key=lambda r: (r["reason"], -r["jd_score"], r["pair"]))


def _line(r: dict, states: dict) -> str:
    def side(jid, company):
        st = states.get(jid, {}).get("state")
        return f"{jid} {company}" + (f" [{st}]" if st else "")
    return (f"{r['reason']:<20} {r['jd_score']:>5g}  {side(r['job_id'], r['company'])}"
            f"  /  {side(r['other'], r['other_company'])}  — {r['title']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="discover-dupes",
        description="List stored job_ids that look like one role and record them in "
                    "jobs/maybe_duplicates.jsonl. Never changes an id.")
    parser.add_argument("--dry-run", action="store_true", help="print only; write nothing")
    parser.add_argument("--jobs-dir", type=Path, default=paths.JOBS)
    args = parser.parse_args(argv)

    from src.discovery.store import append_maybe_duplicates, load_store

    store = load_store(args.jobs_dir / paths.STORE.name)
    states = load_state_index(paths.PIPELINE)
    rows = find_pairs(store, load_trim(), str(pd.Timestamp.today().date()))
    print("pairs by reason:", dict(Counter(r["reason"] for r in rows)))
    both = [r for r in rows if r["job_id"] in states and r["other"] in states]
    one = [r for r in rows if (r["job_id"] in states) != (r["other"] in states)]
    for title, group in (("both tracked", both), ("one tracked", one)):
        print(f"\n{title}: {len(group)}")
        for r in group:
            print(" ", _line(r, states))
    if not args.dry_run:
        new = append_maybe_duplicates(rows, args.jobs_dir / paths.MAYBE_DUPES.name)
        print(f"\nrecorded {len(new)} new pairs in {args.jobs_dir / paths.MAYBE_DUPES.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
