"""SQLite schema and invariants through Phase 4."""

SCHEMA_REVISION = "0002_phase4_review_lifecycle"
ACTIVE_STATES = "'pending', 'running', 'retry_wait'"
TERMINAL_STATES = "'completed', 'dead', 'cancelled', 'superseded'"

PHASE3_SCHEMA_SQL = f"""
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
    CHECK(attempt_count <= max_attempts),
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
CREATE INDEX IF NOT EXISTS ix_queue_jobs_pending_created
ON queue_jobs(created_at) WHERE state = 'pending';
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
    UNIQUE(job_id, attempt_number),
    CHECK(outcome IS NULL OR outcome IN (
        'retry', 'completed', 'dead', 'cancelled', 'superseded', 'lease_expired'
    )),
    CHECK((finished_at IS NULL AND outcome IS NULL AND error_class IS NULL
           AND error_message IS NULL AND retry_delay_seconds IS NULL)
       OR (finished_at IS NOT NULL AND outcome IS NOT NULL))
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

PHASE4_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS review_runs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES queue_jobs(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK(state IN (
        'generation_pending', 'generation_attempted', 'artifact_durable', 'publishing',
        'completed', 'partial', 'superseded', 'publication_indeterminate',
        'check_run_indeterminate'
    )),
    validated_head_sha TEXT NOT NULL,
    validated_base_sha TEXT NOT NULL,
    provider_check_run_id TEXT,
    check_run_external_id TEXT NOT NULL UNIQUE,
    terminal_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_calls (
    id TEXT PRIMARY KEY,
    review_run_id TEXT NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
    call_kind TEXT NOT NULL CHECK(call_kind IN ('initial', 'repair')),
    call_ordinal INTEGER NOT NULL CHECK(call_ordinal > 0),
    provider_id TEXT NOT NULL,
    model_profile_id TEXT NOT NULL,
    model_profile_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'reserved', 'attempt_started', 'response_observed', 'ambiguous',
        'known_rejected', 'completed'
    )),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(review_run_id, call_kind, call_ordinal)
);

CREATE TABLE IF NOT EXISTS provider_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_call_id TEXT NOT NULL UNIQUE REFERENCES provider_calls(id) ON DELETE CASCADE,
    usage_status TEXT NOT NULL CHECK(usage_status IN ('known', 'unknown')),
    uncached_input_tokens INTEGER CHECK(uncached_input_tokens >= 0),
    cached_input_tokens INTEGER CHECK(cached_input_tokens >= 0),
    cache_creation_tokens INTEGER CHECK(cache_creation_tokens >= 0),
    output_tokens INTEGER CHECK(output_tokens >= 0),
    observed_at TEXT NOT NULL,
    CHECK((usage_status = 'known' AND uncached_input_tokens IS NOT NULL
           AND cached_input_tokens IS NOT NULL AND cache_creation_tokens IS NOT NULL
           AND output_tokens IS NOT NULL)
       OR (usage_status = 'unknown' AND uncached_input_tokens IS NULL
           AND cached_input_tokens IS NULL AND cache_creation_tokens IS NULL
           AND output_tokens IS NULL))
);

CREATE TABLE IF NOT EXISTS review_artifacts (
    review_run_id TEXT PRIMARY KEY REFERENCES review_runs(id) ON DELETE CASCADE,
    artifact_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model_profile_id TEXT NOT NULL,
    model_profile_version TEXT NOT NULL,
    summary TEXT NOT NULL CHECK(length(summary) BETWEEN 1 AND 8000),
    findings_json TEXT NOT NULL,
    partial INTEGER NOT NULL CHECK(partial IN (0, 1)),
    reason_codes_json TEXT NOT NULL,
    artifact_digest TEXT NOT NULL CHECK(length(artifact_digest) = 64),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS check_run_operations (
    id TEXT PRIMARY KEY,
    review_run_id TEXT NOT NULL UNIQUE REFERENCES review_runs(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL UNIQUE,
    head_sha TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'reserved_unattempted', 'attempt_started', 'known_rejected', 'ambiguous',
        'reconciled', 'completed', 'integrity_failed'
    )),
    provider_check_run_id TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    terminal_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publish_operations (
    id TEXT PRIMARY KEY,
    review_run_id TEXT NOT NULL UNIQUE REFERENCES review_runs(id) ON DELETE CASCADE,
    operation_key TEXT NOT NULL UNIQUE,
    marker TEXT NOT NULL UNIQUE,
    marker_key_id TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'reserved_unattempted', 'attempt_started', 'known_rejected', 'ambiguous',
        'reconciled', 'completed', 'integrity_failed'
    )),
    provider_review_id TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    terminal_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_provider_calls_run ON provider_calls(review_run_id, call_kind);
CREATE INDEX IF NOT EXISTS ix_review_runs_state ON review_runs(state, updated_at);
CREATE INDEX IF NOT EXISTS ix_publish_operations_state ON publish_operations(state, updated_at);
CREATE INDEX IF NOT EXISTS ix_check_run_operations_state ON check_run_operations(state, updated_at);
"""

SCHEMA_SQL = PHASE3_SCHEMA_SQL + PHASE4_SCHEMA_SQL

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
