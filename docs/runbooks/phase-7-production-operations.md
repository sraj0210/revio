# Phase 7 production operations

Single-node GitHub + Anthropic deployment: observability, limits, backup/restore, rollout, and the
fail-closed production-publishing enablement gate. TLS is terminated by an operator-provided reverse
proxy; Venaroq binds the webhook listener on the operator's chosen interface and serves metrics only on
a separate internal bind.

## Observability

- **Logs**: single-line JSON on stdout. `VENAROQ_LOG_LEVEL` (default `info`), `VENAROQ_LOG_FORMAT`
  (`json`/`plain`). Ship stdout; Venaroq writes no log files. Logs never contain secrets, tokens,
  content, prompts, provider bodies, or raw YAML.
- **Metrics**: Prometheus text at `GET /metrics` on `VENAROQ_METRICS_BIND:VENAROQ_METRICS_PORT` only
  (default disabled). Never expose it on the public webhook listener; scrape it from inside the
  deployment network. Labels are bounded enums; identifiers never appear. `venaroq_metrics_dropped_total`
  counts any series dropped by the cardinality cap.

## Limits

- **Ingress rate limit**: per-source fixed window, `VENAROQ_INGRESS_RATE_LIMIT` per
  `VENAROQ_INGRESS_RATE_WINDOW_SECONDS`. Over-limit requests receive 429 + Retry-After.
- Request body, diff, file, token, and provider response sizes retain their existing bounds.
- Outbound provider work is bounded by the single sequential worker; the queue owns retry/backoff.

## Graceful shutdown

SIGTERM/SIGINT stop accepting new work and drain the in-flight job within
`VENAROQ_QUEUE_SHUTDOWN_TIMEOUT_SECONDS` while heartbeating its lease; an unfinished job is left for
safe re-lease. No lease is abandoned mid-write and no external write is duplicated (durable
reconciliation covers any interrupted write). Compose sets a `stop_grace_period` above the shutdown
timeout.

## Backup and restore

Run offline under the exclusive maintenance lock:

1. Quiesce writers (stop the worker, or run during a maintenance window).
2. Checkpoint and copy: `venaroq-db` acquires the maintenance lock; perform
   `PRAGMA wal_checkpoint(TRUNCATE)` then copy `venaroq.db` (and verify no `-wal`/`-shm` remain).
3. Restore by placing the backup at `VENAROQ_DATABASE_PATH` with `0600`/`0700` permissions on the
   file and its parent, then confirm `venaroq-db check` exits 0 and `/ready` reports ready.

Retention stays operator-run and bounded: `venaroq-db prune-terminal` and `venaroq-db prune-findings`
(both `--dry-run` / `--execute --backup-confirmed`).

## Rollout

Rollout is allow-list driven by stable repository identity
(`VENAROQ_REVIEW_ROLLOUT_REPOSITORIES`, comma-separated `provider:installation:repository`). Repos are
opt-in; production adds no implicit repositories. Repository opt-out is honored only when
`VENAROQ_REVIEW_ALLOW_REPOSITORY_OPTOUT=true`.

## Production-publishing enablement (fail-closed)

In `production`, `VENAROQ_REVIEW_PUBLISH_ENABLED=true` starts only when ALL hold; otherwise startup
fails with a precise error:

- `VENAROQ_PRODUCTION_PUBLISH_CONFIRMED=true`;
- a non-empty `VENAROQ_REVIEW_ROLLOUT_REPOSITORIES`;
- file sources for the marker key (`VENAROQ_PUBLISH_MARKER_KEY_FILE`), the GitHub private key
  (`VENAROQ_GITHUB_PRIVATE_KEY_FILE`), and the Anthropic API key (`VENAROQ_ANTHROPIC_API_KEY_FILE`);
- the GitHub publish/checks write capabilities and marker-key readiness check.

Marker-key rotation with unresolved publish operations remains unsupported; rotate only after all
operations under the old key are resolved.

## Operability drill

With the sandbox App and an isolated Compose project:

1. Enable JSON logs and metrics; confirm logs carry correlation/outcome fields and no secrets, and
   `/metrics` exposes the bounded catalog with no identifier labels.
2. Drill success, duplicate, retry, partial, dead-job, disabled-repo, and recovery outcomes.
3. Exercise the ingress rate limit (429 shedding) and confirm it is observable.
4. SIGTERM the worker mid-review; confirm graceful drain/reschedule and no duplicate external write.
5. Back up, restore, and confirm `/ready`.
6. Verify the production gate: `production` + publish without confirmation fails closed; with every
   precondition it starts (validate in sandbox only).
7. Search logs/metrics for secrets, content, prompts, provider bodies, and identifiers-as-labels;
   confirm absence.
