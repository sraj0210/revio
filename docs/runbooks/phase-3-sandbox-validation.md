# Phase 3 sandbox validation

Use an absolute disposable database path. Apply migrations, configure the read-only GitHub App and webhook secret, and set `REVIO_GITHUB_WEBHOOK_MODE=durable`.

Run `revio-worker check-ready` before worker startup, then validate `/health`,
`/ready`, and `revio-worker health` after startup. Send a signed pull-request webhook
and confirm one delivery/job. Redeliver the exact bytes and confirm no new rows; reuse
the delivery identity with changed bytes and confirm sanitized `409`. Suspend the
installation before processing and confirm no GitHub read. Exercise worker restart
after lease expiry and run retention dry-run/execution only after stopping both
runtime processes and taking a backup.

Inspect the database to confirm it contains no raw webhook body, credentials, source, diff, prompt, or provider response. Confirm GitHub has no comments, reviews, statuses, Check Runs, commits, or settings changes.
