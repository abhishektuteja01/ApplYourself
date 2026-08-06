"""write_parquet: the tmp+rename guarantee for every canonical parquet."""
from __future__ import annotations

import pandas as pd
import pytest

from src.parquet_io import write_parquet


def test_writes_the_frame_and_creates_the_parent(tmp_path):
    path = tmp_path / "nested" / "dir" / "clean.parquet"
    write_parquet(pd.DataFrame({"job_id": ["aaaaaaaa"]}), path)
    assert list(pd.read_parquet(path)["job_id"]) == ["aaaaaaaa"]


def test_drops_the_index_by_default(tmp_path):
    path = tmp_path / "x.parquet"
    df = pd.DataFrame({"a": [1, 2]}, index=["skip", "me"])
    write_parquet(df, path)
    assert list(pd.read_parquet(path).columns) == ["a"]


def test_leaves_no_tmp_file_behind(tmp_path):
    path = tmp_path / "x.parquet"
    write_parquet(pd.DataFrame({"a": [1]}), path)
    assert [p.name for p in tmp_path.iterdir()] == ["x.parquet"]


def test_a_failed_write_leaves_the_previous_file_intact(tmp_path, monkeypatch):
    """The whole point: a crash mid-write must not truncate the canonical file."""
    path = tmp_path / "clean.parquet"
    write_parquet(pd.DataFrame({"job_id": ["good"]}), path)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", boom)
    with pytest.raises(OSError):
        write_parquet(pd.DataFrame({"job_id": ["bad"]}), path)

    assert list(pd.read_parquet(path)["job_id"]) == ["good"]
    assert [p.name for p in tmp_path.iterdir()] == ["clean.parquet"]
