"""Every canonical path, resolved from this file rather than the CWD, so a path
means the same thing whichever entry point you came in through.

Code lives at the fixed `REPO_ROOT`. Data and config live under `root()`, which is
`REPO_ROOT` unless `use_root()` repoints it. Read `paths.X` inside a function, never
copy it into a module-level name: each read resolves from the current root.
"""

from pathlib import Path
from typing import TYPE_CHECKING

REPO_ROOT = Path(__file__).resolve().parents[3]

_root = REPO_ROOT
_overrides: dict[str, Path] = {}  # one name pointed elsewhere; tests set it via setitem

_LAYOUT = {
    "JOBS": "jobs",
    "STORE": "jobs/jobs.parquet",
    "BOARD_STATE": "jobs/boards.parquet",
    "CLEAN": "jobs/clean.parquet",
    "MAYBE_DUPES": "jobs/maybe_duplicates.jsonl",
    "FEED_FAILED_READS": "jobs/feed_failed_reads.json",
    "JOB_IDS": "jobs/job_ids.parquet",
    "SCORED": "jobs/scored.parquet",
    "STAGING": "jobs/scored.staging",
    "JOBS_RAW": "jobs/raw",
    "JOBS_RUNS": "jobs/runs",
    "APPLY_TRIAGE": "jobs/apply_triage.json",
    "APPLY_URLS": "jobs/apply_urls.json",
    "SYNONYM_PROPOSALS": "jobs/synonyms_proposals.yaml",
    "SYNONYM_LOG": "jobs/synonyms_log.jsonl",
    "SYNONYM_OPEN": "jobs/synonyms_open.yaml",
    "PIPELINE": "pipeline",
    "PROFILE": "profile",
    "RESUME": "profile/resume.yaml",
    "BULLETS": "profile/bullets.md",
    "SKILLS": "profile/skills_master.md",
    "STORIES": "profile/stories.md",
    "SETTINGS": "profile/settings.yaml",
    "CONTACTS": "profile/contacts.yaml",
    "SCORING": "profile/scoring.yaml",
    "DE_AI_RULES": "profile/de_ai_rules.yaml",
    "SCORING_DEFAULTS": "profile/scoring_defaults.yaml",
    "ANSWER_BANK": "profile/application_answers.yaml",
    "DISCOVERY_CONFIG": "profile/discovery.yaml",
    "VERTICALS_CONFIG": "profile/verticals.yaml",
    "VERTICALS_DIR": "profile/verticals",
    "SYNONYM_DECLINED": "profile/.state/synonyms_declined.yaml",
    "SYNONYM_HISTORY": "profile/.state/synonym_history",
    "APPLICATIONS": "applications",
    "APPLY_RUNS": "applications/apply_runs",
    "SHORTLIST": "shortlist",
    "INBOX": "inbox",
    "LOGS": "logs",
    "SUMMARY": "summary.md",
    "PIPELINE_MD": "pipeline.md",
    "UNIVERSE": "data/universe",
    "PDF_STAGING": ".pdf_staging",
    "APPLY_BROWSER_DIR": ".apply_profile",
}

JOBS: Path
STORE: Path
BOARD_STATE: Path
CLEAN: Path
MAYBE_DUPES: Path
FEED_FAILED_READS: Path
JOB_IDS: Path
SCORED: Path
STAGING: Path
JOBS_RAW: Path
JOBS_RUNS: Path
APPLY_TRIAGE: Path
APPLY_URLS: Path
SYNONYM_PROPOSALS: Path
SYNONYM_LOG: Path
SYNONYM_OPEN: Path
PIPELINE: Path
PROFILE: Path
RESUME: Path
BULLETS: Path
SKILLS: Path
STORIES: Path
SETTINGS: Path
CONTACTS: Path
SCORING: Path
DE_AI_RULES: Path
SCORING_DEFAULTS: Path
ANSWER_BANK: Path
DISCOVERY_CONFIG: Path
VERTICALS_CONFIG: Path
VERTICALS_DIR: Path
SYNONYM_DECLINED: Path
SYNONYM_HISTORY: Path
APPLICATIONS: Path
APPLY_RUNS: Path
SHORTLIST: Path
INBOX: Path
LOGS: Path
SUMMARY: Path
PIPELINE_MD: Path
UNIVERSE: Path
PDF_STAGING: Path
APPLY_BROWSER_DIR: Path


def root() -> Path:
    return _root


def use_root(path: Path) -> Path:
    """Point every data path at `path`; returns the previous root."""
    global _root
    previous, _root = _root, Path(path)
    return previous


def example(name: str) -> Path:
    """A committed `profile/` template. Shipped with the code, so it never moves."""
    return REPO_ROOT / "profile" / name


def default(name: str) -> Path:
    """A committed default under `data/defaults/`, used when profile/ has no copy."""
    return REPO_ROOT / "data" / "defaults" / name


if not TYPE_CHECKING:

    def __getattr__(name: str) -> Path:
        if name in _overrides:
            return _overrides[name]
        if name in _LAYOUT:
            return _root / _LAYOUT[name]
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
