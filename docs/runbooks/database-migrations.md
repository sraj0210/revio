# Database migrations

Phase 3 requires SQLite 3.35.0 or newer and an absolute local-filesystem database path.

1. Stop API and worker and take a database backup.
2. Confirm no shared runtime lock remains.
3. Run `REVIO_DATABASE_PATH=/absolute/path/revio.db revio-db upgrade`.
4. Run `revio-db current` and `revio-db check` with the same environment.
5. Before starting the worker, run `revio-worker check-ready`; it must acquire the
   worker-instance lock and then release it.
6. Start API and worker; confirm `/ready` and `revio-worker health` succeed. A later
   `check-ready` correctly fails while the running worker owns the instance lock.

Migration never runs automatically in API or worker startup. A maintenance lock failure means a runtime process is still active; do not bypass it. Preserve the database and prefer a forward repair after failure.

Phase 3 downgrade is intentionally unsupported because dropping the durable tables
would destroy accepted deliveries and queue history. Restore a verified backup or use
a reviewed forward migration instead.
