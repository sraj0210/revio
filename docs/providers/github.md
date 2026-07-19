# GitHub provider — Phase 2

## Permissions

- Pull requests: Read
- Contents: Read
- Metadata: Read/implicit

No write permissions are used. Revio has no Phase 2 operation for reviews, comments, statuses, Check Runs, commits, or repository settings.

## Authentication

Configure a GitHub App ID and exactly one PEM source. Personal access tokens are unsupported. Installation tokens are cached only in process memory.

Application bootstrap constructs and registers the read-only adapter only when `REVIO_GITHUB_ENABLED=true`; it validates the RSA key before startup completes. The sandbox CLI reuses the same adapter-private composition path.

## Partial data

Changed-file and tree collections carry a completeness value: `complete`, `provider_truncated`, `service_page_limit`, or `service_item_limit`. Each changed file separately reports `complete`, `missing`, `malformed`, `provider_truncated`, or `binary_or_no_textual_patch` patch state. Non-complete values must never be treated as a full provider result.

## Safety warning

The sandbox webhook is non-durable and must never be enabled in production. A 202 response does not indicate durable storage. Phase 3 is required before production webhook ingestion.
