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

Changed-file and tree collections carry a completeness value: `complete`, `provider_truncated`, `service_page_limit`, or `service_item_limit`. Exact duplicate changed-file entries are deduplicated, conflicting duplicates fail safely, and completeness uses the unique validated file count. Each changed file separately reports `complete`, `missing`, `malformed`, `provider_truncated`, or `no_textual_patch_unknown_reason` patch state. GitHub's changed-files response does not authoritatively distinguish binary files from other reasons a textual patch is absent, so Revio does not infer either binary content or provider truncation from change counts alone. Non-complete and non-`complete` patch values must never be treated as a full textual provider result.

## Safety warning

The sandbox webhook is non-durable and must never be enabled in production. A 202 response does not indicate durable storage. Phase 3 is required before production webhook ingestion.
