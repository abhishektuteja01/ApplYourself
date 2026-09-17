"""Read `jobs/runs/<run_id>.md` back.

The nightly run report has two writers -- `orchestrator.py` (the `## Discovery`
half) and `cleaning.py` (`## Cleaning`) -- and until now no reader. This module
is the reader: it parses the markdown into records and computes the trend
comparisons a digest needs. Rendering and judgment stay in the command session
(R7); nothing here calls an LLM and nothing here writes.

Stdlib only, on purpose: a digest must work on a clone that never installed the
`discovery` dependency group.

Robustness is the contract. A truncated report, a crashed lane, a missing
`## Cleaning` half, a zero-byte file and an unrecognised section all degrade to
partial records plus a note in `parse_notes`; nothing raises.

Two cleaning formats are live. The current writer emits one
`- after dedupe: N (merged M)` line; older reports emit
`- after exact dedupe: N (dropped M)` plus
`- after near dedupe (WRatio>=90): N (dropped M)`. Both normalise to the same
`after_dedupe` / `dropped_dedupe` keys.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path

from src import paths

JOBS_RUNS = paths.JOBS_RUNS

# How many nights the median is taken over, and how far below it a source has
# to fall before the digest calls it out.
DEFAULT_WINDOW = 7
BELOW_MEDIAN_FRACTION = 0.5


class SourceStatus(str, Enum):
    """A lane's outcome for one run.

    SKIPPED is deliberately representable ahead of a writer for it: a cadence
    skip is a lane that was never meant to run tonight, so it is neither a
    crash nor a zero-row alarm, and every consumer below excludes it by name.
    """

    OK = "ok"
    ZERO = "zero"
    CRASHED = "crashed"
    SKIPPED = "skipped"
    NOT_STARTED = "not_started"
    UNKNOWN = "unknown"


@dataclass
class SourceRun:
    name: str
    status: SourceStatus = SourceStatus.UNKNOWN
    rows: int | None = None
    duration_s: float | None = None
    errors: list[str] = field(default_factory=list)
    truncated: bool = False
    # Free text after the status word, e.g. `ZERO rows (inbox empty)` or the
    # cadence reason on a skipped lane.
    detail: str = ""
    crash_traceback: str = ""
    raw_count: int | None = None
    final_count: int | None = None
    # True once a `### Source:` header was seen. A name known only from
    # the cleaning per-source table never ran a lane this report.
    in_discovery: bool = False

    @property
    def is_alarming_zero(self) -> bool:
        """Zero rows and not one error recorded -- the silent-block shape. A
        skipped lane is never this."""
        return self.status is SourceStatus.ZERO and not self.errors


@dataclass
class RunReport:
    run_id: str
    path: str = ""
    wall_time_s: float | None = None
    serial_time_s: float | None = None
    sources: dict[str, SourceRun] = field(default_factory=dict)
    # Normalised cleaning funnel: `raw_rows`, `after_exclusion`, ...,
    # `final_rows`, plus the matching `dropped_*` keys.
    funnel: dict[str, int] = field(default_factory=dict)
    has_discovery: bool = False
    has_cleaning: bool = False
    overrides: str = ""
    # `new job_id` count is NOT in the report -- cleaning computes new ledger
    # ids but never writes them out. None means "not derivable", never zero.
    new_job_ids: int | None = None
    parse_notes: list[str] = field(default_factory=list)

    @property
    def final_rows(self) -> int | None:
        return self.funnel.get("final_rows")

    @property
    def error_count(self) -> int:
        return sum(len(s.errors) for s in self.sources.values())


# --- parsing ---------------------------------------------------------------

_RUN_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}$")
_RUN_HEAD_RE = re.compile(r"^#\s+Run\s+(\S+)")
_SOURCE_RE = re.compile(r"^###\s+Source:\s*(.+?)\s*$")
_TIME_RE = re.compile(r"^Time:\s*([0-9.]+)s")
_ROWS_RE = re.compile(r"^Rows:\s*([0-9]+)")
_ZERO_RE = re.compile(r"^ZERO rows\s*(?:\((.*)\))?")
_SKIPPED_RE = re.compile(r"^SKIPPED\s*(?:\((.*)\))?", re.IGNORECASE)
_WALL_RE = re.compile(r"^Wall time:\s*([0-9.]+)s(?:.*?([0-9.]+)s if summed serially)?")
_NOT_STARTED_RE = re.compile(r"^\*\*DEADLINE REACHED\*\* before (.+?) started")
_FUNNEL_RE = re.compile(
    r"^-\s+(?P<label>[^:]+):\s*(?P<value>-?[0-9]+)"
    r"(?:\s*\((?P<verb>dropped|merged)\s+(?P<delta>-?[0-9]+)\))?\s*$"
)
_PRUNED_RE = re.compile(r"^-\s+pruned\s+([0-9]+)\s+raw files")
_TABLE_ROW_RE = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([0-9]+)\s*\|\s*([0-9]+)\s*\|")

# Funnel label -> (value key, delta key). The two dedupe spellings collapse
# onto one pair; `dropped_dedupe` accumulates so the old format's exact+near
# drops sum to the current format's single merged count.
_FUNNEL_KEYS = {
    "raw rows loaded": ("raw_rows", None),
    "after classification/exclusion": ("after_exclusion", "dropped_exclusion"),
    "after blank-company drop": ("after_blank_company", "dropped_blank_company"),
    "after short-jd drop": ("after_short_jd", "dropped_short"),
    "after stale drop": ("after_stale", "dropped_stale"),
    "after location filter": ("after_location", "dropped_location"),
    "after exact dedupe": ("after_dedupe", "dropped_dedupe"),
    "after near dedupe": ("after_dedupe", "dropped_dedupe"),
    "after dedupe": ("after_dedupe", "dropped_dedupe"),
    "after seen-ledger expiry": ("after_expiry", "dropped_expired"),
    "final rows": ("final_rows", None),
}


def _funnel_key(label: str) -> tuple[str, str | None] | None:
    """Match a funnel label, ignoring its parenthesised threshold -- the
    `(<200 chars)`, `(>14d)` and `(WRatio>=90)` suffixes move with config."""
    base = label.split("(")[0].strip().lower()
    return _FUNNEL_KEYS.get(base)


def parse_report(text: str, run_id: str = "", path: str = "") -> RunReport:
    """Parse one report. Never raises: anything unrecognised lands in
    `parse_notes` and the rest of the record is still returned."""
    report = RunReport(run_id=run_id, path=path)
    if not text.strip():
        report.parse_notes.append("empty report")
        return report

    current: SourceRun | None = None
    section = ""          # "discovery" | "cleaning" | ""
    subsection = ""
    fence: list[str] | None = None
    in_errors = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip()

        if line.startswith("```"):
            if fence is None:
                fence = []
            else:
                if current is not None and current.status is SourceStatus.CRASHED:
                    current.crash_traceback = "\n".join(fence)
                fence = None
            continue
        if fence is not None:
            fence.append(raw_line)
            continue

        m = _RUN_HEAD_RE.match(line)
        if m:
            report.run_id = report.run_id or m.group(1)
            continue

        if line.startswith("## "):
            head = line[3:].strip().lower()
            section = "cleaning" if head.startswith("cleaning") else (
                "discovery" if head.startswith("discovery") else head)
            report.has_discovery |= section == "discovery"
            report.has_cleaning |= section == "cleaning"
            current, subsection, in_errors = None, "", False
            continue

        if line.startswith("### "):
            subsection = line[4:].strip()
            in_errors = False
            m = _SOURCE_RE.match(line)
            if m:
                current = report.sources.setdefault(m.group(1), SourceRun(name=m.group(1)))
                current.in_discovery = True
            else:
                current = None
            continue

        if not line:
            in_errors = False
            continue

        if line.startswith("Run overrides:"):
            report.overrides = line.split(":", 1)[1].strip()
            continue

        m = _WALL_RE.match(line)
        if m:
            report.wall_time_s = _as_float(m.group(1))
            report.serial_time_s = _as_float(m.group(2))
            continue

        m = _NOT_STARTED_RE.match(line)
        if m:
            for name in [n.strip() for n in m.group(1).split(",") if n.strip()]:
                report.sources.setdefault(name, SourceRun(name=name)).status = (
                    SourceStatus.NOT_STARTED)
            continue

        if current is not None:
            if _parse_source_line(current, line):
                in_errors = line == "Errors:"
                continue
            if in_errors and line.startswith("- "):
                current.errors.append(line[2:].strip())
                continue
            # Anything else inside a source section is that source's own
            # report lines -- per-term counts and the like. Not parsed.
            continue

        if section == "cleaning":
            _parse_cleaning_line(report, line, subsection)

    if not report.sources and report.has_discovery:
        report.parse_notes.append("no source sections found")
    if not report.has_cleaning:
        report.parse_notes.append("no cleaning section")
    for src in report.sources.values():
        if src.in_discovery and src.status is SourceStatus.UNKNOWN:
            report.parse_notes.append(f"{src.name}: no status line (truncated?)")
    return report


def _parse_source_line(src: SourceRun, line: str) -> bool:
    """True if `line` was a status/metadata line for this source."""
    m = _TIME_RE.match(line)
    if m:
        src.duration_s = _as_float(m.group(1))
        return True
    m = _ROWS_RE.match(line)
    if m:
        src.rows = int(m.group(1))
        src.status = SourceStatus.OK if src.rows else SourceStatus.ZERO
        return True
    m = _ZERO_RE.match(line)
    if m:
        src.rows, src.status = 0, SourceStatus.ZERO
        src.detail = (m.group(1) or "").strip()
        return True
    m = _SKIPPED_RE.match(line)
    if m:
        src.status = SourceStatus.SKIPPED
        src.detail = (m.group(1) or "").strip()
        return True
    if line.startswith("**CRASHED**"):
        src.status = SourceStatus.CRASHED
        src.detail = line
        return True
    if line.startswith("**DEADLINE REACHED**"):
        src.truncated = True
        return True
    return line == "Errors:"


def _parse_cleaning_line(report: RunReport, line: str, subsection: str) -> None:
    m = _PRUNED_RE.match(line)
    if m:
        report.funnel["pruned_raw"] = int(m.group(1))
        return

    m = _FUNNEL_RE.match(line)
    if m:
        keys = _funnel_key(m.group("label"))
        if keys is None:
            return
        value_key, delta_key = keys
        report.funnel[value_key] = int(m.group("value"))
        if delta_key and m.group("delta") is not None:
            # Accumulate: the old format splits dedupe across two lines.
            report.funnel[delta_key] = (
                report.funnel.get(delta_key, 0) + int(m.group("delta"))
                if delta_key == "dropped_dedupe"
                else int(m.group("delta")))
        return

    if subsection.lower().startswith("per-source counts"):
        m = _TABLE_ROW_RE.match(line)
        if m and m.group(1).lower() != "source":
            src = report.sources.setdefault(m.group(1), SourceRun(name=m.group(1)))
            src.raw_count = int(m.group(2))
            src.final_count = int(m.group(3))


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --- loading ---------------------------------------------------------------

def load_reports(runs_dir: Path | None = None, limit: int = DEFAULT_WINDOW + 1
                 ) -> list[RunReport]:
    """The `limit` most recent reports, newest first. Run ids sort
    lexicographically by time (`YYYY-MM-DD_HHMM`), so the filename is the
    ordering key; anything unreadable is skipped with a note on its own
    record rather than dropped."""
    runs_dir = Path(runs_dir) if runs_dir is not None else JOBS_RUNS
    if not runs_dir.is_dir():
        return []
    out: list[RunReport] = []
    # Run ids sort by time; a file named anything else is scratch and sorts
    # behind every real run rather than ahead of them.
    ordered = sorted(runs_dir.glob("*.md"),
                     key=lambda p: (bool(_RUN_ID_RE.match(p.stem)), p.stem),
                     reverse=True)
    for path in ordered[:limit]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            out.append(RunReport(run_id=path.stem, path=str(path),
                                 parse_notes=[f"unreadable: {exc}"]))
            continue
        out.append(parse_report(text, run_id=path.stem, path=str(path)))
    return out


# --- comparisons -----------------------------------------------------------

@dataclass
class SourceTrend:
    name: str
    status: SourceStatus
    rows: int | None
    median_rows: float | None
    # rows / median, None when there is no history to compare against.
    ratio: float | None
    below_median: bool
    zero_streak: int
    errors: int
    median_errors: float | None
    error_delta: float | None
    silent_zero: bool


@dataclass
class Digest:
    run_id: str
    wall_time_s: float | None
    final_rows: int | None
    new_job_ids: int | None
    funnel: dict[str, int]
    history_runs: int
    trends: list[SourceTrend] = field(default_factory=list)
    crashed: list[str] = field(default_factory=list)
    silent_zeros: list[str] = field(default_factory=list)
    below_median: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    parse_notes: list[str] = field(default_factory=list)


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def build_digest(reports: list[RunReport], window: int = DEFAULT_WINDOW) -> Digest | None:
    """Compare the newest report against the `window` runs before it.

    Every number the digest shows is computed here, so a renderer never has to
    do arithmetic. A skipped lane is excluded from the medians and from every
    alarm list -- it is not a failure and it is not a data point.
    """
    if not reports:
        return None
    latest, history = reports[0], reports[1:window + 1]

    digest = Digest(
        run_id=latest.run_id,
        wall_time_s=latest.wall_time_s,
        final_rows=latest.final_rows,
        new_job_ids=latest.new_job_ids,
        funnel=dict(latest.funnel),
        history_runs=len(history),
        parse_notes=list(latest.parse_notes),
    )

    for name, src in latest.sources.items():
        if not src.in_discovery and src.status is SourceStatus.UNKNOWN:
            continue  # cleaning-table-only row: no lane outcome to trend
        past = [h.sources[name] for h in history if name in h.sources]
        median_rows = _median([p.rows for p in past
                               if p.rows is not None and p.status is not SourceStatus.SKIPPED])
        median_errors = _median([float(len(p.errors)) for p in past
                                 if p.status is not SourceStatus.SKIPPED])
        ratio = (src.rows / median_rows
                 if src.rows is not None and median_rows else None)

        streak = 0
        if src.status is SourceStatus.ZERO:
            streak = 1
            for h in history:
                prev = h.sources.get(name)
                if prev is None or prev.status is SourceStatus.SKIPPED:
                    continue
                if prev.status is SourceStatus.ZERO:
                    streak += 1
                else:
                    break

        below = (src.status is not SourceStatus.SKIPPED
                 and ratio is not None and ratio < BELOW_MEDIAN_FRACTION)
        trend = SourceTrend(
            name=name,
            status=src.status,
            rows=src.rows,
            median_rows=median_rows,
            ratio=ratio,
            below_median=below,
            zero_streak=streak,
            errors=len(src.errors),
            median_errors=median_errors,
            error_delta=(len(src.errors) - median_errors
                         if median_errors is not None else None),
            # Zero rows, no error, and this lane normally returns rows: the
            # quiet-block shape. Gated on the median rather than on a source
            # name, so a lane whose normal is zero (an empty inbox) never fires.
            silent_zero=src.is_alarming_zero and bool(median_rows),
        )
        digest.trends.append(trend)

        if src.status is SourceStatus.CRASHED:
            digest.crashed.append(name)
        if src.status is SourceStatus.SKIPPED:
            digest.skipped.append(name)
        if trend.silent_zero:
            digest.silent_zeros.append(name)
        if below:
            digest.below_median.append(name)
        if src.truncated:
            digest.truncated.append(name)

    digest.trends.sort(key=lambda t: t.name)
    for bucket in (digest.crashed, digest.silent_zeros, digest.below_median,
                   digest.skipped, digest.truncated):
        bucket.sort()
    return digest


def digest_dict(digest: Digest | None) -> dict:
    if digest is None:
        return {}
    out = asdict(digest)
    out["trends"] = [{**asdict(t), "status": t.status.value} for t in digest.trends]
    return out


# --- CLI -------------------------------------------------------------------

def _text_lines(digest: Digest) -> list[str]:
    """The same facts as `--json`, one per line, for a human reading the CLI
    directly. The `/discover` command renders from `--json`."""
    lines = [f"run: {digest.run_id}"
             + (f"  wall {digest.wall_time_s:.0f}s" if digest.wall_time_s else "")
             + (f"  final rows {digest.final_rows}" if digest.final_rows is not None else "")]
    for t in digest.trends:
        bits = [f"{t.name}: {t.status.value}"]
        if t.rows is not None:
            bits.append(f"rows={t.rows}")
        if t.median_rows is not None:
            bits.append(f"median={t.median_rows:g}")
        if t.ratio is not None:
            bits.append(f"ratio={t.ratio:.2f}")
        if t.errors:
            bits.append(f"errors={t.errors}")
        if t.zero_streak > 1:
            bits.append(f"zero x{t.zero_streak}")
        lines.append("  " + "  ".join(bits))
    for label, names in (("crashed", digest.crashed),
                         ("silent zero", digest.silent_zeros),
                         ("below median", digest.below_median),
                         ("truncated", digest.truncated),
                         ("skipped", digest.skipped)):
        if names:
            lines.append(f"{label}: {', '.join(names)}")
    for note in digest.parse_notes:
        lines.append(f"note: {note}")
    return lines


def main(argv=None) -> int:
    """`discover-digest` -- parse the recent run reports and print the trend
    facts. Read-only; writes nothing."""
    parser = argparse.ArgumentParser(
        description="Summarise the most recent jobs/runs/ reports.")
    parser.add_argument("--runs-dir", type=Path, default=None)
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                        help="How many prior runs the medians cover.")
    parser.add_argument("--run-id", type=str, default=None,
                        help="Digest this run id instead of the newest.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.window < 0:
        print("ERROR: --window must be >= 0.")
        return 1

    reports = load_reports(args.runs_dir, limit=args.window + 1
                           if not args.run_id else 10_000)
    if args.run_id:
        idx = next((i for i, r in enumerate(reports) if r.run_id == args.run_id), None)
        if idx is None:
            print(f"ERROR: no report for run id {args.run_id!r}.")
            return 1
        reports = reports[idx:idx + args.window + 1]

    digest = build_digest(reports, window=args.window)
    if digest is None:
        print("ERROR: no run reports found.")
        return 1

    if args.json:
        print(json.dumps(digest_dict(digest), indent=2))
    else:
        print("\n".join(_text_lines(digest)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
