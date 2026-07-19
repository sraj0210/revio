# GitHub provider — Phase 2

## Permissions

- Pull requests: Read
- Contents: Read
- Metadata: Read/implicit

No write permissions are used. Revio has no Phase 2 operation for reviews, comments, statuses, Check Runs, commits, or repository settings.

## Authentication

Configure a GitHub App ID and exactly one PEM source. Personal access tokens are unsupported. Installation tokens are cached only in process memory.

## Safety warning

The sandbox webhook is non-durable and must never be enabled in production. A 202 response does not indicate durable storage. Phase 3 is required before production webhook ingestion.
