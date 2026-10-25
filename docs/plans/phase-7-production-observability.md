# Phase 7 — Production observability, hardening, and rollout

## Status

Draft for review. This document does not authorize implementation.

Phase 7 starts only after this plan is approved. It preserves every Phase 3 queue-durability,
Phase 4 provider/write-ambiguity, Phase 5 trusted-base-policy, and Phase 6 finding-lifecycle
guarantee. Phase 7 is the phase that may, under explicit administrator confirmation, remove the
blanket production-publishing prohibition; until then and absent that confirmation, production
publishing stays rejected at startup.

## 1. Goal

Make the single-node MVP operable and safely deployable: give operators structured logs,
low-cardinality metrics, bounded rate/resource limits, graceful shutdown, backup/restore and
rollout runbooks, and a fail-closed, explicitly-confirmed path to enable production review
publishing. Phase 7 completes the smallest production MVP (GitHub App + Anthropic, single node,
Docker Compose). It adds no new review behavior.

## 2. Decisions requiring approval

Implementation begins only after the following are accepted or replaced. These mirror roadmap §21
item 21 (production TLS, secret manager, logs, backups, retention, privacy policy).

### Decision 1 — Production publishing enablement

Proposed: the current unconditional production prohibition is replaced by an explicit, fail-closed
gate. In `production`, `REVIO_REVIEW_PUBLISH_ENABLED=true` is accepted only when **all** of the
following hold, otherwise startup fails:

- `REVIO_PRODUCTION_PUBLISH_CONFIRMED=true` (a dedicated, non-defaulted acknowledgement);
- a non-empty rollout allow-list (`REVIO_REVIEW_ROLLOUT_REPOSITORIES`);
- a valid marker-key source and GitHub publish/checks write capabilities;
- TLS and secret-source policy (Decisions 2–3) satisfied;
- observability required-configuration (Decision 4) satisfied.

The gate is additive and fail-closed: any missing precondition rejects startup. Non-production
environments are unaffected.

### Decision 2 — TLS and ingress exposure

Proposed: TLS is terminated by an operator-provided reverse proxy (documented), not in-process.
Revio binds loopback/private only; the webhook route additionally enforces the existing
signature/byte/media-type limits. `/metrics` is never exposed publicly (Decision 4).

### Decision 3 — Secret sources

Proposed: production requires file/secret-mount sources for the GitHub private key, Anthropic key,
webhook secret, and marker key (literal environment values are rejected in `production`). No durable
credential storage is introduced; tokens remain in-memory as today.

### Decision 4 — Metrics endpoint and protection

Proposed: a Prometheus text `/metrics` endpoint served only on a separate internal bind
(`REVIO_METRICS_BIND`, default disabled) or gated behind a required bearer token
(`REVIO_METRICS_TOKEN`), never on the public webhook listener. Metrics stay low cardinality
(Decision 5).

### Decision 5 — Log destination and format

Proposed: single-line JSON logs to stdout at `REVIO_LOG_LEVEL` (default `info`). Operators ship
stdout. No log files are written by the process.

### Decision 6 — Rate-limit and resource defaults

Proposed administrator defaults (all overridable, all fail-closed to bounded values): per-source
webhook ingress rate limit, per-provider outbound call concurrency/rate caps, a maximum concurrent
in-flight review, and request/body ceilings reaffirming existing limits.

### Decision 7 — Backup/restore and retention

Proposed: operator-run offline backup of the SQLite database + WAL checkpoint, documented in a
runbook; restore validated by a readiness check. Retention remains operator-run (Phase 3 terminal
history and Phase 6 closed-finding pruning); no automatic online deletion is added.

## 3. Frozen policy decisions

1. Phase 7 adds **no** new review, dedup, or lifecycle behavior; it is observability, hardening,
   rollout, and the production-enablement gate only.
2. Metrics are low cardinality. Repository, pull-request, SHA, installation, user, marker, finding,
   and thread identifiers never become metric labels. Allowed labels are bounded enums
   (outcome/kind/provider/alias) only.
3. Logs never contain secrets, tokens, authorization headers, raw diffs, source content, prompts,
   provider response bodies, raw YAML, marker keys, or unsafe exception text. Only stable codes,
   bounded identifiers, and durations are logged.
4. The production-publishing gate is fail-closed and additive; any missing precondition rejects
   startup. No environment downgrade can silently enable publishing.
5. Rate and resource limits are bounded and fail-closed; exceeding a limit sheds load safely
   (reject/delay) and is observable, never silently dropped.
6. Graceful shutdown drains the in-flight job within the lease/grace window, never abandons a lease
   mid-write, and relies on the existing durable reconciliation for any interrupted external write.
7. Backup, restore, and retention remain offline, operator-run, and bounded.

## 4. Scope

- Structured JSON logging with correlation/delivery/job/review-run/provider/outcome/error-class
  fields and strict redaction.
- A Prometheus exporter implementing the existing `QueueMetricsPort`, a protected `/metrics`
  endpoint, and a documented low-cardinality metric catalog covering Phase 3–6 outcomes.
- Ingress and outbound rate limits, bounded in-flight concurrency, and reaffirmed request/resource
  ceilings.
- Readiness-transition observability (ready↔not_ready) and graceful shutdown hardening for the API
  and worker.
- The fail-closed production-publishing enablement gate and production secret/TLS policy.
- Backup/restore guidance, a rollout runbook, and a production operability drill runbook.
- Configuration additions, contract/boundary tests, and sandbox operability validation.

## 5. Explicit non-goals

- No horizontal scaling, multiple workers, PostgreSQL, or multi-region recovery.
- No admin UI, hosted multi-tenancy, marketplace install flow, or billing.
- No second AI provider (Phase 8), GitLab/Bitbucket (Phase 9), or agent mode (Phase 10).
- No new review/finding behavior, no durable credential storage, no automatic online deletion.
- No cost/billing decisions; observed normalized usage only, as today.

## 6. Structured logging

- One JSON object per line to stdout; stable field names; bounded value sizes.
- Context fields: correlation id, provider id, delivery identity, job id, review-run id, call kind,
  outcome code, error class, and durations. Identifiers are bounded and non-secret; high-cardinality
  tenant identifiers may appear in logs (not metrics) but never secrets or content.
- A central redaction layer strips authorization headers, secret values, tokens, marker keys, raw
  diffs/source/prompts/response bodies, and replaces unsafe exception detail with stable codes.
- Emitted at the same boundaries that already produce metrics so log/metric semantics stay aligned;
  AI metrics/logs emit once per provider call, review metrics/logs once per review run.

## 7. Metrics

- A `PrometheusQueueMetrics` adapter implements `QueueMetricsPort(increment, gauge)` and renders the
  Prometheus text format; `InMemoryQueueMetrics` remains the default for tests and non-exporting runs.
- Documented catalog (low cardinality, bounded enum labels only): webhook validation/deduplication;
  queue depth/age/outcomes; review duration/outcomes; SCM and AI call duration/errors; findings
  routing; prior-finding selection/revalidation outcomes and usage categories; deduplication
  outcomes; thread discovery/resolution outcomes; finding-lifecycle state transitions; partial/
  fallback reviews; token categories; cache usage; provider/model-alias distribution.
- A cardinality guard rejects/clamps unknown or unbounded label values at the adapter boundary.
- `/metrics` is served only on the internal bind or behind the required token; it is never added to
  the public webhook listener and never reveals identifiers or content.

## 8. Security hardening

- Ingress rate limiting per source with bounded, fail-closed shedding before JSON parsing; reaffirm
  byte/media-type/signature checks.
- Outbound per-provider concurrency and rate caps, bounded in-flight review concurrency, and bounded
  provider response/pagination sizes (reaffirming existing limits).
- Redaction posture (§6) applied to all logs, metrics, errors, and readiness output.
- Existing SSRF/origin/redirect/least-privilege controls are retained and covered by regression.

## 9. Production enablement

- Replace the unconditional production prohibition with the Decision 1 fail-closed gate implemented
  in configuration validation, with explicit error messages per missing precondition.
- Rollout stays allow-list driven by stable repository identity; production adds no implicit repos.
- Startup readiness additionally verifies the production preconditions (secret sources, marker-key
  identity, observability configuration) and fails closed otherwise.

## 10. Graceful shutdown and readiness

- API and worker handle SIGTERM/SIGINT: stop accepting new work, finish or safely reschedule the
  in-flight job within the lease/grace window, release locks, and close provider clients.
- Readiness transitions (ready↔not_ready) are logged and counted; `/ready` continues to reflect
  migration currency, database writability, queue usability, required configuration, and Phase 6
  lifecycle integrity.

## 11. Backup, restore, retention

- Runbook: offline exclusive backup (maintenance lock + WAL checkpoint/truncate + copy), restore,
  and post-restore readiness verification.
- Retention remains operator-run: Phase 3 `prune-terminal` and Phase 6 `prune-findings`, unchanged,
  with dry-run/backup-confirmed discipline.

## 12. Configuration additions

Add fail-closed settings under the `REVIO_` prefix, e.g.:
`REVIO_LOG_LEVEL`, `REVIO_LOG_FORMAT=json`, `REVIO_METRICS_BIND`, `REVIO_METRICS_TOKEN`,
`REVIO_INGRESS_RATE_LIMIT`, `REVIO_PROVIDER_MAX_CONCURRENCY`, `REVIO_MAX_INFLIGHT_REVIEWS`,
`REVIO_PRODUCTION_PUBLISH_CONFIRMED`. Every combination is covered by configuration tests, including
all production gate permutations.

## 13. Test plan

- Log redaction: secrets/tokens/headers/content never appear; stable codes and bounded fields do.
- Metric semantics and cardinality: one AI metric per provider call, one review metric per run;
  identifiers never become labels; the cardinality guard clamps unknown labels; `/metrics` auth/bind.
- Rate and resource limits: ingress shedding, outbound caps, in-flight bound, and bounded response
  sizes behave deterministically and observably.
- Readiness transitions and graceful shutdown: in-flight job drains or reschedules without lease
  abandonment or duplicate external writes; locks released.
- Production gate: every permutation of the Decision 1 preconditions; fail-closed startup rejection
  with precise codes; non-production unaffected.
- Backup/restore: checkpoint/copy/restore round-trip followed by a passing readiness check.
- Full Phase 3 durability, Phase 4 ProviderCall/write-ambiguity, Phase 5 configuration, and Phase 6
  lifecycle regression suites remain green.
- Ruff format/check, strict Pyright, full pytest, pre-commit, diff checks, Docker build, isolated
  Compose readiness, and production publishing rejection without confirmation.

## 14. Manual / sandbox validation (operability drill)

Use only `sraj0210/revio-phase4-sandbox` and the dedicated App. With a fresh tunnel and isolated
Compose project:

1. Enable JSON logging and `/metrics`; confirm logs carry correlation/outcome fields and no secrets.
2. Scrape `/metrics`; confirm the catalog is present with only bounded enum labels and no identifiers.
3. Drill success, duplicate, retry, partial, dead-job, disabled-repo, and recovery outcomes; confirm
   metrics/logs reflect each.
4. Exercise ingress and outbound rate limits; confirm safe shedding and observability.
5. SIGTERM the worker mid-review; confirm graceful drain/reschedule and no duplicate external write.
6. Back up, restore, and confirm readiness.
7. Verify the production gate: `production` + publish without confirmation fails closed; with all
   preconditions it starts (validate in sandbox only; do not enable production writes against a
   production repo).
8. Search logs/metrics for secrets, content, prompts, provider bodies, and identifiers-as-labels;
   confirm absence.

## 15. Validation gates

Before merge: approved plan; Ruff format/check; strict Pyright; full pytest and pre-commit; diff
checks; Phase 3–6 regression suites; log-redaction, metric-cardinality, rate/resource-limit,
readiness-transition, graceful-shutdown, and production-gate matrices; Docker build; isolated Compose
readiness; production publishing rejected without confirmation; real sandbox operability drill.

## 16. Suggested implementation slices

1. Structured JSON logging + central redaction and context propagation.
2. Prometheus exporter, `/metrics` protection, and the low-cardinality catalog + cardinality guard.
3. Ingress/outbound rate limits, in-flight concurrency, and reaffirmed resource ceilings.
4. Graceful shutdown + readiness-transition observability for API and worker.
5. Production-enablement gate, production secret/TLS policy, and readiness preconditions.
6. Backup/restore, rollout, and operability-drill runbooks + docs/ADR.
7. Full regression, Docker/Compose, and real sandbox operability validation.

Every slice stays on one Phase 7 feature branch and one implementation pull request. Do not merge
until the complete validation matrix and the real sandbox operability drill pass.

## 17. Completion criteria

Phase 7 is complete only when:

- JSON logs carry correlation/outcome context and contain no secrets or content;
- the documented low-cardinality metric catalog is exported on a protected endpoint with no
  identifier labels;
- ingress/outbound rate and resource limits shed load safely and observably;
- graceful shutdown drains or safely reschedules in-flight work with no duplicate external writes;
- the production-publishing gate is fail-closed and starts only with every precondition satisfied;
- backup/restore and rollout runbooks are validated by a readiness drill;
- Phase 3–6 safety guarantees remain green;
- the real sandbox operability drill passes.

Do not begin Phase 8 without a separate explicit approval.
