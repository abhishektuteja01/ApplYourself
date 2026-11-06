"""write_parquet: the tmp+rename guarantee for every canonical parquet."""

from __future__ import annotations

import stat
import threading
from unittest import mock

import pandas as pd
import pytest

from applyourself.shared.parquet_io import write_parquet


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


def test_the_temp_name_is_unique_per_writer(tmp_path):
    """A second writer's fixed-name temp must not be in play at all."""
    path = tmp_path / "clean.parquet"
    other = tmp_path / "clean.parquet.tmp"
    other.write_bytes(b"owned by another writer")
    write_parquet(pd.DataFrame({"job_id": ["x"]}), path)
    assert other.read_bytes() == b"owned by another writer"


def test_concurrent_writes_to_one_path_do_not_interfere(tmp_path):
    """Two writers overlapping mid-write: the survivor is a whole frame."""
    path = tmp_path / "clean.parquet"
    real = pd.DataFrame.to_parquet
    barrier = threading.Barrier(2, timeout=30)
    errors: list[BaseException] = []

    def staggered(self, target, *args, **kwargs):
        barrier.wait()
        return real(self, target, *args, **kwargs)

    def write(tag):
        try:
            write_parquet(pd.DataFrame({"job_id": [tag] * 200}), path)
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            errors.append(exc)

    with mock.patch.object(pd.DataFrame, "to_parquet", staggered):
        threads = [threading.Thread(target=write, args=(t,)) for t in ("aaaaaaaa", "bbbbbbbb")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

    assert not errors
    out = pd.read_parquet(path)
    assert len(out) == 200
    assert set(out["job_id"]) in ({"aaaaaaaa"}, {"bbbbbbbb"})
    assert [p.name for p in tmp_path.iterdir()] == ["clean.parquet"]


def test_the_written_file_keeps_the_usual_mode(tmp_path):
    """mkstemp opens 0600; the result must match a plain write, not that."""
    path = tmp_path / "x.parquet"
    write_parquet(pd.DataFrame({"a": [1]}), path)
    plain = tmp_path / "plain.bin"
    plain.write_bytes(b"")
    assert stat.S_IMODE(path.stat().st_mode) == stat.S_IMODE(plain.stat().st_mode)


def test_a_scored_file_from_newer_code_is_refused_and_an_unstamped_one_reads(tmp_path):
    from applyourself import score_store
    from applyourself.shared import schemas

    path = tmp_path / "scored.parquet"
    write_parquet(pd.DataFrame({"job_id": ["a"]}), path)
    assert list(score_store.read_scored(path)["job_id"]) == ["a"]
    newer = schemas.SCORED_SCHEMA_VERSION + 1
    write_parquet(pd.DataFrame({"job_id": ["a"]}), path, schema_version=newer)
    with pytest.raises(ValueError, match="schema version"):
        score_store.read_scored(path)


def test_a_waiter_on_a_removed_lock_file_does_not_share_the_lock(tmp_path):
    """B waits on A's lock file; A removes it on release. B must take the
    lock on a fresh file, so C still waits for B."""
    import time

    from applyourself.shared.parquet_io import locked

    path = tmp_path / "state.yaml"
    a_in, a_go, b_in, b_go, c_in, c_go = (threading.Event() for _ in range(6))

    def hold(entered, go, remove):
        with locked(path, remove=remove):
            entered.set()
            go.wait(5)

    a = threading.Thread(target=hold, args=(a_in, a_go, True))
    a.start()
    a_in.wait(5)
    b = threading.Thread(target=hold, args=(b_in, b_go, True))
    b.start()
    time.sleep(0.2)  # B is now blocked on A's file
    a_go.set()
    assert b_in.wait(5)
    c = threading.Thread(target=hold, args=(c_in, c_go, True))
    c.start()
    assert not c_in.wait(0.3)
    b_go.set()
    assert c_in.wait(5)
    c_go.set()
    for t in (a, b, c):
        t.join(5)


def test_the_store_is_stamped_and_one_from_newer_code_is_refused(tmp_path):
    from applyourself.discovery import store
    from applyourself.shared import schemas
    from applyourself.shared.parquet_io import schema_version

    path = tmp_path / "jobs.parquet"
    store.save_store(store.empty_store(), path)
    assert schema_version(path) == schemas.STORE_SCHEMA_VERSION
    write_parquet(store.empty_store(), path, schema_version=schemas.STORE_SCHEMA_VERSION + 1)
    with pytest.raises(ValueError, match="schema version"):
        store.load_store(path)
