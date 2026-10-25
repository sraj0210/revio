# Phase 9 — GitLab adapter and multi-SCM ingress

## Status

Draft for review. This document does not authorize implementation.

Phase 9 starts only after this plan is approved. It preserves every Phase 3 queue-durability,
Phase 4 provider/write-ambiguity, Phase 5 trusted-base-policy, Phase 6 finding-lifecycle, and
Phase 7 observability/production-gate guarantee. It is provider-additive: it must not change GitHub
behavior.

## 1. Goal

Prove the provider-neutral SCM boundary by adding GitLab (GitLab.com and approved self-managed
instances) as a second source-control adapter, and generalize the ingress/composition so any
registered SCM provider can be wired in without touching the core. Merge requests use the same
core review workflow — diff review, repository configuration, finding lifecycle, and ambiguity-safe
publication — with no GitLab conditionals in the core.

## 2. Decisions requiring approval

### Decision 1 — GitLab authentication model

Proposed: an administrator-configured **group or instance access token** for a dedicated review bot
identity, scoped to `api`, loaded from a file/secret-mount source (literal env rejected in
production, matching Phase 7). This is GitLab's analog to the GitHub App platform credential. OAuth
application / per-user flows are out of scope for the single-node MVP. Personal access tokens tied to
a human are not the platform model.

### Decision 2 — Base-URL policy (self-managed)

Proposed: `https://gitlab.com` is always allowed; a self-managed base URL is allowed only when it is
on an administrator allow-list, is HTTPS, passes host/address validation, blocks metadata/private
addresses unless explicitly approved, and rejects redirects to foreign origins (reusing the existing
SSRF/origin posture).

### Decision 3 — Webhook authentication

Proposed: verify GitLab's `X-Gitlab-Token` shared secret with constant-time comparison (GitLab does
not HMAC-sign the body by default). Because this is weaker than GitHub's HMAC, require TLS and a
high-entropy token, and document the difference. Enforce the same route/media-type/byte limits and
the Phase 7 ingress rate limit.

### Decision 4 — Status mechanism

Proposed: map review status to **GitLab commit statuses** (contexts), since GitLab has no Check Run
equivalent. The single-status-per-run reconciliation and non-blocking conclusion mapping mirror the
GitHub Check Run semantics.

### Decision 5 — Thread discovery and resolution

Proposed: map per-finding threads to **GitLab merge-request discussions** (resolvable notes) via REST
v4; discovery matches the exact per-finding hidden marker in note bodies, and resolution uses the
discussion resolve endpoint with the same reserve → attempt → verify-by-reread → reconcile,
ambiguity-safe discipline. The per-finding and review markers are reused unchanged.

### Decision 6 — Marker and lifecycle reuse

Proposed: reuse the existing review/finding marker formats, fingerprints, dedup, selection,
revalidation, and retention unchanged; only the SCM adapter and ingress differ.

## 3. Scope

- Core multi-SCM generalization (provider-routable ingress + multi-bundle composition).
- A native GitLab SCM adapter: auth, webhook normalization, merge-request/diff/file reads, review
  publication, commit status, and resolvable-discussion discovery/resolution.
- GitLab configuration (base URL, token source, webhook secret) with production secret-source policy.
- Shared SCM contract coverage run against the GitLab adapter, plus mocked-HTTP adapter tests.
- Documentation (provider doc + ADR) and contract/boundary validation.

## 4. Explicit non-goals

- Bitbucket Cloud or Data Center (separate later phases).
- OpenAI/second AI provider (Phase 8), agent mode (Phase 10).
- GitLab OAuth per-user flows, GitLab pipelines-as-status, GitLab GraphQL (REST v4 only in MVP).
- Any change to GitHub behavior or to the review/finding-lifecycle logic.
- A live GitLab real-sandbox demonstration in this phase (validated by contract + mocked-HTTP tests;
  live validation is deferred until GitLab credentials/instances are provided).

## 5. Core multi-SCM generalization

Today the ingress is `/webhooks/github` and the API/worker compose exactly one GitHub bundle. Phase 9
generalizes this without changing GitHub behavior:

- Provider-routable ingress: a per-provider webhook route (e.g. `/webhooks/{provider}`) that
  dispatches to a registered provider webhook adapter exposing signature/token verification and
  normalization. GitHub keeps its existing path as an alias for compatibility.
- A provider webhook registry mapping `ProviderId` to its webhook adapter (verification + normalizer)
  and its durable ingress identity scheme.
- Composition registers one or more SCM bundles into the existing `ProviderRegistry`; the worker and
  API iterate registered providers rather than assuming GitHub.
- The normalized `ReviewEvent`, delivery/semantic identity, and durable persistence remain
  provider-neutral and unchanged; only the per-provider normalizer differs.

## 6. GitLab adapter

Inside `adapters/scm/gitlab/`, mirroring the GitHub adapter structure and the segregated ports:

- **Auth**: a bot access token (Decision 1) injected as `PRIVATE-TOKEN`/`Authorization: Bearer`,
  with in-memory handling, redaction, and no durable storage.
- **Client**: an origin-confined async `httpx` client with the Phase 2 same-origin/redirect/timeout
  and SSRF posture, honoring the validated base URL (Decision 2) and bounded pagination.
- **Webhook normalization**: map `Merge Request Hook` actions (`open`, `reopen`, `update`) into the
  normalized `ReviewEvent`, with current-head (`sha`) and base handling under the existing Phase 3
  stale-head/supersession rules.
- **Reads**: current merge request (`ChangeRequest`), diff (merge-request changes), and file at an
  exact commit SHA through `RepositoryContentReadPort` (never a branch name).
- **Publishing**: one batched review as merge-request notes/discussions with the review marker and
  per-finding hidden markers on inline discussions, under the existing operation-reservation and
  marker reconciliation discipline (adapted to GitLab identities).
- **Status**: create/reconcile one commit status per run and transition it through
  queued/in-progress/terminal; findings never block.
- **Threads**: complete, bounded enumeration of MR discussions, exact per-finding marker matching,
  durable linkage, and ambiguity-safe resolution via the discussion resolve endpoint.
- **Capabilities**: declare `inline_comments`, `summary_comments`, `commit_statuses`,
  `repository_file_access`, `resolvable_threads`, and installation/token authentication according to
  what is configured and contract-tested.

No GitLab DTO or URL crosses into the core.

## 7. Configuration

Add `GitLabSettings` (env prefix `VENAROQ_`): enablement, base URL, allow-listed self-managed hosts,
token source (file in production), webhook secret, API version, timeouts, pagination bounds, and the
rollout/stable-identity components. Stable repository identity is
`gitlab:<installation/group>:<project id>`, provider-neutral and name-independent.

## 8. Security

- Reuse the SSRF/origin/redirect controls; validate the self-managed base URL (Decision 2).
- Constant-time webhook token verification (Decision 3); reaffirm route/byte/media-type limits and
  the Phase 7 ingress rate limit.
- Token redaction in logs/metrics/errors; production file-source policy; no durable credentials.

## 9. Test plan

- The shared SCM contract suite runs against the GitLab adapter for every port it implements
  (identity, diff/positions, capability composition, batched publishing, anchor/position prevalidation
  and summary fallback, operation reservation, hidden-marker reconciliation, provider-ID recording,
  single-status updates, pagination, auth refresh, rate limits, error taxonomy, thread identities).
- GitLab-specific mocked-HTTP (respx/MockTransport) tests: MR webhook normalization and actions,
  base-URL/SSRF validation, token auth, pagination, MR diff positions, discussion discovery and
  resolution, commit-status mapping, and redaction.
- Core generalization: provider-routable ingress dispatch, multi-bundle composition, and GitHub
  behavior unchanged (full GitHub regression green).
- Full Phase 3–7 regression suites, Ruff, strict Pyright, pytest, pre-commit, diff checks, Docker
  build, and isolated Compose readiness. Production publishing remains fail-closed.

## 10. Validation gates

Before merge: approved plan; Ruff format/check; strict Pyright; full pytest and pre-commit; diff
checks; Phase 3–7 regression suites; GitLab contract + mocked-HTTP matrices; core generalization and
GitHub-unchanged regression; Docker build and isolated Compose readiness; production publishing
rejection. A live GitLab sandbox demonstration is deferred until credentials are provided and is not
a merge gate for this phase.

## 11. Suggested implementation slices

1. Core provider-routable ingress + multi-bundle composition (GitHub behavior unchanged).
2. GitLab config, client, auth, and base-URL/SSRF validation.
3. GitLab webhook normalization and current-head/base handling.
4. GitLab MR/diff/file reads and the shared read contracts.
5. GitLab publishing, commit status, and marker reconciliation.
6. GitLab discussion discovery and ambiguity-safe resolution.
7. Capability composition, provider doc, ADR, and full regression.

Every slice stays on one Phase 9 feature branch and one implementation pull request.

## 12. Completion criteria

Phase 9 is complete only when:

- a merge request runs the same core review workflow with no GitLab conditionals in the core;
- the GitLab adapter passes the shared SCM contracts and GitLab-specific mocked-HTTP tests;
- the ingress is provider-routable and composition registers multiple SCM bundles;
- GitHub behavior and all Phase 3–7 safety guarantees remain green;
- base-URL/SSRF, token, and production secret-source controls hold;
- production publishing remains fail-closed;
- a live GitLab demonstration is documented as the only deferred item pending credentials.

Do not begin a Bitbucket phase without a separate explicit approval.
