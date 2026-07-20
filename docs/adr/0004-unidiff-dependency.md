# ADR-0004: Unified-diff parser dependency

- Status: Accepted for Phase 2 with monitoring
- Date: 2026-07-19

## Context

Phase 2 needs conservative unified-diff parsing without maintaining a custom parser. `unidiff` 0.7.5 is MIT-licensed and compatible with Revio's Apache-2.0 license, but its latest PyPI release dates to March 2023.

## Decision

Use the locked and hashed `unidiff` 0.7.5 package for Phase 2. Treat malformed input as an explicit non-complete patch state rather than deriving anchors. Do not describe the package as actively maintained.

## Consequences

The dependency age is an accepted Phase 2 risk. Reassess maintenance, Python compatibility, and alternatives before production review execution expands beyond the sandbox read integration.
