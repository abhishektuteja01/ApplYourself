import pandas as pd
import datetime

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
        
    df["date_posted"] = pd.to_datetime(df["date_posted"], errors="coerce")
    return df
