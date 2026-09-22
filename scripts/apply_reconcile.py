#!/usr/bin/env python
"""Apply a slug-reconcile proposal to the universe CSVs. The only writer of `active`.

`scripts/reconcile_slugs.py` proposes; this disposes. It reads one
`jobs/slug_reconcile_<date>.csv` and, per verdict, retires the old row
(`active=0`, in place, name kept) and/or adds the confirmed replacement to
`data/universe/<ats>.local.csv`. Nothing is ever deleted and nothing is ever
created in the tracked list -- `data/universe/README.md` reserves that for
self-verified slugs.

Authorization is re-derived from `scripts/slug_resolvers.py`, never read out of
the CSV: a row may retire only if `is_retireable()` says its verdict may, and
only if the row itself still carries a confirmed 404, `confidence=high`, and
(for a move) a non-empty destination slug. The reconcile pass already enforces
all four. Enforcing them again here is the point of the script: a hand-edited
or stale proposal must not be able to retire a live company.

`--verdict` defaults to nothing, so start with the preview and then name the
verdicts you actually reviewed:

    uv run python scripts/apply_reconcile.py --proposal jobs/slug_reconcile_2026-09-22.csv
    uv run python scripts/apply_reconcile.py --proposal jobs/... --verdict moved --dry-run
    uv run python scripts/apply_reconcile.py --proposal jobs/... --verdict moved --verdict dual
"""
from __future__ import annotations

import argparse
import bisect
import csv
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from src import paths

sys.path.insert(0, str(Path(__file__).resolve().parent))
import slug_resolvers as R  # noqa: E402

UNIVERSE_DIR = paths.REPO_ROOT / "data" / "universe"
OUT_DIR = paths.REPO_ROOT / "jobs"
UNIVERSE_COLUMNS = ["name", "slug", "active"]
LOG_COLUMNS = ["action", "ats", "slug", "name", "verdict", "file", "detail"]
COUNTERS = ("rows", "retired", "added", "refused", "already_retired", "not_in_universe")

# Added by the reconcile pass concurrently with this script; fall back to the
# wire name so a proposal carrying it is understood either way.
MOVED_OFFSITE = getattr(R, "MOVED_OFFSITE", "moved_offsite")

RETIRED = "0"
LIVE = "1"

ACT_RETIRE = "retire"
ACT_ADD = "add"
ACT_REFUSED = "refused"
ACT_ALREADY_RETIRED = "already_retired"
ACT_NOT_IN_UNIVERSE = "not_in_universe"
ACT_ALREADY_PRESENT = "already_present"
ACT_SKIPPED = "skipped"


# Not a resolver verdict. `dead_end` is authorized by the search log, not by
# the proposal: the board 404s, the company may well be alive, and every tier
# -- lever casing, name match across every platform in the dump, the DNS sweep,
# the careers-page scrape, and the web-search pass -- found no replacement.
# A 404 board is a dead end for scraping whether or not the company still
# trades, but only once the search is provably exhausted.
DEAD_END = "dead_end"

SEARCH_EXHAUSTED = "searched_nothing_found"


def known_verdicts() -> tuple[str, ...]:
    """Every verdict this script understands, read at call time so a resolver
    module that grows a verdict needs no edit here."""
    return tuple(dict.fromkeys(tuple(R.VERDICTS) + (MOVED_OFFSITE, DEAD_END)))


def load_search_log(path) -> set[tuple[str, str]]:
    """The `(ats, slug)` pairs a web-search pass proved it could not place.

    Only `outcome == SEARCH_EXHAUSTED` counts. A row recording a *found*
    replacement is deliberately ignored here: that belongs in the proposal as a
    `moved`, and honouring it from this file would let the search log add
    boards, which is not its job.
    """
    exhausted: set[tuple[str, str]] = set()
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("outcome") or "").strip() != SEARCH_EXHAUSTED:
                continue
            ats = (row.get("ats") or "").strip().lower()
            slug = (row.get("slug") or "").strip()
            if ats and slug:
                exhausted.add((ats, slug))
    return exhausted


def retires(verdict: str) -> bool:
    """Whether this verdict retires the OLD row.

    Wider than `is_retireable`, which means "retire the old row *and* add the
    new board". `moved_offsite` retires without adding: the company is on a
    platform we have no client for, so there is no board to add. Read from the
    resolver at call time, with the pre-`moved_offsite` set as the fallback, so
    the two modules cannot drift into disagreeing about what may retire.
    """
    predicate = getattr(R, "retires_old_row", None)
    if predicate is not None:
        return predicate(verdict)
    return R.is_retireable(verdict)


def adds(verdict: str) -> bool:
    return verdict in (R.MOVED, R.DUAL)


@dataclass(frozen=True)
class Change:
    action: str
    ats: str
    slug: str
    name: str
    verdict: str
    path: Path | None
    detail: str

    def log_row(self) -> list[str]:
        return [self.action, self.ats, self.slug, self.name, self.verdict,
                self.path.name if self.path else "", self.detail]


class UniverseFile:
    """One `name,slug[,active]` CSV, held in memory until written once.

    Row order is preserved verbatim; an addition is inserted at its sorted-by-
    slug position so a sorted file stays sorted. `active` is kept as read, so a
    file written before the column existed keeps its blank cells on every row
    this pass did not touch.
    """

    def __init__(self, path: Path):
        self.path = path
        self.exists = path.exists()
        self.rows: list[list[str]] = []
        self.index: dict[str, int] = {}
        self.dirty = False
        if self.exists:
            self._read()

    def _read(self) -> None:
        with open(self.path, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                name = (row.get("name") or "").strip()
                slug = (row.get("slug") or "").strip()
                if not name or not slug:
                    continue
                self.index[slug] = len(self.rows)
                self.rows.append([name, slug, (row.get("active") or "").strip()])

    def has(self, slug: str) -> bool:
        return slug in self.index

    def active(self, slug: str) -> str:
        return self.rows[self.index[slug]][2]

    def retire(self, slug: str) -> None:
        self.rows[self.index[slug]][2] = RETIRED
        self.dirty = True

    def add(self, name: str, slug: str) -> None:
        at = bisect.bisect_left([r[1] for r in self.rows], slug)
        self.rows.insert(at, [name, slug, LIVE])
        self.index = {r[1]: i for i, r in enumerate(self.rows)}
        self.dirty = True


class Universe:
    """The lanes' CSVs, opened on demand."""

    def __init__(self, directory: Path):
        self.dir = directory
        self._files: dict[Path, UniverseFile] = {}

    def file(self, ats: str, local: bool) -> UniverseFile:
        path = self.dir / (f"{ats}.local.csv" if local else f"{ats}.csv")
        if path not in self._files:
            self._files[path] = UniverseFile(path)
        return self._files[path]

    def holder(self, ats: str, slug: str) -> UniverseFile | None:
        """The file that already holds this slug -- tracked first, then the
        overlay. None means no file lists it, which is never a reason to write
        a tombstone for it."""
        for local in (False, True):
            f = self.file(ats, local)
            if f.has(slug):
                return f
        return None

    def dirty_files(self) -> list[UniverseFile]:
        return [f for f in self._files.values() if f.dirty]


def refusal_reasons(row: dict, verdict: str) -> list[str]:
    """Why this row may not retire its old slug. Empty means it may.

    Re-checked from the row rather than taken on trust from the verdict column.
    """
    reasons = []
    if not retires(verdict):
        reasons.append(f"verdict {verdict!r} is not retireable")
    status = (row.get("old_status") or "").strip()
    if status not in R.NOT_FOUND_STATUSES:
        reasons.append(f"old_status {status!r} is not a confirmed 404")
    confidence = (row.get("confidence") or "").strip()
    if confidence != R.HIGH:
        reasons.append(f"confidence {confidence!r} is not {R.HIGH}")
    if verdict in (R.MOVED, MOVED_OFFSITE) and not (row.get("new_slug") or "").strip():
        reasons.append("new_slug is empty")
    return reasons


def dead_end_refusals(row: dict, ats: str, slug: str, exhausted) -> list[str]:
    """Why this row may not be retired as a dead end. Empty means it may."""
    reasons = []
    status = (row.get("old_status") or "").strip()
    if status not in R.NOT_FOUND_STATUSES:
        # The whole point: a 403 means a firewall blocked us and a 429 means we
        # throttled ourselves. Neither is evidence about the board.
        reasons.append(f"old_status {status!r} is not a confirmed 404")
    if (ats, slug) not in exhausted:
        reasons.append("no search-log row proves the search was exhausted")
    return reasons


def plan_row(row: dict, universe: Universe, selected, exhausted=None) -> list[Change]:
    """The changes one proposal row justifies, with its refusals and skips."""
    ats = (row.get("ats") or "").strip().lower()
    slug = (row.get("slug") or "").strip()
    name = (row.get("name") or "").strip()
    verdict = (row.get("verdict") or "").strip()
    new_ats = (row.get("new_ats") or "").strip().lower() or ats
    new_slug = (row.get("new_slug") or "").strip()

    def change(action, detail, path=None, s=slug, a=ats):
        return Change(action, a, s, name, verdict, path, detail)

    if verdict not in known_verdicts():
        return [change(ACT_REFUSED, f"unknown verdict {verdict!r}")]
    if not ats or not slug:
        if verdict in selected:
            return [change(ACT_REFUSED, "row has no ats or slug")]
        return []

    # The dead-end route is selected as `dead_end`, not by the row's own
    # verdict: the proposal says "the company is alive, we just could not place
    # them", and the search log is what turns that into a retirement.
    dead_end = (DEAD_END in selected and exhausted is not None
                and not retires(verdict) and not adds(verdict))
    if verdict not in selected and not dead_end:
        return []
    if not (retires(verdict) or adds(verdict) or dead_end):
        return []

    out: list[Change] = []

    if dead_end:
        reasons = dead_end_refusals(row, ats, slug, exhausted)
        if reasons:
            return [Change(ACT_REFUSED, ats, slug, name, DEAD_END, None,
                            "; ".join(reasons))]
        holder = universe.holder(ats, slug)
        if holder is None:
            return [Change(ACT_NOT_IN_UNIVERSE, ats, slug, name, DEAD_END, None,
                            "no universe file lists this slug")]
        if holder.active(slug) == RETIRED:
            return [Change(ACT_ALREADY_RETIRED, ats, slug, name, DEAD_END,
                            holder.path, "already active=0")]
        holder.retire(slug)
        return [Change(ACT_RETIRE, ats, slug, name, DEAD_END, holder.path,
                        f"dead end after exhausted search (was {verdict}) -> active=0")]

    if retires(verdict):
        reasons = refusal_reasons(row, verdict)
        if verdict == R.GONE and exhausted is not None:
            # Uniform rule once a search log exists: no 404 board is retired
            # without a row proving the search ran. `gone` already means
            # nothing resolved in DNS, but "we looked everywhere" is a stronger
            # claim than "the deterministic tiers found nothing".
            reasons = reasons + dead_end_refusals(row, ats, slug, exhausted)
        if reasons:
            # One refusal stops the whole row: a move whose retirement is not
            # authorized must not half-apply as a bare addition either.
            return [change(ACT_REFUSED, "; ".join(reasons))]
        holder = universe.holder(ats, slug)
        if holder is None:
            out.append(change(ACT_NOT_IN_UNIVERSE, "no universe file lists this slug"))
        elif holder.active(slug) == RETIRED:
            out.append(change(ACT_ALREADY_RETIRED, "already active=0", holder.path))
        else:
            holder.retire(slug)
            out.append(change(ACT_RETIRE, f"{verdict} -> active=0", holder.path))

    if adds(verdict):
        if not new_slug:
            out.append(change(ACT_REFUSED, "new_slug is empty", s=new_slug, a=new_ats))
        elif new_ats not in R.SUPPORTED_ATS:
            out.append(change(ACT_REFUSED, f"unsupported new_ats {new_ats!r}",
                              s=new_slug, a=new_ats))
        else:
            existing = universe.holder(new_ats, new_slug)
            target = universe.file(new_ats, local=True)
            if existing is not None and existing.active(new_slug) == RETIRED:
                out.append(change(ACT_ALREADY_RETIRED,
                                  "destination slug is tombstoned",
                                  existing.path, s=new_slug, a=new_ats))
            elif existing is not None:
                out.append(change(ACT_ALREADY_PRESENT, "destination slug already listed",
                                  existing.path, s=new_slug, a=new_ats))
            else:
                target.add(name or new_slug, new_slug)
                out.append(change(ACT_ADD, f"{verdict} -> active=1", target.path,
                                  s=new_slug, a=new_ats))
    elif verdict == MOVED_OFFSITE and new_slug:
        out.append(change(ACT_SKIPPED,
                          f"destination {new_ats}/{new_slug} is not a lane we scrape",
                          s=new_slug, a=new_ats))

    return out


def backup(path: Path, stamp: str) -> Path:
    target = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, target)
    return target


def write_atomic(path: Path, rows: list[list[str]]) -> None:
    """Temp file in the same directory, then `os.replace`: a crash mid-write
    cannot leave a truncated universe CSV behind."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(UNIVERSE_COLUMNS)
            w.writerows(rows)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_proposal(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in ("ats", "slug", "verdict", "old_status", "confidence")
                   if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: missing column(s) {', '.join(missing)}")
        return list(reader)


def tally(rows: list[dict], changes: dict[int, list[Change]]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for i, row in enumerate(rows):
        verdict = (row.get("verdict") or "").strip() or "(blank)"
        bucket = counts.setdefault(verdict, dict.fromkeys(COUNTERS, 0))
        bucket["rows"] += 1
        for c in changes.get(i, ()):
            if c.action == ACT_RETIRE:
                bucket["retired"] += 1
            elif c.action == ACT_ADD:
                bucket["added"] += 1
            elif c.action == ACT_REFUSED:
                bucket["refused"] += 1
            elif c.action == ACT_ALREADY_RETIRED:
                bucket["already_retired"] += 1
            elif c.action == ACT_NOT_IN_UNIVERSE:
                bucket["not_in_universe"] += 1
    return counts


def print_summary(counts: dict[str, dict[str, int]]) -> None:
    print(f"{'verdict':16} " + " ".join(f"{c:>16}" for c in COUNTERS))
    for verdict in sorted(counts):
        print(f"{verdict:16} " + " ".join(f"{counts[verdict][c]:>16}" for c in COUNTERS))


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--proposal", required=True,
                   help="proposal CSV from scripts/reconcile_slugs.py")
    p.add_argument("--verdict", action="append", choices=sorted(known_verdicts()),
                   help="repeatable; default none -- a bare run previews every verdict "
                        "and writes nothing")
    p.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    p.add_argument("--no-backup", action="store_true",
                   help="skip the .bak-<timestamp> copy of each file about to change")
    p.add_argument("--universe-dir", help="universe directory (default data/universe)")
    p.add_argument("--log", help="change log CSV (default jobs/apply_reconcile_<date>.csv)")
    p.add_argument("--search-log",
                   help="CSV of `ats,slug,outcome` from the web-search pass. A row "
                        f"with outcome={SEARCH_EXHAUSTED!r} is what authorizes "
                        f"--verdict {DEAD_END}, and, once this file is given, is "
                        "required for a `gone` retirement too")
    return p.parse_args(argv)


def resolve(value: str, default: Path) -> Path:
    if not value:
        return default
    path = Path(value)
    return path if path.is_absolute() else paths.REPO_ROOT / path


def main(argv=None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    proposal = resolve(args.proposal, Path())
    universe_dir = resolve(args.universe_dir, UNIVERSE_DIR)
    if not proposal.exists():
        print(f"error: no such proposal CSV: {proposal}", file=sys.stderr)
        return 1
    if not universe_dir.is_dir():
        print(f"error: no such universe directory: {universe_dir}", file=sys.stderr)
        return 1

    try:
        rows = read_proposal(proposal)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    preview = not args.verdict
    selected = frozenset(args.verdict or known_verdicts())
    exhausted = load_search_log(args.search_log) if args.search_log else None
    if exhausted is not None:
        print(f"search log: {len(exhausted)} slug(s) with the search exhausted")
    dry_run = args.dry_run or preview

    universe = Universe(universe_dir)
    changes: dict[int, list[Change]] = {}
    for i, row in enumerate(rows):
        planned = plan_row(row, universe, selected, exhausted)
        if planned:
            changes[i] = planned

    flat = [c for i in sorted(changes) for c in changes[i]]
    for c in flat:
        print(f"{c.action:16} {c.ats:11} {c.slug:34} {c.verdict:14} "
              f"{c.path.name if c.path else '-':22} {c.detail}")
    if flat:
        print()
    print_summary(tally(rows, changes))

    touched = universe.dirty_files()
    if dry_run:
        note = ("no --verdict given: previewing every verdict, writing nothing"
                if preview else "--dry-run: nothing was written")
        print(f"\n{note}. {len(touched)} file(s) would change.")
    else:
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        for f in touched:
            if not args.no_backup and f.exists:
                print(f"backup: {backup(f.path, stamp).name}")
            write_atomic(f.path, f.rows)
        log = resolve(args.log, OUT_DIR / f"apply_reconcile_{date.today()}.csv")
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(LOG_COLUMNS)
            w.writerows(c.log_row() for c in flat)
        print(f"\nWrote {len(touched)} universe file(s); log at {log}")

    refused = sum(1 for c in flat if c.action == ACT_REFUSED)
    if refused:
        print(f"{refused} refusal(s) -- see the log", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
