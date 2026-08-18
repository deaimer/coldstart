"""SQLite schema and connection management for the ColdStart results store.

The store lives at ``.coldstart/results.db`` by default. It holds normalized,
provenance-tracked evaluation data: raw artifact *paths* and *SHA-256 hashes*
are recorded, never full trajectory contents.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(".coldstart/results.db")

# Bumped whenever the schema changes shape. Stored in schema_meta so future
# migrations can detect and upgrade older databases.
SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS benchmark_versions (
    id      INTEGER PRIMARY KEY,
    version TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS tasks (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS task_versions (
    id      INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    version TEXT,
    digest  TEXT NOT NULL,
    path    TEXT,
    UNIQUE (task_id, digest)
);

CREATE TABLE IF NOT EXISTS models (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    provider TEXT,
    UNIQUE (name, provider)
);

CREATE TABLE IF NOT EXISTS agents (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    version TEXT,
    UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS systems (
    id               INTEGER PRIMARY KEY,
    system_key       TEXT NOT NULL UNIQUE,
    model_id         INTEGER REFERENCES models(id),
    agent_id         INTEGER REFERENCES agents(id),
    agent_kwargs_json TEXT
);

-- One row per Harbor job directory (an "evaluation run").
CREATE TABLE IF NOT EXISTS runs (
    id                   INTEGER PRIMARY KEY,
    run_key              TEXT NOT NULL UNIQUE,
    job_uuid             TEXT,
    benchmark_version_id INTEGER REFERENCES benchmark_versions(id),
    started_at           TEXT,
    finished_at          TEXT,
    n_total_trials       INTEGER,
    n_completed_trials   INTEGER,
    n_errored_trials     INTEGER,
    config_json          TEXT,
    source_path          TEXT NOT NULL,
    source_sha256        TEXT,
    ingested_at          TEXT NOT NULL
);

-- One row per trial directory inside a run.
CREATE TABLE IF NOT EXISTS trials (
    id                 INTEGER PRIMARY KEY,
    run_id             INTEGER NOT NULL REFERENCES runs(id),
    trial_key          TEXT NOT NULL UNIQUE,
    trial_uuid         TEXT,
    trial_name         TEXT NOT NULL,
    task_version_id    INTEGER REFERENCES task_versions(id),
    system_id          INTEGER REFERENCES systems(id),
    started_at         TEXT,
    finished_at         TEXT,
    runtime_sec        REAL,
    runtime_basis      TEXT,
    coldstart_pass     REAL,
    strict_pass        INTEGER,
    exception_type       TEXT,
    exception_message    TEXT,
    exception_traceback  TEXT,
    exception_occurred_at TEXT,
    is_infra_exception INTEGER NOT NULL DEFAULT 0,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    cached_tokens      INTEGER,
    cost_usd           REAL,
    agent_result_json  TEXT,
    config_json        TEXT,
    source_path        TEXT NOT NULL,
    source_sha256      TEXT,
    ingested_at        TEXT NOT NULL,
    UNIQUE (run_id, trial_name)
);

-- Diagnostic ColdStart dimension rewards (functional, durability,
-- state_safety, integrity, evidence, coldstart_pass, and any future/unknown
-- reward keys). Strict pass MUST be read from trials.strict_pass /
-- trials.coldstart_pass, never averaged from this table, so that dimension
-- rewards can never contribute partial credit to the strict pass rate.
CREATE TABLE IF NOT EXISTS dimension_scores (
    id        INTEGER PRIMARY KEY,
    trial_id  INTEGER NOT NULL REFERENCES trials(id),
    dimension TEXT NOT NULL,
    value     REAL,
    UNIQUE (trial_id, dimension)
);

CREATE TABLE IF NOT EXISTS verifier_checks (
    id         INTEGER PRIMARY KEY,
    trial_id   INTEGER NOT NULL REFERENCES trials(id),
    check_name TEXT NOT NULL,
    passed     INTEGER,
    raw_json   TEXT,
    UNIQUE (trial_id, check_name)
);

-- Provenance for every raw artifact we touched: path + hash only, never
-- full contents (trajectories in particular are summarized into
-- metadata_json, not copied in).
CREATE TABLE IF NOT EXISTS artifact_references (
    id            INTEGER PRIMARY KEY,
    trial_id      INTEGER REFERENCES trials(id),
    run_id        INTEGER REFERENCES runs(id),
    kind          TEXT NOT NULL,
    source_path   TEXT NOT NULL,
    sha256        TEXT,
    size_bytes    INTEGER,
    metadata_json TEXT,
    UNIQUE (kind, source_path)
);

CREATE INDEX IF NOT EXISTS idx_trials_run ON trials(run_id);
CREATE INDEX IF NOT EXISTS idx_trials_system ON trials(system_id);
CREATE INDEX IF NOT EXISTS idx_trials_task_version ON trials(task_version_id);
CREATE INDEX IF NOT EXISTS idx_dimension_scores_trial ON dimension_scores(trial_id);
CREATE INDEX IF NOT EXISTS idx_verifier_checks_trial ON verifier_checks(trial_id);
CREATE INDEX IF NOT EXISTS idx_artifact_refs_trial ON artifact_references(trial_id);
CREATE INDEX IF NOT EXISTS idx_artifact_refs_run ON artifact_references(run_id);
"""


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the results database with the schema applied."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn
