# GitHub provider — Phase 4

## Permissions

- Pull requests: Read and write only when sandbox publication is enabled
- Checks: Read and write only when sandbox publication is enabled
- Contents: Read
- Metadata: Read/implicit

With `REVIO_REVIEW_PUBLISH_ENABLED=false`, the adapter remains read-only. Phase 4 sandbox publishing
creates or updates one non-blocking Check Run and one `COMMENT` review pinned to the validated head.
Production startup rejects publishing.

## Authentication

Configure a GitHub App ID and exactly one PEM source. Personal access tokens are unsupported. Installation tokens are cached only in process memory.

Application bootstrap constructs and registers the adapter only when `REVIO_GITHUB_ENABLED=true`; it
validates the RSA key before startup completes. Write capabilities are declared only when the
separate publication gate is enabled. The sandbox CLI reuses the same adapter-private composition
path.

## Write reconciliation

Check Run `external_id` and the HMAC review marker are exact reconciliation identities, not uniqueness
constraints. A complete zero-match search authorizes only an unattempted first POST. After an
ambiguous write, Revio reconciles and never reposts from zero matches. Only an explicit non-creating
anchor rejection permits one same-head summary-only fallback.

## Partial data

Changed-file and tree collections carry a completeness value: `complete`, `provider_truncated`, `service_page_limit`, or `service_item_limit`. Exact duplicate changed-file entries are deduplicated, conflicting duplicates fail safely, and completeness uses the unique validated file count. Each changed file separately reports `complete`, `missing`, `malformed`, `provider_truncated`, or `no_textual_patch_unknown_reason` patch state. GitHub's changed-files response does not authoritatively distinguish binary files from other reasons a textual patch is absent, so Revio does not infer either binary content or provider truncation from change counts alone. Non-complete and non-`complete` patch values must never be treated as a full textual provider result.

## Safety warning

The sandbox webhook is non-durable and must never be enabled in production. A 202 response does not indicate durable storage. Phase 3 is required before production webhook ingestion.
