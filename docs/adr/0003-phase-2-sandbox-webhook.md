# ADR-0003: Non-durable sandbox webhook boundary

- Status: Accepted for Phase 2
- Date: 2026-07-19

## Decision

The signed GitHub webhook route is independently controlled by `REVIO_GITHUB_SANDBOX_WEBHOOK_ENABLED`. It returns 404 when disabled and startup fails if enabled in production. Responses expose only generic accepted/ignored status.

## Consequences

Phase 2 is suitable only for local/sandbox validation. Accepted events can be lost. Production ingress remains blocked until Phase 3.
