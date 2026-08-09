# ADR 0005: SQLite durable queue and leasing

Status: accepted for Phase 3.

Revio uses one local SQLite database in WAL mode with `synchronous=FULL`, foreign keys, one API process, and one worker. SQLite 3.35.0 is a non-configurable code capability because leasing uses atomic `UPDATE ... RETURNING`; there is no select/update fallback.

Ingress and leases use `BEGIN IMMEDIATE`. Physical delivery insertion and active semantic-job insertion use explicit `ON CONFLICT ... DO NOTHING RETURNING`, not caught uniqueness exceptions. Active capacity includes pending, running, and retry-wait jobs. Every terminal transition sets `terminal_at` and clears its lease transactionally.

Busy handling defaults to a 500 ms SQLite wait, three adapter attempts, and a two-second total monotonic budget. Remaining budget is recalculated before every attempt, and database contention never consumes a queue job attempt.

API and worker hold a shared `flock` on `revio.maintenance.lock`; migration and retention require an exclusive lock. This is supported only for the Linux container/local-filesystem deployment. Network filesystems and multiple workers are unsupported.

The worker additionally holds an exclusive process-lifetime `revio.worker.lock`.
Database and lock paths must be non-symlink files in a runtime-owned, non-group/world-
writable directory. macOS is supported for local development and tests only; Linux
with a local container volume is the deployment target.

SQLite constraints enforce attempt bounds, terminal timestamps, lease presence, and
attempt completion consistency. Lease ownership and legal transition edges remain
application-enforced through compare-and-set updates because they depend on the
current worker and attempt identity.
