import argparse
import json

import pandas as pd
import pytest

from src import score_cli
from src.discovery.cleaning import CLEAN_COLUMNS
from src.score_cli import coverage, judge_ranges, split_by_vertical


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _score(job_id: str) -> dict:
    return {
        "job_id": job_id,
        "fit_score": 75,
        "fit_subscores": {"title": 30, "skills": 30, "seniority": 15, "domain": 0},
        "vertical": "example_primary",
        "sponsorship_label": "opt_ok",
        "sponsorship_evidence": "no visa sponsorship",
        "reasoning": "fit",
        "keywords_to_mirror": ["widget assembly"],
        "suggested_action": "tailor",
    }


def _make_clean(tmp_path, job_ids=("aaaaaaaa", "bbbbbbbb")):
    """Minimal clean.parquet holding the given ids, so prune_scored keeps
    every merged row."""
    base = {c: "" for c in CLEAN_COLUMNS}
    base.update({
        "remote_flag": False,
        "posted_date": pd.Timestamp("2026-06-01"),
        "posted_date_missing": False,
        "scraped_date": pd.Timestamp("2026-06-06"),
        "salary_min": float("nan"),
        "salary_max": float("nan"),
        "already_seen": False,
        "fit_score": float("nan"),
        "shortlist_rank": float("nan"),
        "sponsorship_label": "unknown",
    })
    df = pd.DataFrame([{**base, "job_id": j, "vertical": "example_primary"} for j in job_ids],
                      columns=CLEAN_COLUMNS)
    p = tmp_path / "clean.parquet"
    df.to_parquet(p, index=False)
    return p


class TestSplitByVertical:
    def test_routes_rows_and_preserves_order(self, tmp_path):
        unscored = tmp_path / "unscored.jsonl"
        _write_jsonl(unscored, [
            {"job_id": "a", "vertical": "example_primary"},
            {"job_id": "b", "vertical": "example_secondary"},
            {"job_id": "c", "vertical": "example_primary"},
        ])
        counts = split_by_vertical(unscored)
        assert counts == {"example_primary": 2, "example_tertiary": 0, "example_secondary": 1}
        sap_ids = [json.loads(l)["job_id"]
                   for l in (tmp_path / "unscored_example_primary.jsonl").open()]
        assert sap_ids == ["a", "c"]
        risk_ids = [json.loads(l)["job_id"]
                    for l in (tmp_path / "unscored_example_secondary.jsonl").open()]
        assert risk_ids == ["b"]

    def test_empty_dump_writes_empty_files(self, tmp_path):
        unscored = tmp_path / "unscored.jsonl"
        unscored.write_text("")
        counts = split_by_vertical(unscored)
        assert counts == {"example_primary": 0, "example_tertiary": 0, "example_secondary": 0}
        assert (tmp_path / "unscored_example_primary.jsonl").read_text() == ""
        assert (tmp_path / "unscored_example_secondary.jsonl").read_text() == ""

    def test_unexpected_vertical_raises(self, tmp_path):
        unscored = tmp_path / "unscored.jsonl"
        _write_jsonl(unscored, [{"job_id": "a", "vertical": ""}])
        with pytest.raises(ValueError, match="unexpected vertical"):
            split_by_vertical(unscored)


class TestCoverage:
    def _stage(self, tmp_path, dumped_ids, batches):
        _write_jsonl(tmp_path / "unscored.jsonl",
                     [{"job_id": j} for j in dumped_ids])
        for name, ids in batches.items():
            (tmp_path / name).write_text(
                json.dumps([{"job_id": j} for j in ids]))

    def test_all_clear(self, tmp_path):
        self._stage(tmp_path, ["a", "b", "c"],
                    {"batch_example_primary_001.json": ["a", "b"],
                     "batch_example_secondary_001.json": ["c"]})
        r = coverage(tmp_path)
        assert r == {"staged": 3, "expected": 3, "missing": [],
                     "unexpected": [], "dupes": 0, "unreadable": []}

    def test_missing_rows(self, tmp_path):
        self._stage(tmp_path, ["a", "b", "c"],
                    {"batch_example_primary_001.json": ["a"]})
        r = coverage(tmp_path)
        assert r["missing"] == ["b", "c"]
        assert r["staged"] == 1

    def test_unexpected_and_dupes(self, tmp_path):
        self._stage(tmp_path, ["a", "b"],
                    {"batch_example_primary_001.json": ["a", "b"],
                     "batch_example_primary_002.json": ["b", "z"]})
        r = coverage(tmp_path)
        assert r["unexpected"] == ["z"]
        assert r["dupes"] == 1

    def test_unreadable_batch_is_reported_not_raised(self, tmp_path):
        self._stage(tmp_path, ["a", "b"], {"batch_example_primary_001.json": ["a"]})
        (tmp_path / "batch_example_primary_002.json").write_text('[{"job_id": "b"')
        r = coverage(tmp_path)
        assert r["unreadable"] == ["batch_example_primary_002.json"]
        assert r["missing"] == ["b"]  # still diagnosable
        assert r["staged"] == 1

    def test_non_array_batch_is_reported_not_raised(self, tmp_path):
        self._stage(tmp_path, ["a"], {})
        (tmp_path / "batch_example_primary_001.json").write_text('{"job_id": "a"}')
        r = coverage(tmp_path)
        assert r["unreadable"] == ["batch_example_primary_001.json"]
        assert r["staged"] == 0


class TestMergeRefusesToClearStaging:
    """A batch merge_scores_from_dir skipped must survive `merge` and
    `prepare` — clearing it destroys ~100 judged rows and forces a re-judge
    of the whole range on the next /score."""

    def _staging(self, tmp_path, monkeypatch, batches: dict[str, str]):
        staging = tmp_path / "staging"
        staging.mkdir()
        for name, body in batches.items():
            (staging / name).write_text(body)
        monkeypatch.setattr(score_cli, "STAGING", staging)
        monkeypatch.setattr(score_cli, "SCORED", tmp_path / "scored.parquet")
        monkeypatch.setattr(score_cli, "CLEAN", _make_clean(tmp_path))
        return staging

    def test_merge_keeps_staging_and_exits_nonzero(self, tmp_path, monkeypatch, capsys):
        staging = self._staging(tmp_path, monkeypatch, {
            "batch_example_primary_001.json": json.dumps([_score("aaaaaaaa")]),
            "batch_example_primary_002.json": '[{"job_id": "bbbbbbbb"',
        })
        rc = score_cli._cmd_merge(argparse.Namespace(model="t"))
        out = capsys.readouterr().out
        assert rc == 1
        assert "merged=1" in out
        assert "batch_example_primary_002.json" in out
        assert (staging / "batch_example_primary_002.json").exists()
        assert (staging / "batch_example_primary_001.json").exists()

    def test_merge_clears_staging_when_all_batches_read(self, tmp_path, monkeypatch, capsys):
        staging = self._staging(tmp_path, monkeypatch, {
            "batch_example_primary_001.json": json.dumps([_score("aaaaaaaa")]),
        })
        rc = score_cli._cmd_merge(argparse.Namespace(model="t"))
        assert rc == 0
        assert "merged=1" in capsys.readouterr().out
        assert not list(staging.glob("batch_*.json"))

    def test_merge_is_rerunnable_after_repair(self, tmp_path, monkeypatch, capsys):
        staging = self._staging(tmp_path, monkeypatch, {
            "batch_example_primary_001.json": json.dumps([_score("aaaaaaaa")]),
            "batch_example_primary_002.json": '[{"job_id": "bbbbbbbb"',
        })
        assert score_cli._cmd_merge(argparse.Namespace(model="t")) == 1
        (staging / "batch_example_primary_002.json").write_text(json.dumps([_score("bbbbbbbb")]))
        capsys.readouterr()
        rc = score_cli._cmd_merge(argparse.Namespace(model="t"))
        assert rc == 0
        # batch_001 re-merges without duplicating: rows overwrite by job_id
        assert "merged=2" in capsys.readouterr().out
        df = pd.read_parquet(tmp_path / "scored.parquet")
        assert sorted(df["job_id"]) == ["aaaaaaaa", "bbbbbbbb"]
        assert not list(staging.glob("batch_*.json"))

    def test_prepare_aborts_instead_of_clearing_leftovers(self, tmp_path, monkeypatch, capsys):
        """prepare is the worse call site: it clears staging then re-dumps, so
        the loss leaves no trace at all."""
        staging = self._staging(tmp_path, monkeypatch, {
            "batch_example_primary_001.json": json.dumps([_score("aaaaaaaa")]),
            "batch_example_primary_002.json": '[{"job_id": "bbbbbbbb"',
        })
        cleared = []
        monkeypatch.setattr(score_cli, "clear_staging", lambda s: cleared.append(s))
        rc = score_cli._cmd_prepare(argparse.Namespace(force_all=False))
        out = capsys.readouterr().out
        assert rc == 1
        assert cleared == []  # never reached
        assert "batch_example_primary_002.json" in out
        assert (staging / "batch_example_primary_002.json").exists()
        assert "recovered=1" in out  # the good batch was still banked


class TestJudgeRanges:
    """CLAUDE.md's central /score correctness claim — "a judge only picks rows
    from its assigned range, so gaps/collisions are impossible by
    construction" — is entirely this function. Assert the invariant, not
    examples: the ranges must tile 1..n exactly."""

    @staticmethod
    def _rows(ranges, vertical):
        """Every row number a vertical's judges would claim, in emitted order,
        with duplicates preserved so overlaps are visible."""
        out = []
        for v, a, b in ranges:
            if v == vertical:
                out.extend(range(a, b + 1))
        return out

    @pytest.mark.parametrize("n", [1, 2, 7, 99, 100, 101, 199, 200, 201, 250, 1000])
    @pytest.mark.parametrize("chunk", [10, 20, 30, 100, 200, 1000])
    def test_ranges_tile_one_to_n_exactly(self, n, chunk):
        ranges = judge_ranges({"example_primary": n}, chunk=chunk)
        assert self._rows(ranges, "example_primary") == list(range(1, n + 1))

    @pytest.mark.parametrize("chunk", [10, 30, 100])
    def test_zero_count_emits_no_range(self, chunk):
        assert judge_ranges({"example_primary": 0}, chunk=chunk) == []

    def test_all_verticals_zero_emits_nothing(self):
        assert judge_ranges({"example_primary": 0, "example_secondary": 0, "example_tertiary": 0}) == []

    def test_empty_counts_emits_nothing(self):
        assert judge_ranges({}) == []

    @pytest.mark.parametrize("chunk", [10, 30, 100])
    def test_ranges_are_ascending_and_non_overlapping(self, chunk):
        ranges = judge_ranges({"example_primary": 250}, chunk=chunk)
        prev_end = 0
        for _, a, b in ranges:
            assert a == prev_end + 1  # contiguous, no gap
            assert b >= a             # non-empty
            prev_end = b
        assert prev_end == 250

    @pytest.mark.parametrize("chunk,n", [(100, 250), (30, 70), (10, 25)])
    def test_only_the_last_chunk_is_short(self, chunk, n):
        sizes = [b - a + 1 for _, a, b in judge_ranges({"example_primary": n}, chunk=chunk)]
        assert all(s == chunk for s in sizes[:-1])
        assert 0 < sizes[-1] <= chunk

    def test_exact_multiple_leaves_no_short_chunk(self):
        assert judge_ranges({"example_primary": 200}, chunk=100) == [
            ("example_primary", 1, 100), ("example_primary", 101, 200),
        ]

    def test_count_below_chunk_is_one_range(self):
        assert judge_ranges({"example_primary": 42}, chunk=100) == [("example_primary", 1, 42)]

    def test_count_of_one_is_a_degenerate_range(self):
        assert judge_ranges({"example_primary": 1}, chunk=100) == [("example_primary", 1, 1)]

    def test_default_chunk_is_100(self):
        assert judge_ranges({"example_primary": 150}) == [("example_primary", 1, 100), ("example_primary", 101, 150)]

    def test_verticals_are_independent_and_never_interleave(self):
        """Each vertical's ranges index into its own unscored_<v>.jsonl, so
        numbering restarts at 1 per vertical and the groups stay contiguous."""
        ranges = judge_ranges({"example_primary": 150, "example_secondary": 0, "example_tertiary": 100})
        assert ranges == [
            ("example_primary", 1, 100), ("example_primary", 101, 150), ("example_tertiary", 1, 100),
        ]
        assert self._rows(ranges, "example_primary") == list(range(1, 151))
        assert self._rows(ranges, "example_tertiary") == list(range(1, 101))

    def test_emission_order_follows_counts_insertion_order(self):
        """prepare prints these lines in order; the orchestrator fans out in
        the order it reads them."""
        names = [v for v, _, _ in judge_ranges({"example_tertiary": 1, "example_primary": 1})]
        assert names == ["example_tertiary", "example_primary"]

    @pytest.mark.parametrize("n", [-1, -100])
    def test_negative_count_emits_no_range(self, n):
        """A count can't go negative in practice, but the loop must not spin
        or emit an inverted range if one ever did."""
        assert judge_ranges({"example_primary": n}) == []

    def test_no_collision_in_the_batch_filename_formula_at_default_chunk(self):
        """score-judge.md derives its batch filename as NNN = ceil(first_row/10),
        which is only injective while chunk stays a multiple of 10 (item 16).
        Guard the shipped default."""
        ranges = judge_ranges({"example_primary": 1000, "example_tertiary": 450}, chunk=100)
        keys = [(v, -(-a // 10)) for v, a, _ in ranges]
        assert len(keys) == len(set(keys))

    @pytest.mark.parametrize("chunk", [1, 5, 7, 15, 99, 101, 0, -10])
    def test_off_grid_chunk_is_rejected(self, chunk):
        """Off a 10-row grid two judges compute the same batch_<v>_NNN.json and
        the second Write destroys the first's rows; check-coverage then reports
        them as `missing`, so /score re-spawns into the same collision. Fail
        loud at the only place the chunk size is chosen."""
        with pytest.raises(ValueError, match="multiple of 10"):
            judge_ranges({"example_primary": 20}, chunk=chunk)

    @pytest.mark.parametrize("chunk", [10, 20, 100, 1000])
    def test_on_grid_chunks_are_accepted_and_collision_free(self, chunk):
        ranges = judge_ranges({"example_primary": 1000, "example_tertiary": 450}, chunk=chunk)
        keys = [(v, -(-a // 10)) for v, a, _ in ranges]
        assert len(keys) == len(set(keys))

    def test_the_guard_fires_before_any_range_is_emitted(self):
        """No partial fan-out: a bad chunk must not leave callers holding
        half a range list."""
        with pytest.raises(ValueError):
            judge_ranges({"example_primary": 0}, chunk=5)


class TestClearStaging:
    """Every other test monkeypatches clear_staging away, so the real one —
    which deletes judged batch files — was unexercised."""

    def test_removes_every_staging_glob(self, tmp_path):
        staging = tmp_path / "staging"
        staging.mkdir()
        for name in ("batch_example_primary_001.json", "batch_example_tertiary_002.json",
                     "unscored.jsonl", "unscored_example_primary.jsonl",
                     "auto_skip.jsonl", "auto_skip_ineligible.jsonl",
                     "auto_skip_sap.jsonl"):
            (staging / name).write_text("x")
        score_cli.clear_staging(staging)
        assert sorted(p.name for p in staging.iterdir()) == []

    def test_leaves_unrelated_files_alone(self, tmp_path):
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "batch_example_primary_001.json").write_text("x")
        keep = {
            "notes.md": "hand notes",
            "batch_example_primary_001.json.bak": "a manual repair copy",
            "scored.parquet": "not a staging file",
            "unscored.jsonl.tmp": "partial write",
        }
        for name, body in keep.items():
            (staging / name).write_text(body)
        score_cli.clear_staging(staging)
        assert sorted(p.name for p in staging.iterdir()) == sorted(keep)

    def test_is_a_noop_on_empty_staging(self, tmp_path):
        staging = tmp_path / "staging"
        staging.mkdir()
        score_cli.clear_staging(staging)
        assert list(staging.iterdir()) == []

    def test_missing_staging_dir_does_not_raise(self, tmp_path):
        """glob on a nonexistent dir yields nothing rather than raising, so
        prepare can call this before mkdir."""
        score_cli.clear_staging(tmp_path / "absent")

    def test_does_not_recurse_into_subdirs(self, tmp_path):
        staging = tmp_path / "staging"
        (staging / "archive").mkdir(parents=True)
        (staging / "archive" / "batch_example_primary_001.json").write_text("x")
        score_cli.clear_staging(staging)
        assert (staging / "archive" / "batch_example_primary_001.json").exists()


class TestRangesCommand:
    """`ranges` is the single range printer /score (via prepare) and /rescore
    share — /rescore used to re-derive the chunking in prompt prose."""

    def _staging(self, tmp_path, monkeypatch, counts: dict[str, int],
                 *, mkdir=True):
        staging = tmp_path / "staging"
        if mkdir:
            staging.mkdir()
            for v, n in counts.items():
                _write_jsonl(staging / f"unscored_{v}.jsonl",
                             [{"job_id": f"{v}{i:04d}", "vertical": v}
                              for i in range(n)])
        monkeypatch.setattr(score_cli, "STAGING", staging)
        return staging

    def _run(self, capsys):
        rc = score_cli._cmd_ranges(argparse.Namespace())
        return rc, capsys.readouterr().out.splitlines()

    def test_prints_counts_then_one_line_per_judge(self, tmp_path, monkeypatch, capsys, cfg):
        self._staging(tmp_path, monkeypatch, {"example_primary": 150, "example_tertiary": 0, "example_secondary": 100})
        rc, lines = self._run(capsys)
        assert rc == 0
        assert lines[0] == "example_primary=150 example_secondary=100 example_tertiary=0"
        assert lines[1:] == [
            "range example_primary 1-100", "range example_primary 101-150", "range example_secondary 1-100",
        ]

    def test_line_format_matches_prepare(self, tmp_path, monkeypatch, capsys, cfg):
        """score.md/rescore.md both parse `range <vertical> <A>-<B>`."""
        self._staging(tmp_path, monkeypatch, {"example_primary": 5})
        _, lines = self._run(capsys)
        assert lines[1] == "range example_primary 1-5"

    def test_counts_cover_every_configured_vertical_in_order(
            self, tmp_path, monkeypatch, capsys, cfg):
        self._staging(tmp_path, monkeypatch, {})
        _, lines = self._run(capsys)
        assert lines[0] == " ".join(f"{v}=0" for v in cfg.names)

    def test_missing_per_vertical_file_counts_zero(self, tmp_path, monkeypatch, capsys, cfg):
        """A --vertical-scoped /rescore only splits one lane; the others have
        no file at all and must not error."""
        self._staging(tmp_path, monkeypatch, {"example_primary": 20})
        rc, lines = self._run(capsys)
        assert rc == 0
        assert lines[0].startswith("example_primary=20 ")
        assert lines[1:] == ["range example_primary 1-20"]

    def test_missing_staging_dir_reports_zeroes(self, tmp_path, monkeypatch, capsys, cfg):
        self._staging(tmp_path, monkeypatch, {}, mkdir=False)
        rc, lines = self._run(capsys)
        assert rc == 0
        assert lines == [" ".join(f"{v}=0" for v in cfg.names)]

    def test_empty_file_emits_no_range(self, tmp_path, monkeypatch, capsys, cfg):
        staging = self._staging(tmp_path, monkeypatch, {})
        (staging / "unscored_example_primary.jsonl").write_text("")
        rc, lines = self._run(capsys)
        assert rc == 0
        assert len(lines) == 1  # counts only, no range lines

    def test_blank_lines_are_not_counted(self, tmp_path, monkeypatch, capsys, cfg):
        """Line count is the judge's sed address space — a trailing newline
        must not create a phantom row."""
        staging = self._staging(tmp_path, monkeypatch, {"example_primary": 3})
        (staging / "unscored_example_primary.jsonl").write_text(
            (staging / "unscored_example_primary.jsonl").read_text() + "\n\n")
        _, lines = self._run(capsys)
        assert lines[0].startswith("example_primary=3 ")
        assert lines[1] == "range example_primary 1-3"

    def test_counts_match_the_files_split_wrote(self, tmp_path, monkeypatch, capsys, cfg):
        """End to end against the real splitter: /rescore runs split, then
        ranges, so the two must agree on every count."""
        staging = self._staging(tmp_path, monkeypatch, {})
        _write_jsonl(staging / "unscored.jsonl",
                     [{"job_id": f"{i:08x}", "vertical": "example_primary"} for i in range(7)]
                     + [{"job_id": f"b{i:07x}", "vertical": "example_secondary"} for i in range(2)])
        split_counts = split_by_vertical(staging / "unscored.jsonl")
        _, lines = self._run(capsys)
        assert lines[0] == " ".join(f"{v}={n}" for v, n in split_counts.items())
        assert lines[1:] == ["range example_primary 1-7", "range example_secondary 1-2"]

    def test_exposed_as_a_cli_subcommand(self, tmp_path, monkeypatch, capsys, cfg):
        """rescore.md calls `uv run python -m src.score_cli ranges`."""
        self._staging(tmp_path, monkeypatch, {"example_primary": 12})
        assert score_cli.main(["ranges"]) == 0
        out = capsys.readouterr().out
        assert "range example_primary 1-12" in out
