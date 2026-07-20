"""Phase 3 SQLite schema and invariants."""

SCHEMA_REVISION = "0001_phase3_durable_queue"
ACTIVE_STATES = "'pending', 'running', 'retry_wait'"
TERMINAL_STATES = "'completed', 'dead', 'cancelled', 'superseded'"

SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    delivery_identity TEXT NOT NULL,
    event_name TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    disposition TEXT NOT NULL,
    ignored_reason TEXT,
    event_schema_version INTEGER,
    normalized_event_json TEXT,
    semantic_identity TEXT,
    linked_job_id TEXT,
    received_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(provider_id, delivery_identity),
    FOREIGN KEY(linked_job_id) REFERENCES queue_jobs(id)
);

CREATE TABLE IF NOT EXISTS queue_jobs (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    job_type TEXT NOT NULL,
    semantic_identity TEXT NOT NULL,
    event_schema_version INTEGER NOT NULL DEFAULT 1,
    event_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ({ACTIVE_STATES}, {TERMINAL_STATES})),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
    available_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    first_started_at TEXT,
    last_started_at TEXT,
    terminal_reason TEXT,
    safe_error_class TEXT,
    safe_error_message TEXT,
    observed_head_sha TEXT,
    observed_base_sha TEXT,
    terminal_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK((state IN ({TERMINAL_STATES}) AND terminal_at IS NOT NULL)
       OR (state IN ({ACTIVE_STATES}) AND terminal_at IS NULL)),
    CHECK((state = 'running' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
       OR (state <> 'running' AND lease_owner IS NULL AND lease_expires_at IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_queue_jobs_active_semantic
ON queue_jobs(provider_id, job_type, semantic_identity)
WHERE state IN ({ACTIVE_STATES});
CREATE INDEX IF NOT EXISTS ix_queue_jobs_lease
ON queue_jobs(state, available_at, lease_expires_at, created_at);
CREATE INDEX IF NOT EXISTS ix_queue_jobs_terminal_at
ON queue_jobs(terminal_at) WHERE terminal_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_queue_jobs_expired
ON queue_jobs(state, lease_expires_at);
CREATE INDEX IF NOT EXISTS ix_queue_jobs_semantic_history
ON queue_jobs(provider_id, semantic_identity, created_at);
CREATE INDEX IF NOT EXISTS ix_webhook_deliveries_received
ON webhook_deliveries(received_at);
CREATE INDEX IF NOT EXISTS ix_webhook_deliveries_semantic
ON webhook_deliveries(provider_id, semantic_identity);

CREATE TABLE IF NOT EXISTS job_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES queue_jobs(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL,
    worker_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    error_class TEXT,
    error_message TEXT,
    retry_delay_seconds REAL,
    UNIQUE(job_id, attempt_number)
);

CREATE TABLE IF NOT EXISTS installation_states (
    provider_id TEXT NOT NULL,
    installation_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('active', 'suspended', 'deleted')),
    source_delivery_id TEXT NOT NULL REFERENCES webhook_deliveries(id),
    provider_updated_at TEXT,
    ordering_delivery_identity TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(provider_id, installation_id)
);

CREATE TABLE IF NOT EXISTS webhook_delivery_tombstones (
    provider_id TEXT NOT NULL,
    delivery_identity TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    retained_at TEXT NOT NULL,
    PRIMARY KEY(provider_id, delivery_identity)
);

CREATE TABLE IF NOT EXISTS alembic_version (
    version_num VARCHAR(32) NOT NULL PRIMARY KEY
);
"""

ACTIVE_JOB_INSERT_SQL = f"""
INSERT INTO queue_jobs (
    id, provider_id, job_type, semantic_identity,
    event_schema_version, event_json, state, attempt_count, max_attempts,
    available_at, created_at, updated_at
) VALUES (?, ?, ?, ?, 1, ?, 'pending', 0, ?, ?, ?, ?)
ON CONFLICT(provider_id, job_type, semantic_identity)
WHERE state IN ({ACTIVE_STATES})
DO NOTHING RETURNING id
"""

LEASE_SQL = """
UPDATE queue_jobs
SET state = 'running', lease_owner = ?, lease_expires_at = ?,
    available_at = ?, attempt_count = attempt_count + 1,
    first_started_at = COALESCE(first_started_at, ?), last_started_at = ?, updated_at = ?
WHERE id = (
    SELECT id FROM queue_jobs
    WHERE ((state IN ('pending', 'retry_wait') AND available_at <= ?)
       OR (state = 'running' AND lease_expires_at <= ?))
      AND attempt_count < max_attempts
    ORDER BY available_at, created_at, id LIMIT 1
)
RETURNING *
"""
