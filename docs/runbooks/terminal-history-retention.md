# Terminal-history retention

Retention is operator-run and never automatic. It uses `terminal_at`, never `updated_at`.

1. Stop API and worker.
2. Checkpoint WAL and make a verified backup.
3. Run `revio-db prune-terminal --dry-run` and review bounded counts.
4. Run `revio-db prune-terminal --execute --backup-confirmed`.
5. Run `revio-db check`, then restart API and worker.

Only terminal jobs, their attempts, and eligible linked deliveries older than `REVIO_RETENTION_TERMINAL_AGE_DAYS` are removed in bounded batches. Permanent identity/hash tombstones are inserted in the same transaction first. Active jobs, active-linked deliveries, installation states, and unrelated ignored deliveries are never deleted.
