# ADR-0002: Native GitHub App authentication

- Status: Accepted for Phase 2
- Date: 2026-07-19

## Context

Revio needs installation-scoped, least-privilege GitHub reads without personal credentials.

## Decision

Generate short-lived RS256 App JWTs, exchange them for opaque installation tokens, and cache tokens in memory. Refresh margin and minimum usable lifetime are separate. Per-installation locks retain stable identity across invalidation. Idempotent reads retry authentication once after a 401.

## Consequences

No PAT path exists. Tokens are process-local and a restart clears cache state. Horizontal token coordination is deferred.
