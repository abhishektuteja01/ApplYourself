import pandas as pd
import pytest

from src.discovery.schema import make_row, validate_frame, COLUMNS

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
