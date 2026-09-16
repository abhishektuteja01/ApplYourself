import pandas as pd
import pytest

from src.discovery.schema import make_row, naive_datetime, validate_frame, COLUMNS

def test_make_row_defaults():
    row = make_row()
    assert row["site"] == ""
    assert row["date_posted"] is None
    assert row["is_remote"] is False
    assert row["job_url_direct"] == ""
    assert row["min_amount"] is None

def test_make_row_job_url_direct_fallback():
    row = make_row(job_url="http://example.com")
    assert row["job_url"] == "http://example.com"
    assert row["job_url_direct"] == "http://example.com"

    row2 = make_row(job_url="http://example.com", job_url_direct="http://direct.com")
    assert row2["job_url"] == "http://example.com"
    assert row2["job_url_direct"] == "http://direct.com"

def test_validate_frame_rejects_missing_column():
    df = pd.DataFrame([{"site": "linkedin"}])
    with pytest.raises(ValueError, match="Frame validation failed"):
        validate_frame(df)

def test_validate_frame_rejects_extra_column():
    row = make_row()
    row["extra_col"] = "test"
    row["ingested_run_id"] = "1"
    row["scraped_date"] = pd.Timestamp.now()
    df = pd.DataFrame([row])
    with pytest.raises(ValueError, match="Frame validation failed"):
        validate_frame(df)

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


def test_validate_frame_strips_timezone():
    row = make_row(date_posted="2026-08-01T10:00:00Z")
    row["ingested_run_id"] = "1"
    row["scraped_date"] = pd.Timestamp("2026-08-01")
    out = validate_frame(pd.DataFrame([row]))
    assert out["date_posted"].dtype == "datetime64[ns]"
    assert out["date_posted"].iloc[0] == pd.Timestamp("2026-08-01 10:00:00")


def test_validate_frame_normalizes_scraped_date():
    for value, expected in (
        ("2026-08-01", pd.Timestamp("2026-08-01")),
        ("2026-08-01T10:00:00-05:00", pd.Timestamp("2026-08-01 15:00:00")),
        (pd.Timestamp("2026-08-01T10:00:00Z"), pd.Timestamp("2026-08-01 10:00:00")),
        (pd.Timestamp("2026-08-01"), pd.Timestamp("2026-08-01")),
    ):
        row = make_row()
        row["ingested_run_id"] = "1"
        row["scraped_date"] = value
        out = validate_frame(pd.DataFrame([row]))
        assert out["scraped_date"].dtype == "datetime64[ns]"
        assert out["scraped_date"].iloc[0] == expected


def test_validate_frame_scraped_date_none_becomes_nat():
    row = make_row()
    row["ingested_run_id"] = "1"
    row["scraped_date"] = None
    out = validate_frame(pd.DataFrame([row]))
    assert out["scraped_date"].dtype == "datetime64[ns]"
    assert out["scraped_date"].isna().all()


def test_validate_frame_accepts_zero_row_frame():
    df = pd.DataFrame(columns=COLUMNS + ["ingested_run_id", "scraped_date"])
    out = validate_frame(df)
    assert out.empty
    assert out["scraped_date"].dtype == "datetime64[ns]"
    assert out["date_posted"].dtype == "datetime64[ns]"


def test_validate_frame_coerces_amounts():
    row = make_row(min_amount=None, max_amount=None)
    row["ingested_run_id"] = "1"
    row["scraped_date"] = pd.Timestamp.now()
    df = pd.DataFrame([row])

    validated = validate_frame(df)
    assert pd.isna(validated["min_amount"].iloc[0])
    assert pd.isna(validated["max_amount"].iloc[0])
    assert validated["min_amount"].dtype == float
    assert validated["max_amount"].dtype == float
