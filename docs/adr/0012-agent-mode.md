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
paths, and C0/DEL/C1 control characters; enforces a **case-insensitive** secret deny-list (`*.pem`,
`*.key`, `.env`/`*.env`, `*.tfstate`/`*.tfvars`, `id_rsa*`, `*.gpg`, `kubeconfig`, `.git/**`,
`.ssh/**`, `.aws/**`, `.kube/**`, credentials, …) that is never returned even when explicitly
requested; and caps per-file bytes, aggregate bytes (tree listings included), distinct files, and
total tool calls. `get_tree` additionally omits any excluded path from its listing, so secret
filenames are never even enumerated. A content-port failure degrades to a tool refusal rather than
crashing the review. Reaching a budget marks the context incomplete and stops rather than crawling
further. Fetched content is returned only as an untrusted tool result and can never change which
tools exist, the limits, or whether a finding is created.

Because the published summary and finding narrative are model-controlled free text, a filename
deny-list alone cannot stop an injected instruction from copying a *non*-denylisted file's contents
into the review. The core therefore also enforces **verification-only output**: after a final review,
the service checks the summary and every finding's title/explanation against the content actually
fetched this run and, if any long verbatim span is reproduced, withholds the entire review (partial,
no findings). The model may read files to judge findings but may not quote them.

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
tool requests, inject instructions through fetched files, or exhaust resources. Defenses, in order of
reliance: the core (not the model) decides and bounds every fetch; a case-insensitive secret deny-list
and a tree-listing filter block obvious secret paths; the verification-only output guard withholds any
review that reproduces fetched content; fetched content is untrusted and cannot alter controls or
manufacture findings; tools are read-only with no search primitive; provenance records paths/SHAs
only; every turn is admission-bounded and ambiguity-safe; and the feature is off by default and
triple-gated.

Residual risk, stated honestly: (1) the verbatim-quote guard triggers only on spans at or above the
shared long-source threshold, so a **short** secret copied into the narrative from a non-denylisted
file could still slip through — the deny-list is the first line for known-secret files, but a small
secret in an ordinary config file remains a theoretical exfiltration path; (2) fetched content can
still mislead the model's judgment, bounded by keeping findings diff-anchored and evidence-verified
and by surfacing provenance for human audit. These are why agent mode is off by default and gated.
Validation for this phase is mocked-only (the full traversal/secret/injection/exhaustion matrix, the
verification-only output guard, the agent loop, the provider wire format, and an end-to-end run
through the durable generation path); a live sandbox demonstration is optional and not claimed here.
