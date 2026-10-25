# ADR 0011: OpenAI as a second, interchangeable AI provider

Status: accepted for Phase 8.

Revio adds a native OpenAI review generator alongside Anthropic to prove that the AI boundary is
genuinely provider-neutral. The same `ReviewRequest` produces an identical-shaped `ReviewResult`
through either provider, and no provider name appears in the orchestrator, execution, or lifecycle
layers.

The structured review and revalidation contract is extracted into a single shared module,
`revio.adapters.ai.review_schema`: the lenient structured-output DTOs, the `review-v2` JSON schema,
the system prompts, the request serialization, and the mapping from a validated response into domain
`Finding` / `ReviewResult` / revalidation objects. Both adapters import it, so the finding identity,
the evidence-must-appear-verbatim-in-the-diff rule, and the three-state revalidation contract cannot
drift between providers. Each adapter supplies only its own transport, structured-output dialect,
usage parsing, and error-taxonomy mapping.

Provider selection is alias-driven. The worker registers both model profiles, resolves the
administrator `REVIO_REVIEW_MODEL_ALIAS` (or the fallback) to one `ResolvedModelProfile`, and the
composition root constructs the single enabled adapter keyed by that profile's `provider_id`. Adding
a provider is a new adapter plus one enablement line in the composition root; nothing else changes.
Anthropic remains the shipped default (`review-default`); OpenAI is selected explicitly with
`openai-review-default`.

OpenAI uses the Responses API with strict JSON-schema structured output and a pinned, deterministic
GPT-5-class model (minimal reasoning effort, default sampling), mirroring the pinned-model,
thinking-disabled posture of the Anthropic path. OpenAI exposes no bounded token-count endpoint, so
the profile declares `token_counting=false` and preflight bounds admission with a conservative local
byte-based estimate instead of a network round-trip; real usage is always read back from the
response. Usage maps from `input_tokens` / `input_tokens_details.cached_tokens` / `output_tokens`
into the existing `TokenUsage` (uncached = total input − cached; cache-creation is never billed).

Outcomes map onto the existing provider-call taxonomy with the same crash-safe discipline as
Anthropic: connect-before-acceptance is a safe retry; timeouts and transport loss are ambiguous and
never retransmitted; 429 is an explicit safe retry; 5xx is ambiguous; other 4xx are terminal; an
observed 2xx refusal or `incomplete`/`max_output_tokens` is an observed-terminal generation outcome;
a 2xx without trustworthy usage is observed-invalid; and a 2xx with usage but an unparseable body is
malformed-but-accounted. The durable ProviderCall reserve → attempt → observe state machine is owned
by the lifecycle driver and is reused unchanged.

Configuration mirrors Anthropic's security posture: `REVIO_OPENAI_ENABLED`, exactly one API-key
source, a production file-source requirement (literal env keys rejected in production), an absolute
key-file path, and a base URL locked to `https://api.openai.com`. Azure OpenAI, Gemini, LiteLLM, and
arbitrary OpenAI-compatible endpoints are out of scope and rejected at configuration time.

Validation for this phase is mocked-only: a shared cross-provider contract suite plus OpenAI-specific
schema, usage, context, and error tests driven by mocked HTTP. The live "same synthetic review
through both providers" demonstration is deferred until an OpenAI API key is available and is not
claimed here.
