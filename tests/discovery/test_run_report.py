"""`src/discovery/run_report.py` -- reading the nightly run report back.

Fixtures under `fixtures/runs/` are real report shapes, smallest that pin the
behaviour:

- `2026-01-01_0200` normal run, OLD cleaning format (exact + near dedupe lines)
- `2026-01-02_0200` normal run, CURRENT cleaning format (one `after dedupe` line,
  three-column per-source table, empty-description line)
- `2026-01-03_0200` crashed lane, skipped lane, silent zero, error spike,
  truncated lane, a lane that never started
- `2026-01-04_0200` truncated mid-report: a source header with no status line
  and no `## Cleaning` half
- `2026-01-05_0200` zero bytes
- `2026-01-06_0200` cleaning half only, no `## Discovery`
"""

from pathlib import Path

import pytest

from src.discovery import run_report
from src.discovery.run_report import SourceStatus

RUNS = Path(__file__).parent / "fixtures" / "runs"


def load(run_id: str):
    path = RUNS / f"{run_id}.md"
    return run_report.parse_report(path.read_text(encoding="utf-8"), run_id=run_id)


# --- parsing ---------------------------------------------------------------

def test_normal_run_parses_sources_and_funnel():
    rep = load("2026-01-01_0200")
    assert rep.run_id == "2026-01-01_0200"
    assert rep.has_discovery and rep.has_cleaning
    assert rep.wall_time_s == 100.0
    assert rep.serial_time_s == 180.0
    assert rep.sources["linkedin"].status is SourceStatus.OK
    assert rep.sources["linkedin"].rows == 1000
    assert rep.sources["linkedin"].duration_s == 60.0
    assert rep.sources["greenhouse"].errors == ["board acme: HTTP 404"]
    assert rep.funnel["raw_rows"] == 1500
    assert rep.funnel["final_rows"] == 200
    assert rep.final_rows == 200
    assert rep.funnel["pruned_raw"] == 1
    assert rep.parse_notes == []


def test_per_source_table_attaches_raw_and_final_counts():
    """An OLD two-column table: no gate column to read."""
    rep = load("2026-01-01_0200")
    lin, gh = rep.sources["linkedin"], rep.sources["greenhouse"]
    assert (lin.raw_count, lin.after_gate_count, lin.final_count) == (1000, None, 140)
    assert (gh.raw_count, gh.after_gate_count, gh.final_count) == (500, None, 60)


def test_per_source_table_reads_the_gate_column():
    rep = load("2026-01-02_0200")
    lin, gh = rep.sources["linkedin"], rep.sources["greenhouse"]
    assert (lin.raw_count, lin.after_gate_count, lin.final_count) == (1000, 500, 148)
    assert (gh.raw_count, gh.after_gate_count, gh.final_count) == (500, 400, 62)


def test_empty_description_line_parses():
    assert load("2026-01-02_0200").funnel["dropped_empty_jd"] == 7
    # Absent from an older report rather than reported as zero.
    assert "dropped_empty_jd" not in load("2026-01-01_0200").funnel


def test_old_cleaning_format_sums_both_dedupe_drops():
    rep = load("2026-01-01_0200")
    assert rep.funnel["after_dedupe"] == 380      # the near-dedupe line wins
    assert rep.funnel["dropped_dedupe"] == 320    # 300 exact + 20 near


def test_current_cleaning_format_normalises_to_the_same_keys():
    rep = load("2026-01-02_0200")
    assert rep.funnel["after_dedupe"] == 380
    assert rep.funnel["dropped_dedupe"] == 320
    assert rep.funnel["dropped_expired"] == 170


def test_overrides_line_is_kept():
    assert load("2026-01-03_0200").overrides.startswith("`--max-terms 2`")


def test_crashed_lane_keeps_its_traceback_out_of_the_funnel():
    src = load("2026-01-03_0200").sources["ashby"]
    assert src.status is SourceStatus.CRASHED
    assert src.rows is None
    assert "ValueError: boom" in src.crash_traceback


def test_skipped_lane_is_neither_crash_nor_zero():
    src = load("2026-01-03_0200").sources["workday"]
    assert src.status is SourceStatus.SKIPPED
    assert src.detail == "cadence: every 3 days, last run 2026-01-02"
    assert src.is_alarming_zero is False


def test_zero_rows_with_no_errors_is_the_silent_shape():
    src = load("2026-01-03_0200").sources["linkedin"]
    assert src.status is SourceStatus.ZERO
    assert src.rows == 0
    assert src.errors == []
    assert src.is_alarming_zero is True


def test_zero_rows_with_errors_is_not_silent():
    rep = load("2026-01-03_0200")
    rep.sources["linkedin"].errors.append("HTTP 429")
    assert rep.sources["linkedin"].is_alarming_zero is False


def test_truncated_lane_and_never_started_lane():
    rep = load("2026-01-03_0200")
    assert rep.sources["lever"].truncated is True
    assert rep.sources["lever"].rows == 40
    assert rep.sources["indeed"].status is SourceStatus.NOT_STARTED


def test_error_list_stops_at_the_blank_line():
    rep = load("2026-01-03_0200")
    assert len(rep.sources["greenhouse"].errors) == 4
    assert rep.error_count == 4


def test_truncated_report_degrades_to_notes():
    rep = load("2026-01-04_0200")
    assert rep.sources["linkedin"].status is SourceStatus.UNKNOWN
    assert rep.has_cleaning is False
    assert any("no cleaning section" in n for n in rep.parse_notes)
    assert any("linkedin" in n for n in rep.parse_notes)


def test_empty_report_does_not_raise():
    rep = load("2026-01-05_0200")
    assert rep.sources == {}
    assert rep.funnel == {}
    assert rep.parse_notes == ["empty report"]


def test_cleaning_only_report_parses_its_half():
    rep = load("2026-01-06_0200")
    assert rep.has_discovery is False
    assert rep.has_cleaning is True
    assert rep.final_rows == 150
    assert rep.sources == {}


def test_unknown_sections_and_junk_are_ignored():
    text = "# Run x\n\n## Discovery\n\n## Weather\n\n- rain: lots\n\nblah | blah\n"
    rep = run_report.parse_report(text, run_id="x")
    assert rep.funnel == {}
    assert rep.sources == {}


def test_new_job_ids_is_not_derivable_from_the_report():
    # cleaning.py computes new seen-ledger ids but never writes them out.
    assert load("2026-01-01_0200").new_job_ids is None


# --- loading ---------------------------------------------------------------

def test_load_reports_is_newest_first_and_limited():
    reports = run_report.load_reports(RUNS, limit=3)
    assert [r.run_id for r in reports] == [
        "2026-01-06_0200", "2026-01-05_0200", "2026-01-04_0200"]


def test_load_reports_sorts_non_run_id_filenames_last(tmp_path):
    (tmp_path / "scratch.md").write_text("# Run scratch\n", encoding="utf-8")
    (tmp_path / "2026-01-01_0200.md").write_text("# Run 2026-01-01_0200\n",
                                                 encoding="utf-8")
    assert run_report.load_reports(tmp_path)[0].run_id == "2026-01-01_0200"


def test_load_reports_on_a_missing_directory(tmp_path):
    assert run_report.load_reports(tmp_path / "nope") == []


# --- digest ----------------------------------------------------------------

@pytest.fixture
def digest():
    reports = [load(rid) for rid in
               ("2026-01-03_0200", "2026-01-02_0200", "2026-01-01_0200")]
    return run_report.build_digest(reports)


def test_digest_flags_the_crash_and_the_skip_separately(digest):
    assert digest.crashed == ["ashby"]
    assert digest.skipped == ["workday"]


def test_digest_flags_a_silent_zero_against_the_sources_own_median(digest):
    # linkedin's own median is 1000, so 0 rows with no error is an alarm.
    assert digest.silent_zeros == ["linkedin"]


def test_manual_zero_is_never_an_alarm(digest):
    # manual's own median is 0 -- an empty inbox is its normal.
    assert "manual" not in digest.silent_zeros
    assert "manual" not in digest.below_median


def test_digest_flags_a_source_more_than_half_below_its_median(digest):
    trend = {t.name: t for t in digest.trends}["greenhouse"]
    assert trend.median_rows == 500
    assert trend.rows == 120
    assert trend.ratio == pytest.approx(0.24)
    assert trend.below_median is True
    assert digest.below_median == ["greenhouse", "linkedin"]


def test_digest_reports_the_error_rate_delta(digest):
    trend = {t.name: t for t in digest.trends}["greenhouse"]
    assert trend.errors == 4
    assert trend.median_errors == 1.0
    assert trend.error_delta == 3.0


def test_digest_counts_a_zero_streak(digest):
    assert {t.name: t for t in digest.trends}["manual"].zero_streak == 3


def test_skipped_lane_has_no_row_alarm(digest):
    trend = {t.name: t for t in digest.trends}["workday"]
    assert trend.below_median is False
    assert trend.silent_zero is False
    assert "workday" not in digest.crashed


def test_digest_carries_the_truncated_lane_and_the_funnel(digest):
    assert digest.truncated == ["lever"]
    assert digest.final_rows == 90
    assert digest.funnel["after_dedupe"] == 100
    assert digest.history_runs == 2


def test_digest_of_a_single_run_has_no_medians():
    d = run_report.build_digest([load("2026-01-01_0200")])
    assert d.history_runs == 0
    assert all(t.median_rows is None and t.ratio is None for t in d.trends)
    assert d.below_median == [] and d.silent_zeros == []


def test_digest_of_an_empty_report_list_is_none():
    assert run_report.build_digest([]) is None


def test_digest_of_a_zero_byte_report_still_returns_a_record():
    d = run_report.build_digest([load("2026-01-05_0200")])
    assert d.trends == []
    assert d.parse_notes == ["empty report"]


def test_digest_dict_is_json_safe(digest):
    import json
    payload = json.loads(json.dumps(run_report.digest_dict(digest)))
    assert payload["run_id"] == "2026-01-03_0200"
    assert {t["name"]: t["status"] for t in payload["trends"]}["ashby"] == "crashed"


def test_digest_dict_of_none_is_empty():
    assert run_report.digest_dict(None) == {}


# --- CLI -------------------------------------------------------------------

def test_cli_text_output(capsys):
    assert run_report.main(["--runs-dir", str(RUNS),
                            "--run-id", "2026-01-03_0200", "--window", "2"]) == 0
    out = capsys.readouterr().out
    assert "run: 2026-01-03_0200" in out
    assert "crashed: ashby" in out
    assert "skipped: workday" in out
    assert len(out.strip().splitlines()) <= 20


def test_cli_json_output(capsys):
    import json
    assert run_report.main(["--runs-dir", str(RUNS), "--json",
                            "--run-id", "2026-01-01_0200"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["final_rows"] == 200


def test_cli_unknown_run_id(capsys):
    assert run_report.main(["--runs-dir", str(RUNS), "--run-id", "nope"]) == 1
    assert "no report for run id" in capsys.readouterr().out


def test_cli_on_an_empty_runs_dir(tmp_path, capsys):
    assert run_report.main(["--runs-dir", str(tmp_path)]) == 1
    assert "no run reports found" in capsys.readouterr().out


def test_cli_rejects_a_negative_window(capsys):
    assert run_report.main(["--runs-dir", str(RUNS), "--window", "-1"]) == 1
    assert "--window must be >= 0" in capsys.readouterr().out


def test_module_never_writes(monkeypatch):
    """R7's neighbour: this is a reader. Nothing here opens a file for write."""
    real_open = Path.open

    def guard(self, mode="r", *a, **kw):
        assert "w" not in mode and "a" not in mode, f"wrote to {self}"
        return real_open(self, mode, *a, **kw)

    monkeypatch.setattr(Path, "open", guard)
    run_report.build_digest(run_report.load_reports(RUNS))
