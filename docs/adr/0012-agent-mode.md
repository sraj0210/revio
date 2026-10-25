# ADR 0012: Bounded, read-only agent mode for finding verification

Status: accepted for Phase 10.

Agent mode lets a capable model verify diff findings against bounded repository context through two
read-only tools (`get_file`, `get_tree`) instead of reasoning from the diff alone. It is
verification-only: findings stay anchored to changed lines and each finding's evidence must still
appear verbatim in the diff; fetched content only confirms or refutes a candidate finding and never
becomes a new finding source. Agent mode never executes code, writes anything, searches or indexes
the repository, or reviews code outside the diff.

The agent loop lives in the provider-neutral core (`AgentReviewService`), not in any adapter. It
reuses the existing diff-only preflight/generate/repair seam, so the durable ProviderCall state
machine and artifact persistence in `ReviewGenerationService` are unchanged; the whole agent run is
one durable generation attempt. The adapter only translates a single model turn into either neutral
tool requests or a final structured review, and appends the core's tool results to its own
conversation state. The adapter never touches the SCM. No provider name enters the orchestrator,
execution, or lifecycle layers; a grep-style guard test enforces this.

Tools are executed by the core through a guarded executor (`AgentToolExecutor`) over the existing
`RepositoryContentReadPort`, pinned to the pull request's head commit. The executor is the single
place that decides what the model may read: it canonicalizes paths and rejects traversal, absolute
paths, and control characters; enforces a secret deny-list (`*.pem`, `*.key`, `.env*`, `id_rsa*`,
`.git/**`, `secrets*`, credentials, …) that is never returned even when explicitly requested; and
caps per-file bytes, aggregate bytes, distinct files, and total tool calls. Reaching a budget marks
the context incomplete and stops rather than crawling further. Fetched content is returned only as an
untrusted tool result and can never change which tools exist, the limits, or whether a finding is
created.

The final structured review is delivered through a `submit_review` tool whose input schema is the
shared `review-v2` schema, so tool use and structured output never conflict. A turn that produces
neither tools nor a review is malformed (degrades to a partial review); a refusal or truncation is an
observed-terminal outcome; an ambiguous turn aborts to a partial review and is never retransmitted
(the read-only tools themselves have no side effects and are freely retryable within limits). A loop
that never converges within the step budget yields a partial review rather than an unbounded run.

Each review records verification provenance — the `path@sha` set actually consulted and whether the
context was complete — surfaced as a compact note in the published review (paths and commit SHAs
only, never fetched content, consistent with the no-raw-content rule).

Agent mode is off by default and triple-gated: the administrator switch
(`REVIO_REVIEW_AGENT_MODE_ENABLED`), an agent-capable model profile (declaring the `agent` review
mode and native tool use — shipped as the `agent-review-default` Anthropic alias), and an SCM that
advertises file and tree access. If any gate is missing, the review runs diff-only with byte-identical
behavior. OpenAI ships without an agent-turn adapter and therefore always falls back to diff-only; an
OpenAI agent turn is a follow-up.

Threat model and residual risk: an attacker controlling PR content might try to exfiltrate secrets via
tool requests, inject instructions through fetched files, or exhaust resources. The core (not the
model) decides and bounds every fetch; fetched content is untrusted and cannot alter controls or
manufacture findings; tools are read-only with no search primitive; provenance records paths/SHAs
only; every turn is admission-bounded and ambiguity-safe; and the feature is off by default. Residual
risk is that fetched content could still mislead the model's judgment — bounded by keeping findings
diff-anchored and evidence-verified and by surfacing provenance for human audit. Validation for this
phase is mocked-only (the full traversal/secret/injection/exhaustion matrix plus the agent loop and
wire format); a live sandbox demonstration is optional and not claimed here.
