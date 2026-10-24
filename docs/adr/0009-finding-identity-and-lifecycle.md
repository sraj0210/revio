# ADR 0009: Finding identity, deduplication, and conservative lifecycle

Status: accepted for Phase 6.

Findings carry bounded, structured identity (normalized rule, optional symbol, and an exact
evidence snippet validated against the submitted diff). The primary fingerprint v1 is a
length-framed SHA-256 over stable repository identity, normalized path, category, rule, optional
symbol, and bounded evidence. Generated title, explanation, remediation, confidence wording, and
line number are never identity inputs. The secondary fingerprint adds a conservative title for
candidate ranking only and never authorizes suppression, linkage, or resolution. The review
prompt/schema advance to `review-v2`; legacy `review-v1` artifacts keep their identity and are
never regenerated.

Finding records are scoped by stable repository identity plus pull-request number, so an exact
primary-fingerprint match within the same pull request is a duplicate and is not reposted, while
the same code in another pull request remains a distinct finding. Deduplication persists the
authoritative artifact first, validates evidence, computes fingerprints, atomically upserts finding
records and occurrences, and builds a durable publication projection containing only new findings.
When a complete run has no new publishable findings, the Check Run finishes successfully with a
deterministic summary and no pull-request review is created.

Lifecycle operations are conservative and ambiguity-safe. At most 25 prior open findings are
selected per run by a deterministic order (changed path, observed-again, recency, stable id). Only
selected findings with a durable `fixed` revalidation result, obtained from content fetched at the
exact latest current head, may resolve their linked thread. Revalidation uses a distinct durable
ProviderCall kind; ambiguous Messages outcomes are never retransmitted and yield durable
`uncertain` results. Thread discovery is an adapter-private, bounded, cursor-paginated GraphQL read
matching only the exact per-finding hidden marker; a link is persisted only when complete
enumeration proves exactly one match. Thread resolution issues at most one `resolveReviewThread`
mutation verified by complete reread; ambiguous mutations reconcile read-only and are never
repeated, leaving the finding open or resolution-indeterminate without proof. Node-id mismatch or
duplicate linkage is an integrity failure that fails readiness.

Open and resolution-indeterminate findings, unresolved operations, and their links are retained
indefinitely. Confirmed-closed history becomes eligible for operator-initiated pruning 180 days
after resolution, under the existing offline, dry-run, backup-confirmed maintenance discipline.
Only bounded evidence (capped at 2 KiB) is persisted as a source-derived lifecycle value; full
files, prompts, provider bodies, credentials, and generated prose never enter persistence or logs.
