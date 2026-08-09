# ADR 0007: Durable Anthropic calls and normalized artifacts

Status: accepted for Phase 4.

Every actual Messages attempt has immutable identity and a monotonic durable state. Revio commits
`attempt_started` before transmission. A restart with no trustworthy recorded response treats the
call as ambiguous and never retransmits it, including repair calls. Observed usage is append-only;
ambiguous usage is explicitly unknown.

Every publishable outcome produces a bounded provider-neutral `NormalizedReviewArtifact` before an
outcome-dependent GitHub write. The artifact stores validated review prose, routing, stable reasons,
and a deterministic digest—not raw Messages blocks, prompts, diffs, patches, source snapshots,
dedicated evidence, long quotations, provider bodies, or secrets. Restarts reuse the artifact and do
not repeat generation.
