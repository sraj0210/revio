# ADR 0006: Durable webhook ingress and lifecycle authority

Status: accepted for Phase 3.

`REVIO_GITHUB_WEBHOOK_MODE` is `disabled`, `sandbox`, or `durable`. Durable mode verifies the exact raw-body signature, stores only its SHA-256 and a bounded normalized event, then returns `202` only after its SQLite transaction commits. Same delivery/hash is idempotent; reused identity with a different hash is a sanitized `409`; new work beyond active capacity rolls back and returns `503`.

Installation lifecycle state is stored atomically with its delivery. The worker checks this durable state before a GitHub read and invalidates its own cached token when suspended or deleted. Serialized committed delivery arrival is the Phase 3 authority for lifecycle state. Provider `updated_at` is retained only as informational evidence because GitHub does not document it as a trusted lifecycle sequence. A delayed provider delivery can therefore temporarily regress state; exact delivery identity remains idempotent.

The worker fetches only current pull-request metadata. Closed work is cancelled, a changed head is superseded, and a current head completes. It does not fetch diffs, source, files, or trees and performs no GitHub repository write.
