# Phase 10 — Generic agent mode

## Status

Draft for review. This document does not authorize implementation.

Phase 10 starts only after this plan is approved. It preserves every prior guarantee (queue
durability, provider/write ambiguity safety, trusted-base policy, finding lifecycle, observability
and the production gate, and provider neutrality) and is strictly **additive**: when agent mode is
unavailable or disabled, review behaves exactly as today (diff-only). This phase materially expands
prompt-injection and data-exposure risk, so §9 is a dedicated security review and is part of the
approval.

## 1. Goal

Let capable model + SCM pairs **verify** diff findings against bounded repository context, via a
read-only tool loop (`get_file`, `get_tree`), reducing false positives and false negatives on
misleading diffs. Agent mode never executes code, never writes anything, never searches or indexes
the repository broadly, and never reviews code outside the diff. Every context-informed claim carries
**verification provenance** (which files, at which commit, were consulted), and any limit exhaustion
yields a clearly-marked **partial** review rather than an unbounded crawl.

## 2. Scope boundary: verification, not exploration

Proposed and load-bearing for the whole design: agent mode is **verification-only**.

- Findings remain **anchored to changed lines**, and each finding's `evidence` must still appear
  verbatim in the diff (the Phase 6 rule is unchanged). Agent mode does not create findings about
  unchanged code or files not in the diff.
- Fetched file/tree content is used only to **confirm or refute** a candidate finding (e.g. a diff
  that looks buggy is fine given a guard elsewhere in the file, or vice versa) and to set confidence.
- This directly satisfies the roadmap's "no broad search/index" and keeps the data-exposure surface
  to files the model deliberately requests to verify a specific changed line.

## 3. Decisions requiring approval

### Decision 1 — The agent loop lives in the provider-neutral core, not the adapter

Proposed: a new application-layer `AgentReviewService` owns the bounded loop, the limits, the path
and secret guards, the provenance recording, and the partial-review decision. The AI port gains a
single turn primitive — the adapter translates one model turn into either (a) a set of neutral tool
requests or (b) a final structured review — and nothing else. **Tools are executed by the core**
against the existing `RepositoryContentReadPort`; the adapter never touches the SCM. This keeps all
security controls in one provider-neutral place and keeps each adapter responsible only for its own
tool-call wire format (Anthropic tool-use blocks, OpenAI function/tool calls). No provider name
enters the orchestrator (the Phase 8 guard test is extended to the agent path).

### Decision 2 — Exactly two read-only tools, pinned to the PR commits

Proposed: `get_tree(path)` and `get_file(path)` only. Both resolve against the existing content port
and are pinned to the pull request's **head** commit (base allowed only for explicit before/after
comparison); arbitrary refs, other repositories, and any non-content tool are rejected. No tool can
write, execute, search, or enumerate beyond a single directory/file request. Unknown tool names and
malformed arguments are refused without a provider round-trip.

### Decision 3 — Path and secret protections (defense in depth)

Proposed, enforced in the core before any content call:

- Canonicalize and reject traversal (`..`), absolute paths, and anything resolving outside the repo
  root; normalize separators; reject NUL/control characters and overlong paths.
- A secret/deny-list of path globs (`.env*`, `*.pem`, `*.key`, `id_rsa*`, `*.p12`, `.git/**`,
  `**/secrets/**`, `**/credentials*`, lockfiles over a size cap, etc.) is never returned, even if the
  model requests it; the refusal is recorded as a bounded, non-reproducing event.
- Per-file byte cap, aggregate byte cap, and a cap on tree-entry count; binary/oversized files are
  skipped with a typed reason, never truncated into the prompt silently.

### Decision 4 — Hard limits with partial-review fallback

Proposed per review: a maximum number of tool calls (e.g. 8), a maximum number of distinct files
(e.g. 10), the aggregate byte cap from Decision 3, and a wall-clock/turn bound. Reaching any limit
**stops the loop and produces a partial review** flagged exactly like today's completeness partials;
it never errors the job or crawls further. Admission accounting from Phase 4/8 covers each turn's
input, and usage is summed across turns.

### Decision 5 — Prompt-injection posture (untrusted content channel)

Proposed: file and tree results are inserted only as clearly-delimited **tool results**, never into
the instruction channel, and the system prompt reasserts that repository content is data and never
instructions. The model cannot change the tool set, request new capabilities, alter limits, or cause
any side effect; tool arguments are validated structurally and the core — not the model — decides
what is fetched and whether it is allowed. Findings are still strictly validated and evidence-bound
to the diff, so injected text in a fetched file cannot manufacture a finding.

### Decision 6 — Capability gating and graceful fallback

Proposed: agent mode runs only when **all** hold — the resolved model profile allows the `agent`
review mode and declares tool use (a new `AIProviderCapabilities.tool_use`, plus the existing
`native_tools`), the SCM bundle advertises `repository_file_access` and `tree_access`, and an
administrator switch (`VENAROQ_REVIEW_AGENT_MODE_ENABLED`, default false) is on. If any is missing, the
review silently runs diff-only. Default shipped profiles stay diff-only; agent mode is opt-in via a
distinct agent-capable model alias.

### Decision 7 — Verification provenance in the artifact and published review

Proposed: the review artifact records, per run and per finding, the `path@sha` set actually consulted
and whether the context was complete. The published review surfaces this compactly (e.g. "verified
against N files at `<sha>`") and marks partial reviews. Provenance contains only paths and commit
SHAs — never fetched file contents — consistent with the no-raw-content-in-DB rule.

### Decision 8 — Ambiguity safety per turn

Proposed: each model turn reuses the existing provider-call taxonomy and reserve→attempt→observe
discipline. An ambiguous turn aborts to a partial review and is **never retransmitted** (a tool loop
must not replay a possibly-applied turn). Read-only tools have no external side effects, so tool
execution itself is freely retryable within limits.

## 4. Scope and files

New (indicative):
- `src/venaroq/application/review/agent.py` — `AgentReviewService` (loop, limits, provenance, partial).
- `src/venaroq/application/review/agent_tools.py` — neutral tool definitions + core-side executor with
  the path/secret/limit guards over `RepositoryContentReadPort`.
- `src/venaroq/domain/agent.py` — neutral tool-request/result and provenance value objects.
- Port change in `src/venaroq/ports/ai.py` — an agent-turn primitive on the review generator.
- Adapter turn implementations for Anthropic and OpenAI (tool-call wire formats) reusing the shared
  review schema for the final structured output.
- `AIProviderCapabilities.tool_use`; agent-capable model profiles/aliases.
- Config: `VENAROQ_REVIEW_AGENT_MODE_ENABLED` and the agent limits.
- `docs/adr/0012-agent-mode.md`; a sandbox-validation runbook.

Explicitly **not** modified: the SCM write/publish path, persistence schema beyond additive
provenance fields, finding identity/fingerprints, dedup, and the diff-only execution path (it remains
the default and the fallback).

## 5. Delivered behavior

For an enabled, capable repository, the reviewer may fetch a bounded set of files to verify candidate
findings before finalizing; the published review is diff-anchored as today but with fewer false
positives/negatives and a provenance note. For every other repository or provider, behavior is byte
-for-byte the current diff-only review.

## 6. Tests

- **Path traversal / absolute / control-char** requests are refused.
- **Secret deny-list** files are never returned even when explicitly requested.
- **Oversized / binary / too-many-files / too-many-calls** hit limits and yield a partial review.
- **Prompt injection** in a fetched file cannot create a finding or change the tool set/limits.
- **Unsupported capability** (model or SCM) falls back to diff-only with no tool calls.
- **Verified claim**: a misleading diff whose surrounding file flips the outcome is handled correctly
  with provenance recorded (mocked SCM content + mocked tool-use turns).
- **Ambiguous turn** aborts to partial and never retransmits.
- Guard test extended: no provider name in the agent orchestration path.
- Full suite stays green (ruff, pyright, pytest, docker build + `up --wait`).

## 7. Out of scope

Shell or code execution; writing/patching files; broad search, grep, or repository indexing;
autonomous changes or multi-PR actions; arbitrary refs or cross-repository reads; findings on
unchanged code; new SCM/AI providers.

## 8. Invariants preserved

Provider-neutral core (no provider-name branch; guard test); hexagonal ports/adapters (the adapter
never calls the SCM); `review-v2` identity and the evidence-in-diff rule; ambiguity-safe provider and
write discipline; production gate and file-source credential posture; structured logging/redaction
(no fetched content, prompts, or secrets in logs or DB); explicit completeness/partial semantics.

## 9. Security review (part of approval)

Threat: agent mode lets an attacker who controls PR content attempt to (a) exfiltrate repository
secrets via tool requests, (b) inject instructions through fetched file content, or (c) exhaust
resources. Mitigations, in order of reliance: the **core**, not the model, decides every fetch and
enforces traversal/secret/size/count limits (Decision 3–4); fetched content enters only an untrusted,
delimited tool-result channel and can never create a finding or alter controls (Decision 5); tools
are read-only and pinned to the PR commits with no search primitive (Decision 2); provenance records
paths/SHAs only, never content (Decision 7); every turn is admission-bounded and ambiguity-safe
(Decision 8); and the whole feature is off by default and triple-gated (Decision 6). Residual risk:
a fetched file's content could still mislead the model's judgment — bounded by keeping findings
diff-anchored and evidence-verified, and by surfacing provenance so a human can audit what informed a
finding.

## 10. Validation approach (decision)

Proposed: **mocked-only** for correctness and all security tests (mocked `RepositoryContentReadPort`
content and mocked provider tool-use turns), consistent with recent phases and sufficient for the
traversal/secret/injection/exhaustion matrix. Because a GitHub App sandbox and an Anthropic sandbox
already exist, an **optional live sandbox demo** of a single misleading-diff case can be run if
desired; this plan does not require it for completion. (Confirm which you want.)

## 11. Acceptance / completion

- Capable pairs verify context through the bounded tools; every other pair stays diff-only.
- Context claims carry `path@sha` provenance; any limit exhaustion yields a partial review.
- The full security test matrix in §6 passes; no provider name enters the agent orchestration path.
- Full local validation suite green; validation scope recorded honestly.
