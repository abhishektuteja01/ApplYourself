from __future__ import annotations

import pandas as pd

COLUMNS: list[str] = [
    "site",
    "company",
    "title",
    "location",
    "job_url",
    "job_url_direct",
    "description",
    "date_posted",
    "is_remote",
    "min_amount",
    "max_amount",
    "currency",
    "job_type",
    "job_level",
    "vertical",
]

def naive_datetime(values) -> pd.Series:
    """Parse to tz-naive UTC. Both keywords are load-bearing on a column that
    concatenated shards have left as object dtype: without utc=True a single
    tz-aware value coerces every naive one to NaT, and without format="mixed"
    the format inferred from the first element does the same to every element
    that doesn't share it."""
    return pd.to_datetime(
        values, errors="coerce", utc=True, format="mixed"
    ).dt.tz_localize(None)


def make_row(**kwargs) -> dict:
    row = {
        "site": "",
        "company": "",
        "title": "",
        "location": "",
        "job_url": "",
        "job_url_direct": "",
        "description": "",
        "date_posted": None,
        "is_remote": False,
        "min_amount": None,
        "max_amount": None,
        "currency": "",
        "job_type": "",
        "job_level": "",
        "vertical": "",
    }
    
    for k, v in kwargs.items():
        if k in row:
            row[k] = v
            
    if "job_url_direct" not in kwargs or not kwargs["job_url_direct"]:
        row["job_url_direct"] = row["job_url"]
        
    return row

def validate_frame(df: pd.DataFrame) -> pd.DataFrame:
    required_cols = set(COLUMNS + ["ingested_run_id", "scraped_date"])
    actual_cols = set(df.columns)
    
    if required_cols != actual_cols:
        raise ValueError(
            f"Frame validation failed. "
            f"Missing: {required_cols - actual_cols}, "
            f"Extra: {actual_cols - required_cols}"
        )
        
    df = df.copy()
    
    for col in ("min_amount", "max_amount"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
        
    # shards must be naive before they are concatenated
    df["date_posted"] = naive_datetime(df["date_posted"])
    return df
