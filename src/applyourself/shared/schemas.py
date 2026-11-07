"""Schema versions for data files. Bump one when its file's shape changes."""

STATE_SCHEMA_VERSION = 1
EVENT_SCHEMA_VERSION = 1
APPLY_RUN_SCHEMA_VERSION = 1  # applications/apply_runs/<stem>.json, key `schema`
NIGHT_RUN_SCHEMA_VERSION = 1  # logs/pipeline_*/run.json, key `schema_version`
SCORED_SCHEMA_VERSION = 1  # jobs/scored.parquet, parquet metadata `schema_version`
JOB_IDS_SCHEMA_VERSION = 1  # jobs/job_ids.parquet, parquet metadata `schema_version`
STORE_SCHEMA_VERSION = 1  # jobs/jobs.parquet, parquet metadata `schema_version`
BOARD_STATE_SCHEMA_VERSION = 1  # jobs/boards.parquet, parquet metadata `schema_version`
CLEAN_SCHEMA_VERSION = 1  # jobs/clean.parquet, parquet metadata `schema_version`
MAYBE_DUPES_SCHEMA_VERSION = 1  # jobs/maybe_duplicates.jsonl, key `schema_version` per line
DISCOVERY_RUN_SCHEMA_VERSION = 1  # jobs/runs/<run_id>.json, key `schema_version`
APPLY_TRIAGE_SCHEMA_VERSION = 1  # jobs/apply_triage.json, key `schema_version`
SYNONYM_PROPOSALS_SCHEMA_VERSION = 1  # jobs/synonyms_proposals.yaml, key `schema_version`
SYNONYM_OPEN_SCHEMA_VERSION = 1  # jobs/synonyms_open.yaml, key `schema_version`
SYNONYM_LOG_SCHEMA_VERSION = 1  # jobs/synonyms_log.jsonl, key `schema_version` per line
SYNONYM_KEYWORDS_SCHEMA_VERSION = 1  # `synonyms keywords` output (jobs/synonyms_keywords.json)
SCORE_STAGING_SCHEMA_VERSION = (
    2  # jobs/scored.staging/run.json, key `schema_version`; 1: the old judge
)
LEDGER_SCHEMA_VERSION = 1  # applications/apply_runs/submitted.jsonl, key `schema_version` per line
PLAN_SCHEMA_VERSION = 1  # `apply plan --json` output (applications/<dir>/plan.json)
SESSION_YAML_SCHEMA_VERSION = 1  # applications/<dir>/ session YAMLs (session_schema.KINDS)
SETTINGS_SCHEMA_VERSION = 1  # profile/settings.yaml, key `schema_version`
CONTACTS_SCHEMA_VERSION = 1  # profile/contacts.yaml, key `schema_version`
RESUME_SCHEMA_VERSION = 1  # profile/resume.yaml, key `schema_version`


def check(version: object, known: int, where: object) -> None:
    """A missing version (None) is 1; a version newer than this code knows is refused."""
    version = 1 if version is None else version
    if not isinstance(version, int) or version > known:
        raise ValueError(f"{where} has schema version {version!r}; this code knows up to {known}")
