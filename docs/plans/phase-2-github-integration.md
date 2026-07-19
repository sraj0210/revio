# Phase 2 Plan — GitHub Webhook and GitHub App Integration

Status: Proposed for review
Implementation status: Not started
Scope: Phase 2 only
Safety classification: Local/sandbox only and non-durable

## 1. Repository baseline

Phase 1 is merged at `1cb57744` and provides:

- Immutable provider-neutral domain models
- Extensible `ProviderId`
- Segregated SCM read, repository-content, publishing, status, and thread protocols
- Separate AI review and conversation protocols
- Optional capabilities through `SCMAdapterBundle`
- Provider/model registries and `ResolvedModelProfile`
- Fake orchestration and contract-test foundation
- A boundary test preventing provider SDK imports in the core
- CI for Ruff, Pyright, pytest, and container health

Baseline validation passed: uv lock, Ruff formatting/linting, Pyright strict mode, and 11 tests.

## 2. Objective

Add a read-only GitHub adapter and a local/sandbox webhook endpoint that validate Phase 1 abstractions against a real SCM provider.

```text
Signed GitHub webhook
  -> sandbox-only FastAPI endpoint
  -> signature/header validation
  -> private GitHub payload DTO
  -> provider-neutral ReviewEvent
  -> optional read-only GitHub API operations
```

Phase 2 does not durably accept work. Atomic delivery and job persistence begin in Phase 3.

## 3. Exact scope

- GitHub App service configuration
- RS256 GitHub App JWT generation
- Installation-token exchange and in-memory caching
- Expiry margin and per-installation refresh locking
- Local/sandbox webhook endpoint
- Raw-body, media-type, signature, delivery, and event validation
- Installation lifecycle event normalization
- Pull request `opened`, `reopened`, and `synchronize` normalization
- Provider-neutral `ReviewEvent` and related identities
- Current pull-request metadata retrieval
- Changed-file and patch retrieval
- File retrieval at an explicit ref
- Repository tree retrieval at an explicit ref
- A sandbox-only read-validation CLI independent of webhook delivery
- Pagination, truncation, rate-limit, and typed error behavior
- Sanitized fixtures, errors, logs, provider docs, and contract tests

Prefer a narrow `httpx` implementation over a GitHub SDK so GitHub DTOs, headers, pagination, and API-version behavior remain explicit and adapter-private.

## 4. Expected files

```text
src/revio/
├── cli/
│   ├── __init__.py
│   └── github_sandbox.py
├── api/
│   ├── dependencies.py
│   └── routes/github_webhook.py
├── application/webhook/
│   ├── service.py
│   └── result.py
├── config/
│   ├── service.py
│   └── github.py
├── domain/
│   ├── events.py
│   └── models.py
├── ports/webhook.py
└── adapters/scm/github/
    ├── adapter.py
    ├── auth.py
    ├── client.py
    ├── errors.py
    ├── pagination.py
    ├── patch_parser.py
    ├── redaction.py
    ├── dto/
    │   ├── app.py
    │   ├── contents.py
    │   ├── pull_request.py
    │   └── webhook.py
    └── webhook/
        ├── normalizer.py
        └── signature.py
tests/
├── contracts/
│   ├── scm_read_contract.py
│   └── repository_content_contract.py
├── github/
├── integration/test_github_sandbox_flow.py
└── fixtures/github/
docs/
├── adr/0002-github-app-authentication.md
├── adr/0003-phase-2-sandbox-webhook.md
├── providers/github.md
└── runbooks/github-sandbox-validation.md
```

No database migrations or persistence adapters are included.

Expose the CLI as a `pyproject.toml` script such as `revio-github-sandbox`. It calls the same read adapter as the application and does not call webhook code.

## 5. Provider-neutral events

Add a minimal normalized model:

```python
class ReviewEvent(BaseModel):
    provider_id: ProviderId
    delivery_identity: DeliveryIdentity
    semantic_identity: SemanticIdentity
    event_type: ReviewEventType
    trigger: ReviewTrigger
    installation: InstallationRef
    repository: RepositoryRef | None
    change_request: ChangeRequestTarget | None
    event_head_sha: str | None
    event_base_sha: str | None
```

Supporting types:

- `DeliveryIdentity`
- `SemanticIdentity`
- `ReviewEventType`
- `ReviewTrigger`
- `WebhookNormalizationResult`
- `WebhookDisposition`: `supported` or `ignored`
- `IgnoredWebhookReason`

The domain receives no GitHub DTO, SDK type, URL template, or arbitrary payload dictionary.

Delivery identity:

```text
github:<X-GitHub-Delivery>
```

Pull-request semantic identity:

```text
github:<installation-id>:<repository-id>:pull-request:<number>:<head-sha>:<trigger>
```

Delivery identity represents physical redelivery. Semantic identity represents logical review intent. Phase 3 will persist and enforce both.

## 6. GitHub App authentication

### Private-key loading

Support exactly one administrator-controlled source:

- `REVIO_GITHUB_PRIVATE_KEY`: PEM text
- `REVIO_GITHUB_PRIVATE_KEY_FILE`: absolute secret-mount path

Rules:

- Reject zero or two configured sources.
- Reject relative paths and oversized files.
- Validate an RSA private key at startup.
- Never log PEM content, a key hash, or fragments in parse errors.
- Keep key material process-local and never write temporary files.

### App JWT

Use `PyJWT[crypto]` or a similarly narrow dependency.

- Algorithm: RS256
- Issuer: configured GitHub App ID/client ID
- Issued-at: injected current time with bounded skew allowance
- Expiry: short-lived and within GitHub's allowed maximum
- Inject clock for deterministic tests

### Installation-token exchange

Use the GitHub App JWT for:

```text
POST /app/installations/{installation_id}/access_tokens
```

Accept provider token strings as opaque values. Never assume a fixed length or prefix.

### Token cache

Cache key:

```text
(ProviderId, installation external ID)
```

Cache entry:

- Redacted secret token wrapper
- Provider expiry timestamp
- Refresh-after timestamp

Behavior:

- Treat refresh margin and minimum usable lifetime as separate settings.
- Refresh when remaining lifetime enters the configured refresh margin.
- Never return an expired token.
- Maintain one refresh lock per installation.
- Protect lock-map creation against races.
- Recheck cache after acquiring the lock.
- Allow different installations to refresh concurrently.
- After refresh failure, reuse a cached token only when its remaining lifetime is strictly longer than the configured minimum usable lifetime.
- On `401` from an idempotent GitHub read, invalidate the cached token, refresh, and retry exactly once. Mark the request context as authentication-retried so a second `401` becomes a typed authentication failure rather than another refresh loop.
- Clear local token and lock entries on installation suspension/deletion where applicable.
- Never persist installation tokens in Phase 2.
- Provide no personal access-token fallback.

Typed failures include private-key, JWT, installation-not-found, suspension, authentication, and permission errors. Messages must be safe for logs.

## 7. Configuration

```text
REVIO_ENVIRONMENT=local|sandbox|production
REVIO_GITHUB_ENABLED=true
REVIO_GITHUB_SANDBOX_WEBHOOK_ENABLED=false
REVIO_GITHUB_APP_ID=<numeric id>
REVIO_GITHUB_PRIVATE_KEY=<secret PEM>
REVIO_GITHUB_PRIVATE_KEY_FILE=/run/secrets/github-app.pem
REVIO_GITHUB_WEBHOOK_SECRET=<secret>
REVIO_GITHUB_API_URL=https://api.github.com
REVIO_GITHUB_API_VERSION=<administrator-pinned version>
REVIO_GITHUB_HTTP_TIMEOUT_SECONDS=10
REVIO_GITHUB_TOKEN_REFRESH_MARGIN_SECONDS=60
REVIO_GITHUB_TOKEN_MINIMUM_USABLE_LIFETIME_SECONDS=30
REVIO_GITHUB_WEBHOOK_MAX_BYTES=1048576
```

Rules:

- Repository content cannot set these values.
- GitHub.com is the only Phase 2 API host.
- Secret values use Pydantic secret types and redacted representations.
- `REVIO_GITHUB_ENABLED` controls the read adapter and GitHub App authentication independently of webhook ingress.
- `REVIO_GITHUB_SANDBOX_WEBHOOK_ENABLED` controls only the non-durable route.
- Startup must fail when the sandbox webhook is enabled in `production`.
- Production may enable the read adapter while leaving the sandbox webhook disabled.
- `.env.example` contains names and safe placeholders only.

## 8. Local/sandbox webhook endpoint

Route:

```text
POST /webhooks/github
```

Processing order:

1. Return `404` when sandbox ingress is disabled.
2. Accept `application/json` with syntactically valid media-type parameters, including charset; reject other media types.
3. Read the raw body through a bounded streaming reader.
4. Return `413` as soon as the configured limit is exceeded.
5. Require `X-Hub-Signature-256`.
6. Calculate HMAC-SHA256 over the exact raw bytes.
7. Compare the full `sha256=<hex>` value with `hmac.compare_digest`.
8. Require, length-bound, character-validate, and normalize `X-GitHub-Delivery` before logging.
9. Require, length-bound, character-validate, and normalize `X-GitHub-Event` before logging.
10. Decode a private, event-specific GitHub DTO.
11. Normalize a supported action or return an explicit no-op.
12. Return only a generic accepted/ignored response, never normalized event data.
13. Return without review or durable processing.

Responses:

| Condition | Status |
|---|---:|
| Supported valid event | 202 |
| Valid unsupported event/action | 202 |
| Missing/invalid signature | 401 |
| Missing/invalid delivery or event header | 400 |
| Unsupported media type | 415 |
| Malformed JSON or required fields | 400 |
| Oversized body | 413 |
| Route disabled | 404 |

Responses and logs never expose normalized payload data, expected signatures, secrets, raw payloads, installation tokens, or stack traces.

## 9. Webhook normalization

Support `installation` actions:

- `created`
- `deleted`
- `suspend`
- `unsuspend`

Installation events remain non-durable. `deleted` and `suspend` may evict process-local installation-token cache entries and their locks. `created` and `unsuspend` require no provider-side action. No installation state is persisted. Other valid installation actions are documented no-ops.

Support `pull_request` actions:

- `opened`
- `reopened`
- `synchronize`

Required private DTO fields:

- Installation ID
- Repository numeric ID and owner/name
- Pull request number, state, and draft status
- Base SHA/ref
- Head SHA/ref
- Sanitized sender identity only if required

DTOs should validate required fields while ignoring unrelated GitHub fields. Do not reproduce GitHub's full payload schema.

Unknown event names and unsupported actions with valid signatures return `202` and an ignored disposition. Safe logs contain event name, action, delivery ID, and reason only.

## 10. Read-only SCM adapter

Implement only:

- `ChangeRequestReadPort`
- `RepositoryContentReadPort`

Do not implement publishing, status, or thread ports as failing stubs.

Phase 2 capabilities:

- `repository_file_access = true`
- `tree_access = true`
- `webhook_event_uuids = true`
- `installation_authentication = true`
- All publishing, status, and thread capabilities remain false

### Current pull request

Fetch the current PR when read operations begin. Normalize target, title/body, state, draft flag, current base SHA, and current head SHA. Do not treat webhook SHAs as current provider state.

### Changed files and patches

Use the pull-request files endpoint with bounded pagination and `per_page=100`.

Normalize:

- Old/new paths
- Added, modified, deleted, and renamed states
- Additions/deletions
- Patch availability and truncation
- Binary state where detectable
- Parsed `DiffLine` sides and line numbers

Use a maintained unified-diff parser after dependency/license review rather than an ad hoc parser. Missing patches and GitHub's file-count ceiling must create explicit partial/truncated results. A patch is never treated as a complete final file.

### Explicit-ref files

Use the contents endpoint with an explicit SHA/ref.

- Require the ref argument.
- Encode paths safely.
- Never substitute the default branch.
- Decode Base64 strictly.
- Enforce decoded-size limits.
- Return `None` only for genuine not-found.
- Distinguish unsupported object type from genuine file not found, and classify permission, size, decoding, object-type, and transport failures separately.

### Trees

Phase 2 includes tree access because the existing `RepositoryContentReadPort` includes `get_tree`. Add a normalized `RepositoryEntry` model and replace the placeholder `list[str]` return type.

- Require an explicit ref and resolve it to a tree SHA before tree retrieval.
- Use non-recursive tree retrieval by default.
- Apply hard request/page and item limits to ref resolution and tree traversal.
- Any bounded recursive behavior must be an explicit later extension, not the default.
- Surface GitHub and service truncation distinctly.

## 11. HTTP, pagination, and errors

Use one injected `httpx.AsyncClient` with:

- Fixed GitHub.com HTTPS base URL
- Connect/read/write/pool timeouts
- GitHub JSON Accept header
- Pinned API-version header
- Safe Revio user agent
- Per-request installation authorization
- No cross-host authorization redirects

Pagination must enforce same-origin links, maximum pages, and maximum items. It must not loop on malformed links.

Provider-neutral errors:

- `SCMAuthenticationError`
- `SCMPermissionError`
- `SCMNotFoundError`
- `SCMRateLimitedError`
- `SCMValidationError`
- `SCMTransportError`
- `SCMProviderError`
- `SCMResponseFormatError`

GitHub HTTP interpretation remains inside the adapter. Inspect status, `Retry-After`, and rate-limit headers. Return retry metadata; do not sleep for long periods or implement queue retry before Phase 3.

## 12. GitHub App permissions

Minimum Phase 2 repository permissions:

- Pull requests: Read
- Contents: Read
- Metadata: Read/implicit

Subscribe to pull-request and installation events.

Do not request Checks write, statuses write, review-comment write, contents write, administration, actions, or deployments. Phase 4 publishing permissions require separate approval.

## 13. Test plan

### Authentication

- Valid/invalid RSA PEM sources and mutual exclusion
- Correct RS256 JWT claims and deterministic clock/skew
- Installation-token exchange uses App JWT
- Valid cache hit without HTTP
- Expiry and proactive refresh
- Refresh begins at the refresh-margin boundary
- Refresh failure reuses cache only above minimum usable lifetime
- Expired token never returned
- Concurrent same-installation requests perform one refresh
- Different installations refresh concurrently
- A `401` on an idempotent read invalidates, refreshes, and retries exactly once
- A second `401` does not trigger another authentication retry
- Arbitrary provider token lengths accepted
- No PAT configuration/path
- Key, token, and authorization redaction

### Webhook security

- Valid and modified-body signatures
- Wrong/missing/malformed signatures
- SHA-1-only signature rejected
- Constant-time comparison path
- Missing/invalid delivery and event headers
- `application/json` with valid parameters accepted
- Invalid parameters and non-JSON media types rejected
- Event/delivery headers length- and character-validated before logging
- Malformed JSON
- Exact-limit and oversized streamed bodies
- Sanitized error responses
- Responses contain generic accepted/ignored status only, never normalized payloads

### Normalization

- Installation lifecycle actions
- Deleted/suspend evict local tokens and locks
- Created/unsuspend perform no provider-side action
- PR opened, reopened, synchronize
- Composite identities and base/head mapping
- Stable delivery and semantic identities
- New head changes semantic identity
- Unsupported action/event no-op
- Missing required fields
- Extra GitHub fields do not enter the domain

### Read adapter

- Current PR and current base/head retrieval
- Changed-file pagination
- Rename/add/modify/delete/binary mapping
- Unified-diff line parsing
- Missing/truncated patch handling
- Provider file-count limit produces partial state
- Explicit-ref file retrieval and safe path encoding
- Strict Base64 and size limits
- Unsupported object type distinguished from genuine not-found
- Explicit ref resolved to tree SHA
- Non-recursive tree retrieval by default, with hard request/item limits and truncation
- Same-origin pagination and page/item ceilings

### Errors and boundaries

- 401, 403, 404, 422, 429, 500, and 503 classification
- Primary/secondary rate limits and Retry-After
- Connect/read/timeouts
- Malformed provider JSON
- Secret/header redaction
- Read-port contract suites
- Capabilities match implemented ports
- No unsupported stub methods
- No GitHub imports/types in domain, application, or ports

Use `respx` for all normal CI HTTP tests. Live GitHub calls are opt-in only.

### Sandbox validation CLI

- Reject execution unless environment is local/sandbox.
- Require installation ID, repository owner/name, and PR number.
- Inspect current PR metadata and current base/head SHAs.
- List bounded changed-file metadata.
- Optionally inspect metadata for a repository path at an explicit ref.
- Resolve an explicit ref and retrieve a bounded, non-recursive tree.
- Sanitize errors and output identifiers.
- Print file metadata only by default; never print source contents unless a separately approved future flag is introduced.
- Perform no webhook, comment, review, status, Check Run, or repository-write operation.

## 14. Manual validation

1. Create a private development GitHub App.
2. Grant Pull requests read and Contents read only.
3. Subscribe to installation and pull-request events.
4. Install only on a dedicated sandbox repository.
5. Enable the GitHub read adapter and leave the sandbox webhook disabled.
6. Run the validation CLI with installation ID, repository, and PR number.
7. Confirm current PR metadata, base/head SHAs, bounded changed files, explicit-ref file metadata, and a bounded non-recursive tree without source contents.
8. Enable the sandbox webhook separately and start Revio in local/sandbox environment.
9. Send an exact signed synthetic fixture to `/webhooks/github`.
10. Confirm processing and test signature rejection after changing one byte; responses must not contain normalized data.
11. Confirm unsupported actions return generic accepted/ignored results.
12. Optionally use an ephemeral forwarding tunnel limited to the webhook route.
13. Open a sandbox PR with changed, renamed, deleted, and binary files.
14. Confirm no comments, reviews, statuses, Check Runs, commits, or settings are modified.
15. Confirm the disabled route returns `404` and production startup fails only when sandbox webhook enablement is true.
16. Revoke/uninstall the sandbox App and confirm safe authentication failure.

Do not install the sandbox App broadly or on sensitive repositories.

## 15. Security controls

- Exact raw-byte HMAC-SHA256 validation
- Constant-time comparison
- Strict body, media, and header limits
- Header validation before logging
- Event/action allowlists
- Private minimal GitHub DTOs
- No raw payload logging
- Redacted secrets and authorization
- Startup RSA-key validation
- No PAT authentication
- Separate refresh-margin/minimum-usable-lifetime controls
- One-time `401` refresh retry for idempotent reads only
- Fixed HTTPS API endpoint
- Explicit timeouts and same-origin pagination
- Bounded pages, files, trees, patches, and decoded content
- Explicit-ref-to-tree-SHA resolution and non-recursive tree default
- Typed, sanitized errors
- Read-only permissions
- Independent adapter/webhook enablement and startup guard prohibiting sandbox webhook ingress in production
- Read-only CLI with metadata-only default output

## 16. Safety boundary and documentation

README, provider documentation, and local deployment documentation must display:

> Phase 2 webhook ingestion is non-durable and intended only for local or sandbox validation. A 202 response does not mean the event has been durably stored. Production deployment is prohibited until Phase 3 implements atomic delivery and queue-job persistence.

Docker Compose remains development-only and gains no production profile.

## 17. Known limitations

- Accepted events are lost on process failure.
- No durable delivery deduplication or installation state
- In-memory tokens and locks are single-process
- A process restart clears all token cache and eviction state
- No stale-job handling because there are no jobs
- GitHub file and tree provider limits
- Missing/truncated patches may produce partial results
- GitHub.com only; no Enterprise Server
- No reviews, comments, statuses, or Check Runs
- No production readiness claim
- The read adapter may run in production configuration, but Phase 2 does not claim the wider service is production-ready

## 18. Explicitly out of scope

- SQLite/PostgreSQL persistence
- Queue, worker, retries, or stale-job supersession
- AI providers, prompts, or review generation
- Publishing, comments, statuses, Check Runs, or threads
- `.revio.yml` loading
- Diff hashes, fingerprints, or finding lifecycle
- Reviewer-request triggers
- GitLab, Bitbucket, or GitHub Enterprise Server
- Production deployment

## 19. Completion criteria

- Dedicated Phase 2 branch from merged main
- GitHub DTOs isolated inside the adapter
- Deterministic JWT/token-cache tests pass
- Refresh margin, minimum usable lifetime, one-time `401` retry, and loop prevention are tested
- No PAT path
- GitHub adapter and sandbox webhook can be enabled independently
- Enabling the sandbox webhook in production fails startup; a disabled route returns `404`
- Sandbox endpoint enforces all security validation
- Installation and supported PR actions normalize correctly
- Valid unsupported events/actions are documented no-ops
- Only required read ports are implemented
- Current PR, SHAs, diffs, files, and trees normalize correctly
- Tree refs resolve to tree SHAs and retrieval is non-recursive with hard limits by default
- Unsupported file object types are not reported as missing files
- Sandbox CLI validates the read adapter independently and hides source content by default
- Pagination, truncation, limits, errors, and redaction are tested
- GitHub passes read-port contract suites
- Boundary tests prove no GitHub leakage into core
- Production use is prominently prohibited
- Ruff, Pyright, pytest, pre-commit, and container checks pass
- Optional live validation proves read-only behavior
- Focused commits, pushed branch, unmerged PR
- No Phase 3 code

## 20. Risks and rollback

| Risk | Mitigation |
|---|---|
| Key/token leakage | Secret wrappers, redaction tests, no raw logging |
| Duplicate refresh | Per-installation lock and cache recheck |
| Stale token | Separate refresh margin and minimum usable lifetime |
| Authentication retry loop | Retry one idempotent read once and mark retry context |
| Forged webhook | Exact raw-body HMAC-SHA256 |
| Resource abuse | Streaming body and result ceilings |
| DTO leakage | Adapter directories and import tests |
| Partial diff treated complete | Explicit partial/truncated metadata |
| Pagination abuse | Same-origin and page/item limits |
| Accidental writes | Read-only permissions and no write ports |
| CLI leaks source or secrets | Metadata-only default and sanitized output |
| Premature webhook production use | Independent enablement, startup failure, and warnings |

Rollback:

1. Set `REVIO_GITHUB_SANDBOX_WEBHOOK_ENABLED=false`; the route then returns `404` while the read adapter may remain enabled.
2. Rotate the webhook secret.
3. Revoke the App private key if necessary.
4. Disable `REVIO_GITHUB_ENABLED` if the read adapter must also be rolled back.
5. Suspend/uninstall the sandbox App, which also invalidates provider credentials; local cache is process-only.
6. Stop/restart the process to clear all local token and lock state.
7. Revert the Phase 2 PR.

There is no data migration or provider-side review cleanup because Phase 2 has no durable state or write behavior.

## 21. Eventual implementation workflow

1. Fast-forward local main to merged Phase 1.
2. Create `feat/phase-2-github-read-integration`.
3. Implement Phase 2 only.
4. Add tests and documentation with focused changes.
5. Run lock, Ruff, Pyright, pytest, pre-commit, and Docker validation.
6. Perform optional sandbox validation.
7. Push and open a Pull Request against main.
8. Do not merge automatically.
9. Do not begin Phase 3.
