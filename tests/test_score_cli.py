import json

import pytest

from src.score_cli import coverage, split_by_vertical


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


class TestSplitByVertical:
    def test_routes_rows_and_preserves_order(self, tmp_path):
        unscored = tmp_path / "unscored.jsonl"
        _write_jsonl(unscored, [
            {"job_id": "a", "vertical": "sap"},
            {"job_id": "b", "vertical": "risk_ai"},
            {"job_id": "c", "vertical": "sap"},
        ])
        counts = split_by_vertical(unscored)
        assert counts == {"sap": 2, "ai_eng": 0, "risk_ai": 1}
        sap_ids = [json.loads(l)["job_id"]
                   for l in (tmp_path / "unscored_sap.jsonl").open()]
        assert sap_ids == ["a", "c"]
        risk_ids = [json.loads(l)["job_id"]
                    for l in (tmp_path / "unscored_risk_ai.jsonl").open()]
        assert risk_ids == ["b"]

    def test_empty_dump_writes_empty_files(self, tmp_path):
        unscored = tmp_path / "unscored.jsonl"
        unscored.write_text("")
        counts = split_by_vertical(unscored)
        assert counts == {"sap": 0, "ai_eng": 0, "risk_ai": 0}
        assert (tmp_path / "unscored_sap.jsonl").read_text() == ""
        assert (tmp_path / "unscored_risk_ai.jsonl").read_text() == ""

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
                    {"batch_sap_001.json": ["a", "b"],
                     "batch_risk_ai_001.json": ["c"]})
        r = coverage(tmp_path)
        assert r == {"staged": 3, "expected": 3, "missing": [],
                     "unexpected": [], "dupes": 0}

    def test_missing_rows(self, tmp_path):
        self._stage(tmp_path, ["a", "b", "c"],
                    {"batch_sap_001.json": ["a"]})
        r = coverage(tmp_path)
        assert r["missing"] == ["b", "c"]
        assert r["staged"] == 1

    def test_unexpected_and_dupes(self, tmp_path):
        self._stage(tmp_path, ["a", "b"],
                    {"batch_sap_001.json": ["a", "b"],
                     "batch_sap_002.json": ["b", "z"]})
        r = coverage(tmp_path)
        assert r["unexpected"] == ["z"]
        assert r["dupes"] == 1
