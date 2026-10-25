# ADR 0010: Production observability, hardening, and the publishing-enablement gate

Status: accepted for Phase 7.

Logs are single-line JSON to stdout with a bounded allowlisted context (correlation, provider,
delivery, job, review-run, call kind, outcome, error class, duration) and a defensive scrubber that
redacts bearer tokens, GitHub/Anthropic key shapes, api-key/secret/authorization assignments, and
PEM private-key blocks. Exceptions log only their class. Venaroq code does not log secrets or content;
the scrubber is defense in depth.

Metrics are low cardinality. A Prometheus exporter implements the existing queue-metrics port and is
served only on a separate internal bind (`VENAROQ_METRICS_BIND`, default disabled), never on the public
webhook listener. Metric names are sanitized, outcome labels are bounded and escaped, and a hard
series cap drops overflow into `venaroq_metrics_dropped_total`. Repository, pull-request, SHA,
installation, user, marker, finding, and thread identifiers are never labels.

Ingress is rate limited per source with a bounded, process-local fixed window enforced before body
reading and JSON parsing; over-limit requests are shed with 429 and logged, never dropped silently.
Outbound provider work remains bounded by the one-worker sequential model. Graceful shutdown drains
or safely reschedules the in-flight job within the heartbeat/lease window without abandoning a lease
or duplicating an external write; readiness transitions are logged and counted.

Production publishing is unlocked only by a fail-closed gate. In `production`,
`VENAROQ_REVIEW_PUBLISH_ENABLED=true` is accepted only when `VENAROQ_PRODUCTION_PUBLISH_CONFIRMED=true`,
the rollout allow-list is non-empty, and the marker key, GitHub private key, and Anthropic API key
all use file sources; any missing precondition rejects startup with a precise reason. Non-production
environments are unaffected. TLS is terminated by an operator-provided reverse proxy; Venaroq writes no
log files and introduces no durable credential storage. Backup/restore and retention remain offline,
operator-run, and bounded.
