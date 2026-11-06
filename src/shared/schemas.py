"""Schema versions for data files. Bump one when its file's shape changes."""

STATE_SCHEMA_VERSION = 1
EVENT_SCHEMA_VERSION = 1
APPLY_RUN_SCHEMA_VERSION = 1  # applications/apply_runs/<stem>.json, key `schema`
NIGHT_RUN_SCHEMA_VERSION = 1  # logs/pipeline_*/run.json, key `schema_version`
SCORED_SCHEMA_VERSION = 1  # jobs/scored.parquet, parquet metadata `schema_version`
JOB_IDS_SCHEMA_VERSION = 1  # jobs/job_ids.parquet, parquet metadata `schema_version`


def check(version: object, known: int, where: object) -> None:
    """A missing version (None) is 1; a version newer than this code knows is refused."""
    version = 1 if version is None else version
    if not isinstance(version, int) or version > known:
        raise ValueError(f"{where} has schema version {version!r}; this code knows up to {known}")
