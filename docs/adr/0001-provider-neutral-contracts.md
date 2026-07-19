# ADR-0001: Provider-neutral contracts and model profiles

- Status: Accepted for Phase 1
- Date: 2026-07-19
- Decision owners: Revio maintainers

## Context

Revio must add SCM and AI providers without leaking provider SDK types or forcing unsupported future methods onto adapters. AI behavior also varies by model, not merely provider.

## Decision

Use an extensible validated `ProviderId`, segregated structural protocols, adapter bundles assembled by registries, and separate AI provider capabilities from administrator-resolved model profiles. Model aliases are the only public selection mechanism. Phase 1 provides fake-only orchestration and contract tests.

## Consequences

Adapters implement only supported roles. Core workflows depend on small ports and normalized immutable models. Registries must validate duplicates and unknown identifiers. Model-dependent limits and features cannot be inferred from provider identity.

## Alternatives considered

- Closed provider enum: rejected because every adapter would require a core change.
- Monolithic provider interfaces: rejected because adapters would need unsupported stubs.
- Provider-wide model capabilities: rejected because models within a provider differ.

## Follow-up

Phase 2 will exercise the SCM read contracts with a GitHub adapter. Phase 4 will exercise AI review generation with Anthropic. Neither is implemented here.
