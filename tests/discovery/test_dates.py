"""The four posting-date coercions, co-located in `src/discovery/dates.py`.

`iso_date` and `ms_date` were covered only through their board sources; the
board-specific shapes they reject are asserted directly here.
"""
from datetime import date

import pandas as pd
import pytest

from src.discovery.dates import (
    iso_date,
    ms_date,
    naive_datetime,
    relative_posted_date,
)


class TestIsoDate:
    def test_z_suffixed_timestamp(self):
        assert iso_date("2026-08-01T10:00:00Z") == date(2026, 8, 1)

    def test_offset_timestamp(self):
        assert iso_date("2026-08-01T10:00:00-05:00") == date(2026, 8, 1)

    def test_a_plain_date(self):
        assert iso_date("2026-08-01") == date(2026, 8, 1)

    @pytest.mark.parametrize("bad", ["", "not a date", "2026-13-40", "08/01/2026"])
    def test_garbage_is_none(self, bad):
        assert iso_date(bad) is None

    @pytest.mark.parametrize("bad", [None, 1754006400000, 1.5, date(2026, 8, 1)])
    def test_a_non_string_is_none(self, bad):
        assert iso_date(bad) is None


class TestMsDate:
    def test_epoch_milliseconds(self):
        assert ms_date(0) == date.fromtimestamp(0)
        assert ms_date(1754043000000) == date.fromtimestamp(1754043000)

    def test_a_bool_is_rejected(self):
        """True is an int in Python, and 1ms after the epoch is not a date
        anyone meant."""
        assert ms_date(True) is None
        assert ms_date(False) is None

    def test_out_of_range_is_none(self):
        assert ms_date(10 ** 30) is None

    @pytest.mark.parametrize("bad", ["1754043000000", None, "", date(2026, 8, 1)])
    def test_a_non_number_is_none(self, bad):
        assert ms_date(bad) is None


class TestRelativePostedDate:
    def test_today(self):
        assert relative_posted_date("Posted Today", today=date(2026, 7, 10)) == date(2026, 7, 10)

    def test_yesterday(self):
        assert relative_posted_date("Posted Yesterday", today=date(2026, 7, 10)) == date(2026, 7, 9)

    def test_n_days_ago(self):
        assert relative_posted_date("Posted 19 Days Ago", today=date(2026, 7, 10)) == date(2026, 6, 21)

    def test_n_plus_days_ago(self):
        assert relative_posted_date("Posted 30+ Days Ago", today=date(2026, 7, 10)) == date(2026, 6, 10)

    def test_unrecognized_text_is_none(self):
        assert relative_posted_date("Some other phrasing") is None

    def test_missing_is_none(self):
        assert relative_posted_date(None) is None
        assert relative_posted_date("") is None


def test_naive_datetime_keeps_naive_values_when_column_is_mixed():
    # object dtype, one tz-aware value among naive ones
    s = pd.Series(
        [pd.Timestamp("2026-08-01T10:00:00Z"),
         pd.Timestamp("2026-08-04"),
         pd.Timestamp("2026-01-01")],
        dtype=object,
    )
    out = naive_datetime(s)
    assert out.dtype == "datetime64[ns]"
    assert not out.isna().any()
    assert list(out) == [pd.Timestamp("2026-08-01 10:00:00"),
                         pd.Timestamp("2026-08-04"),
                         pd.Timestamp("2026-01-01")]


def test_naive_datetime_handles_mixed_offset_strings():
    out = naive_datetime(pd.Series(["2026-08-01T10:00:00+00:00",
                                    "2026-08-01T10:00:00-05:00",
                                    "2026-08-04"]))
    assert out.dtype == "datetime64[ns]"
    # normalized to UTC, so the -05:00 value shifts forward
    assert list(out) == [pd.Timestamp("2026-08-01 10:00:00"),
                         pd.Timestamp("2026-08-01 15:00:00"),
                         pd.Timestamp("2026-08-04")]


def test_naive_datetime_coerces_garbage_and_empty():
    out = naive_datetime(pd.Series(["not a date", None, ""]))
    assert out.dtype == "datetime64[ns]"
    assert out.isna().all()
    assert naive_datetime(pd.Series([], dtype=object)).dtype == "datetime64[ns]"
