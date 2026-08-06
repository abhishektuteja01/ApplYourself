"""Every canonical path, resolved from this file rather than the CWD, so a path
means the same thing whichever entry point you came in through.

Consumers keep their own module-level names bound to these values, because the
tests redirect those names and the slash commands read them.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

JOBS = REPO_ROOT / "jobs"
CLEAN = JOBS / "clean.parquet"
SCORED = JOBS / "scored.parquet"
STAGING = JOBS / "scored.staging"
JOBS_RAW = JOBS / "raw"
JOBS_RUNS = JOBS / "runs"

PIPELINE = REPO_ROOT / "pipeline"
PROFILE = REPO_ROOT / "profile"
APPLICATIONS = REPO_ROOT / "applications"
SHORTLIST = REPO_ROOT / "shortlist"
INBOX = REPO_ROOT / "inbox"
