# Anthropic provider

Phase 4 supports only the native Messages API at `https://api.anthropic.com` and the administrator
alias `review-default`, pinned to `claude-sonnet-5`. The request sends
`thinking.type=disabled`, omits non-default sampling fields, and uses
`output_config.format` JSON schema. Prompt caching marks only trusted versioned system instructions;
pull-request content is never intentionally cached.

Configure exactly one of `REVIO_ANTHROPIC_API_KEY` or an absolute
`REVIO_ANTHROPIC_API_KEY_FILE`. Review execution is separately gated by
`REVIO_REVIEW_ENABLED`. Exact normalized usage comes only from observed Messages responses; an
ambiguous attempt records unknown usage and is never retransmitted.

Token-count failures occur before Messages and are classified as safe preflight retry or terminal
preflight rejection. Messages connection failure before acceptance and explicit 429/529 responses
may use a new durable ProviderCall ordinal; timeout, read-loss, and 5xx ambiguity never does.
Refusal, `max_tokens`, and terminal 4xx responses become deterministic neutral artifacts and never
escape the worker. Repair has an independent ordinal sequence and the same no-retransmission rule.
