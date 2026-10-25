# Phase 8 — OpenAI second provider

## Status

Draft for review. This document does not authorize implementation.

Phase 8 starts only after this plan is approved. It preserves every Phase 3 queue-durability,
Phase 4 provider/write-ambiguity, Phase 5 trusted-base-policy, Phase 6 finding-lifecycle, and
Phase 7 observability/production-gate guarantee. It is **provider-additive**: it must not change
Anthropic behavior, SCM behavior, the domain, the ports, or the orchestrator's provider-neutral
contract. No provider-name branch may enter orchestration.

## 1. Goal

Prove genuine AI portability by adding a native OpenAI review generator as a second
`AIReviewGenerator` / `AIFindingRevalidator` behind the existing segregated AI ports. The same
synthetic review request must flow through either Anthropic or OpenAI selected purely by an
administrator model alias, with identical domain-level behavior (same `review-v2` finding identity,
evidence-in-diff validation, bounded admission, ambiguity-safe provider-call discipline, and
usage accounting) and **no changes to the orchestrator or any core code path**.

## 2. Decisions requiring approval

### Decision 1 — Provider selection mechanism (no new routing concept)

Proposed: selection stays entirely alias-driven. `resolve_review_profile` registers **both**
`anthropic_model_profile()` and `openai_model_profile()` in the `ModelRegistry`, then resolves the
administrator `review_model_alias` (or `review_fallback_alias`) to a single `ResolvedModelProfile`.
That profile already carries `provider_id`; the worker looks up the matching `AIAdapterBundle` from
the `ProviderRegistry` by `profile.provider_id`. No explicit "which provider" toggle is added, and
no provider name is ever compared in the orchestrator, execution, or lifecycle layers.

This replaces the two current Anthropic hard-codes in `worker/main.py`
([`resolve_review_profile`](../../src/revio/worker/main.py) registering only
`anthropic_model_profile()`, and the `AnthropicReviewAdapter(...)` / `not anthropic_enabled`
construction) with a provider-additive registry build.

### Decision 2 — OpenAI API surface and structured output

Proposed: use the **OpenAI Responses API** with **strict JSON-schema structured outputs**
(`response_format` / `text.format` json_schema, `strict: true`). This is OpenAI's first-class
structured-output mechanism and is the closest analog to the Anthropic structured tool/schema path
already used. The request is bounded and diff-only, reusing the same system prompt intent and the
same finding/identity schema shape as `review-v2`. The API version is pinned. Chat Completions is
not used.

### Decision 3 — Endpoint lock (no Azure / compatible endpoints)

Proposed: lock the base URL to `https://api.openai.com`, exactly mirroring the Anthropic
`anthropic_api_url` lock. Azure OpenAI, Gemini, LiteLLM, and arbitrary OpenAI-compatible endpoints
are explicitly out of scope (per the roadmap). Any other URL is rejected at config validation.

### Decision 4 — Model pinning and determinism

Proposed: `openai_model_profile()` pins a single administrator-approved OpenAI model ID (a current
GPT-5-class model; exact ID set in the profile), with reasoning/verbosity controls set for
determinism (minimal reasoning effort, `temperature` fixed/low where the model honors it), matching
the "pinned `claude-sonnet-5`, thinking disabled" posture of Phase 4. `allowed_review_modes`
includes `diff_only`. The alias is distinct (e.g. `openai-review-default`) so administrators select
it explicitly; the shipped default alias behavior is unchanged (Anthropic remains the default).

### Decision 5 — Config / enablement posture (mirror Anthropic exactly)

Proposed: a new `OpenAISettings` mirroring `AnthropicSettings` field-for-field in security posture:
`REVIO_OPENAI_ENABLED`, exactly one of `openai_api_key` / `openai_api_key_file` required when
enabled, **production requires the file source** (literal env key rejected in production), key file
must be absolute, URL locked (Decision 3), bounded HTTP timeout. Both providers may be enabled at
once. The worker requires that the provider selected by the resolved alias is actually enabled and
composed; otherwise it fails closed with a clear error (replacing the current
`review execution requires Anthropic` check with a provider-neutral "selected review provider is
not enabled" check).

### Decision 6 — Reuse of finding schema, identity, lifecycle, and taxonomy

Proposed: reuse unchanged — the finding/identity DTO shape, strict core re-validation,
evidence-must-appear-verbatim-in-diff rule, fingerprints, dedup/selection, revalidation three-state
outcome, retention, and markers. Only the transport, schema dialect, usage parsing, and error
mapping are OpenAI-specific. The OpenAI adapter maps OpenAI outcomes onto the **existing** provider
-call error taxonomy (`ProviderCallAmbiguousError`, `ProviderCallRejectedError`,
`ProviderCallSafeRetryError`, `ProviderCallTerminalError`, `ProviderCallObservedTerminalError`,
`ProviderCallObservedInvalidResponseError`, `MalformedProviderOutputError`, `ProviderTransientError`)
and produces the same `TokenUsage` with observed-vs-unknown accounting. The durable ProviderCall
reserve → attempt → observe state machine and **no-retransmission-after-ambiguous** rule are owned by
the existing lifecycle driver and are reused as-is.

### Decision 7 — Validation depth (confirmed with requester)

Confirmed: **mocked-only** for this phase. Validation is shared contract tests plus OpenAI-specific
schema, usage, context-window, and error-case tests driven by mocked HTTP (respx / `MockTransport`).
The live "same synthetic review through both providers" demonstration is **deferred** until an
OpenAI API key is available; this plan does not claim a live cross-provider run.

## 3. Scope and files

New:
- `src/revio/config/openai.py` — `OpenAISettings` (Decision 5).
- `src/revio/adapters/ai/openai/__init__.py`, `src/revio/adapters/ai/openai/adapter.py` —
  `OpenAIReviewAdapter` implementing `AIReviewGenerator` + `AIFindingRevalidator`, `OPENAI_PROVIDER_ID`,
  `openai_model_profile()` (Decisions 2, 4, 6).
- `docs/adr/0011-openai-second-provider.md` — record the portability seam and the endpoint/model lock.
- Tests: a shared provider-contract suite exercising both adapters against the same synthetic review,
  plus OpenAI-specific schema/usage/context/error tests.

Modified (minimal, provider-additive):
- `src/revio/worker/main.py` — register both model profiles; build enabled AI adapters into the
  `ProviderRegistry`; select the adapter by `profile.provider_id`; provider-neutral enablement check.
- `src/revio/api/app.py` / composition — register the OpenAI `AIAdapterBundle` when enabled, if the
  API path composes AI (parity with how Anthropic is wired).
- `.env.example`, `docker-compose.yml` — add the `REVIO_OPENAI_*` keys (defaults disabled/empty),
  mirroring the Anthropic entries.
- `README.md` — add OpenAI to current capabilities and the provider-selection note.

Explicitly **not** modified: `src/revio/domain/**`, `src/revio/ports/ai.py`, the orchestrator,
execution, lifecycle, markers, persistence schema, and all SCM adapters.

## 4. Delivered behavior

The same `ReviewRequest` produces a `ReviewResult` with identical structure and identity whether the
resolved alias points at Anthropic or OpenAI. Switching providers is an administrator alias change
only; no code path branches on provider name. Both providers honor the same bounded diff-only
request, the same structured finding schema, the same evidence validation, and the same crash-safe
provider-call ambiguity discipline.

## 5. Tests

- **Shared contract suite:** parametrized over both adapters — same synthetic diff in, assert same
  finding identity/evidence/validation outcomes, same bounded-admission behavior, same mapping of a
  clean response, an empty-findings response, and an over-limit/invalid identity (dropped by strict
  core re-validation).
- **OpenAI schema:** strict json_schema request shape; malformed/extra-field responses rejected
  safely; no fenced code / no source leakage assertions.
- **OpenAI usage:** observed usage parsed into `TokenUsage`; missing/partial usage → unknown
  accounting (never fabricated).
- **OpenAI context:** oversize request handling and bounded admission.
- **OpenAI errors:** map 400/401/403/409/429/5xx and network/timeout/ambiguous conditions onto the
  existing taxonomy; assert **no retransmission** after an ambiguous attempt; assert secrets and raw
  bodies never appear in errors/logs.
- Full suite stays green (ruff format/check, pyright strict, pytest, docker-compose build + `up --wait`).

## 6. Out of scope

Azure OpenAI, Gemini, LiteLLM, and arbitrary OpenAI-compatible endpoints; agent mode; new review
modes; runtime cross-provider failover (the fallback alias remains default-selection, not live
failover); any SCM, domain, port, or schema change; a live OpenAI run (deferred, Decision 7).

## 7. Invariants preserved

Provider-neutral core (no SCM/AI SDK types or provider-name conditionals leak into domain,
application, or orchestrator); hexagonal ports/adapters; `review-v2` finding identity and
evidence-in-diff rule; ambiguity-safe ProviderCall and write discipline; Phase 7 production gate and
file-source credential requirement; structured logging + redaction (no secrets, prompts, provider
bodies, or credentials in logs or DB).

## 8. Acceptance / completion

- Both adapters pass the shared contract suite.
- No provider-name branch exists in orchestration/execution/lifecycle (verified by inspection + a
  guard test or grep check in CI-style validation).
- A model-alias change is the only switch between providers.
- Full local validation suite green; mocked-only validation documented honestly; the live
  cross-provider demo is recorded as deferred pending a key.
