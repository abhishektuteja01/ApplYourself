"""atomic_write_text: a failed write keeps the old file and leaves no temp."""

from __future__ import annotations

from unittest import mock

import pytest

from src.shared.files import atomic_write_text


def test_a_failed_write_keeps_the_old_file_and_no_temp(tmp_path):
    path = tmp_path / "x.json"
    path.write_text("old", encoding="utf-8")
    with mock.patch("os.replace", side_effect=OSError("disk full")), pytest.raises(OSError):
        atomic_write_text(path, "new")
    assert path.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]
