# Phase 6 — Deduplication and Finding Lifecycle

## Status

Approved and implemented on the Phase 6 feature branch (`feat/phase-6-finding-lifecycle`). Merge
remains subject to the validation gates below.

Phase 6 preserves all Phase 3 queue durability, Phase 4 provider/write ambiguity safety, and Phase 5
trusted-base policy guarantees. Production review publishing remains prohibited; Phase 7 owns
production rollout.

## 1. Goal

Prevent repeated finding noise and conservatively resolve earlier GitHub review threads only when a
bounded revalidation against the latest current pull-request head provides explicit evidence that the
finding is fixed.

Phase 6 adds exact, versioned finding identity; durable finding and thread linkage; bounded prior
selection; latest-head file retrieval; three-state revalidation; and ambiguity-safe thread resolution.

## 2. Frozen policy decisions

1. At most **25** open prior findings are selected for revalidation per review run. This is an
   administrator ceiling and repository configuration cannot raise or change it.
2. `still_present` findings receive no reply or update in Phase 6. Their existing thread remains open.
3. Open findings, unresolved resolution operations, and their thread links are retained indefinitely.
   Closed finding history becomes eligible for operator-initiated deletion **180 days** after closure.
   Phase 6 does not add automatic online deletion.
4. Only a selected finding with a durable `fixed` result based on content fetched from the exact latest
   current head may enter thread resolution.
5. `still_present`, `uncertain`, unselected, summary-only, unlinked, excluded, protected, unavailable,
   oversized, truncated, renamed-without-proof, or ambiguous findings remain open.
6. Exact primary-fingerprint matches are duplicates and are not reposted. Secondary fingerprints never
   suppress or resolve by themselves; they may only rank candidates for bounded revalidation.
7. When a completed review has no new publishable findings after exact deduplication, Venaroq completes
   the existing Check Run successfully with a deterministic service-owned summary and does not create a
   pull-request review.
8. An unchanged normalized diff hash may skip new generation only when the same repository, pull
   request, head SHA, effective-policy digest, prompt/schema/profile versions, and complete diff envelope
   already have a durable authoritative artifact. It never replaces webhook idempotency or current-head
   validation.
9. GitHub thread resolution is a durable external write. An ambiguous mutation is never blindly
   repeated. Complete thread rereads may reconcile it; otherwise it remains indeterminate and open.
10. Thread replies, “still present” updates, fuzzy/embedding matching, and reviewer re-request behavior
    remain out of scope.

## 3. Scope

Phase 6 includes:

- primary and secondary finding fingerprint v1;
- structured, bounded identity evidence in normalized findings;
- normalized diff hashing and narrowly authorized unchanged-run reuse;
- durable finding records and occurrences;
- deterministic bounded selection of earlier open findings;
- exact-latest-head repository file reads for selected findings;
- a provider-neutral finding-revalidation request/result contract;
- one durable ProviderCall identity per actual revalidation Messages attempt;
- three-state `fixed`, `still_present`, and `uncertain` results;
- exact duplicate suppression before GitHub review publication;
- per-finding hidden markers and durable GitHub review-thread linkage;
- complete, paginated GitHub thread discovery through an adapter-private GraphQL implementation;
- durable ambiguity-safe GitHub thread-resolution operations;
- lifecycle-aware retention and readiness checks;
- focused restart, concurrency, ambiguity, and real-sandbox validation.

## 4. Explicit non-goals

Phase 6 does not add:

- fuzzy matching, embeddings, edit distance, or probabilistic duplicate suppression;
- automatic resolution based on absence from a diff or model silence;
- repository-wide search, symbol indexing, code execution, or agent tools;
- arbitrary repository content supplied to the normal review prompt;
- thread replies, remediation conversations, or `still_present` comments;
- reviewer assignment/re-request event behavior;
- lifecycle support for providers without declared thread discovery/resolution capability;
- organization policy, an administrative UI, or automatic retention scheduling;
- production publishing or Phase 7 observability functionality.

## 5. Finding identity contract

### 5.1 Structured identity input

The versioned review schema adds a required `identity` object to each generated finding:

```json
{
  "rule": "sql-injection",
  "symbol": "UserRepository.find_by_name",
  "evidence": "query = f\"SELECT ... {name}\""
}
```

- `rule`: lower-case administrator-normalized identifier, 1–64 ASCII characters, pattern
  `[a-z0-9]+(?:[-_.][a-z0-9]+)*`;
- `symbol`: optional code anchor/symbol, normalized Unicode, 1–256 code points;
- `evidence`: an exact bounded snippet derived from the submitted diff, 1–512 code points and at most
  2 KiB UTF-8;
- path and category remain existing normalized finding fields;
- line and side remain publication anchors, not identity.

The core verifies that evidence occurs in the bounded submitted diff file after newline/whitespace
normalization. Invented, absent, oversized, or ambiguous evidence makes that finding locally invalid;
it is omitted and the existing partial/output-invalid policy applies. Generated title, explanation,
remediation, confidence wording, and free-form prose are never identity inputs.

This changes the review prompt/schema versions. Existing durable Phase 4/5 artifacts retain their old
schema identity and are never regenerated. Only Phase 6 artifacts can create lifecycle findings.

### 5.2 Canonical normalization

Primary fingerprint v1 hashes a length-delimited canonical encoding of:

1. literal version `finding-primary-v1`;
2. provider ID;
3. installation/account ID;
4. provider repository ID;
5. normalized repository-relative POSIX path;
6. normalized category;
7. normalized rule;
8. normalized optional symbol, or an explicit absent sentinel;
9. bounded normalized evidence.

Normalization:

- Unicode NFC;
- LF newlines;
- trim leading/trailing blank lines;
- collapse horizontal whitespace runs outside line boundaries;
- preserve case in paths, symbols, and evidence;
- lower-case category and rule only;
- preserve evidence line order;
- reject NUL, control characters, absolute/traversal/backslash paths, and over-limit values.

The digest is lower-case hexadecimal SHA-256. Canonical serialization uses explicit byte lengths, not
delimiter concatenation.

Secondary fingerprint v1 hashes version, stable repository identity, normalized path, category, rule,
and a conservative normalized title. It is stored only for selection/ranking and never authorizes
suppression, linkage, or resolution.

### 5.3 Rename behavior

A path change produces a different primary fingerprint. Phase 6 does not infer rename equivalence from
the provider diff alone. A previous finding on a renamed path may be selected, but it remains
`uncertain` unless revalidation explicitly identifies the exact current path and evidence. No record is
silently moved or closed.

## 6. Normalized diff hash

Compute `diff-hash-v1` only after provider completeness validation and Phase 5 exclusions, and before
service/token truncation. Hash canonical file identity, status, paths, patch state, and normalized line
side/old-line/new-line/content in provider order after deterministic duplicate validation.

An incomplete provider collection or incomplete patch can have a diagnostic hash but is never eligible
for unchanged-run generation reuse. The hash contains no raw content in persistence beyond the digest.

Reuse requires an exact durable key of stable repository identity, pull-request identity, current head
SHA, current base SHA, diff hash, effective-policy digest, prompt version, schema version, model profile
ID/version, and service limit profile. Reuse returns the existing durable artifact; it does not create a
new ProviderCall. It must not reuse a superseded, partial-input, indeterminate, or legacy artifact.

## 7. Durable data model and migration

Add migration `0004_phase6_finding_lifecycle`.

### 7.1 `finding_records`

- `id` deterministic UUID derived from stable repository identity plus primary fingerprint;
- stable provider/installation/repository identity components;
- pull-request identity;
- fingerprint version, primary fingerprint, and secondary fingerprint;
- normalized path, category, rule, optional symbol;
- evidence digest and bounded normalized evidence needed for revalidation;
- lifecycle state: `open`, `resolved`, or `resolution_indeterminate`;
- first/last seen run IDs and head SHAs;
- resolved head SHA and resolved timestamp when confirmed;
- created/updated timestamps;
- unique stable repository identity plus primary fingerprint.

The bounded evidence field is the only permitted source-derived lifecycle value. It is capped at 2 KiB,
never includes a whole file, and follows the same retention protections as an open finding.

### 7.2 `finding_occurrences`

- finding-record ID plus review-run ID primary identity;
- artifact finding ordinal;
- observed head SHA and path/line anchor metadata;
- disposition: `new`, `exact_duplicate`, `selected_prior`, `unselected_prior`;
- publication eligibility and provider comment linkage status;
- created timestamp.

Occurrences never store generated explanation/remediation beyond the authoritative normalized artifact.

### 7.3 `review_thread_links`

- finding-record ID primary/foreign key;
- provider review ID and provider thread node ID, both scoped by stable repository identity;
- provider comment ID when available;
- per-finding marker digest/version;
- link state: `pending_discovery`, `linked`, `not_threaded`, `discovery_indeterminate`;
- created/updated timestamps;
- uniqueness for provider thread ID and marker digest.

### 7.4 `finding_revalidation_runs` and results

One revalidation run per review run records current validated head SHA, selection limit, selected count,
unselected count, exact policy digest, state, and timestamps. One result per selected finding records
`fixed`, `still_present`, or `uncertain`, service-owned reason code, exact fetched ref, optional current
path, and timestamps. It stores no fetched file body or provider response.

### 7.5 `thread_resolution_operations`

- deterministic operation ID and finding-record ID;
- provider thread ID and validated head SHA;
- state: `reserved_unattempted`, `attempt_started`, `known_rejected`, `ambiguous`, `reconciled`,
  `completed`, or `integrity_failed`;
- attempt count, provider observed state, terminal reason, and timestamps;
- unique finding record and provider thread identity.

All new durable writes use the existing commit-reconciliation and compare-and-swap discipline. Never
persist full current files, raw provider bodies, prompts, credentials, or unsafe exceptions.

## 8. Prior-finding selection

Selection occurs after the normal artifact and its fingerprint projection are durable, but before any
latest-head file read, revalidation call, or lifecycle external write. The candidate query considers
only findings first observed before the current review run, so new findings cannot select themselves.

Eligible records are open findings for the same stable repository and pull request with a linked open
thread or a summary-only record that still requires lifecycle tracking. Exclude records whose path is
currently protected/excluded; persist them as unselected with reason `policy_excluded`.

Deterministic order:

1. exact normalized path appears in the complete current changed-file set;
2. primary fingerprint is observed again in the current durable artifact;
3. most recently seen head/time descending;
4. finding-record ID ascending as a stable tie-breaker.

Select at most 25. Persist the complete bounded decision set and counts before any file read or Messages
call. A concurrent worker must observe the same durable selection and cannot add candidates.

## 9. Latest-head evidence collection

Immediately before content reads, re-fetch `ChangeRequest` and require its head SHA to equal the run's
validated current head. A mismatch supersedes the run before lifecycle writes.

For each selected finding:

1. require a normalized, non-protected, non-excluded current path;
2. call `RepositoryContentReadPort.get_file` at exactly the latest current head SHA;
3. enforce a 128 KiB UTF-8 byte limit before revalidation;
4. never use a branch name, event head, base SHA, cached mutable ref, or content URL;
5. classify missing, oversized, non-UTF-8, truncated/malformed, permission, timeout, and ref mismatch as
   service-owned evidence outcomes.

Missing content is not proof of deletion. Every unavailable or non-authoritative outcome becomes
`uncertain`. A transient read may retry only before a revalidation ProviderCall or lifecycle external
write begins and under existing job retry bounds.

Fetched file content is ephemeral, grouped within an aggregate 512 KiB ceiling, and never persisted or
logged. Limit exhaustion marks remaining selected findings `uncertain` deterministically.

## 10. Revalidation contract and ProviderCalls

Add provider-neutral immutable models:

- `FindingRevalidationRequest` containing stable finding IDs, normalized identity material, bounded
  current-file content, and exact current head metadata;
- `FindingRevalidationResult` containing finding ID, enum state, optional current path, and bounded
  structured evidence offsets;
- `FindingRevalidationBatchResult` with exact usage.

The versioned revalidation prompt asks only whether the exact earlier issue is fixed, still present, or
uncertain. It cannot create new review findings or free-form thread text. Every selected ID must appear
exactly once; unknown, duplicate, missing, or invalid results make the affected batch `uncertain`.

Use a distinct ProviderCall kind `revalidation` and schema/prompt versions. The durable identity is
derived from review-run ID plus batch ordinal. Preserve Phase 4 rules:

- reserve durably before the Messages attempt;
- mark attempt started immediately before transport;
- never retransmit an ambiguous call;
- record exact usage only for observed responses;
- no automatic repair call in Phase 6; malformed/terminal/ambiguous outcomes yield durable `uncertain`;
- restart reuses durable results and never repeats an observed or ambiguous attempt;
- safe, proven pre-generation rejection may create the next bounded ordinal under existing retry rules.

At most one revalidation batch and one actual accepted Messages attempt occur per review run. If the
batch cannot fit the administrator token ceiling, deterministically reduce the selected suffix and mark
removed selections `uncertain` with `revalidation_limit`.

## 11. New finding persistence and exact deduplication

After the normal Phase 6 artifact is durable and before outcome-dependent GitHub review publication:

1. validate structured identity evidence against the submitted diff;
2. compute primary and secondary fingerprints;
3. atomically upsert finding records and occurrences;
4. classify a primary-fingerprint conflict as `exact_duplicate`;
5. retain the original finding/thread ownership and update only safe last-seen metadata;
6. construct a durable publication projection containing only new findings;
7. never mutate the authoritative normalized artifact to pretend duplicates were absent.

Duplicate suppression uses stable repository identity, not repository name. A duplicate from another
pull request is not suppressed in the MVP because its discussion context and thread differ.

If no new publishable findings remain, complete the Check Run with:

> No new findings were published; matching findings are already tracked.

No pull-request review POST is reserved or attempted for that outcome. Existing partial-artifact and
configuration-warning semantics remain stronger: a partial run stays neutral and warnings remain
visible in the Check summary.

## 12. Per-finding markers and thread discovery

Every newly published inline comment includes a service-generated hidden marker derived from the
finding record ID and active marker key. It contains no raw fingerprint, repository identity, evidence,
or secret. Summary findings are recorded as `not_threaded` and cannot be auto-resolved.

After a batched review POST is durably completed or reconciled, discover threads through a new
provider-neutral structured `ReviewThread` model. The GitHub adapter privately uses GraphQL with:

- installation authentication and the existing origin/TLS policy;
- complete cursor pagination with hard page/item bounds;
- pull-request repository/number scoping;
- thread node ID, resolved state, comment IDs/bodies, review IDs, and commit/path metadata;
- exact marker matching only;
- conflict detection for zero, duplicate, or cross-review matches.

Persist a link only after complete enumeration proves one exact marker match. Incomplete enumeration,
zero after provider propagation bounds, or multiple matches produces `discovery_indeterminate`; it
never guesses. Restart performs read-only discovery again and never repeats the already durable review
POST.

Phase 6 updates GitHub capability composition to declare `resolvable_threads=true` only when both
complete thread discovery and resolution mutation are configured and contract-tested.

## 13. Thread resolution safety

Only durable `fixed` results with a `linked` unresolved thread may reserve a resolution operation.
Immediately before reservation, re-fetch the current change request and require the same latest head
used by revalidation. A newer head supersedes lifecycle writes; fixed evidence from an older head never
resolves a thread.

For each operation:

1. completely reread the exact linked thread;
2. if already resolved, mark `reconciled` without mutation;
3. durably transition `reserved_unattempted -> attempt_started`;
4. issue one GitHub `resolveReviewThread` mutation;
5. on an observed success, reread and require `isResolved=true` before `completed`;
6. on an ambiguous transport outcome, transition to `ambiguous` and perform bounded complete rereads;
7. reconcile only when the exact thread is observed resolved;
8. otherwise leave `ambiguous`, set the finding `resolution_indeterminate`, and never repeat the
   mutation automatically;
9. known validation/auth/permission rejection leaves the finding open with a safe terminal reason.

No operation resolves a different thread after relinking. Duplicate/mismatched node identities are an
integrity failure and fail readiness.

## 14. Ordering in the review workflow

The effective Phase 6 order is:

1. Phase 3 current-head and installation validation;
2. Phase 5 rollout and durable trusted-base policy resolution;
3. complete provider diff collection, normalized diff hash, Phase 5 exclusions, and service/token
   admission;
4. unchanged-run artifact reuse when the complete exact key permits it, otherwise normal review
   generation and durable authoritative artifact;
5. durable fingerprint/occurrence upsert and new-finding publication projection;
6. durable bounded selection of findings first observed before this run;
7. exact latest-head file collection for selected prior findings;
8. durable revalidation ProviderCall/results, or deterministic `uncertain` fallback;
9. current-head validation;
10. existing ambiguity-safe Check Run/review publication of new findings only;
11. complete thread discovery and durable linkage;
12. latest-head revalidation check and ambiguity-safe resolution of confirmed-fixed linked threads;
13. terminal Check Run and queue transitions.

Revalidation resolution must not block publication of unrelated new findings. A resolution-indeterminate
finding makes the lifecycle portion partial/neutral with a stable reason; it never causes a duplicate
review POST or another Messages attempt.

## 15. Retention

Extend the existing offline, exclusive maintenance command with a Phase 6 lifecycle batch:

- never delete `open` or `resolution_indeterminate` findings;
- never delete unresolved thread discovery/resolution operations;
- closed findings become eligible 180 days after confirmed resolution;
- delete occurrences, links, and resolved operations only atomically with their eligible finding;
- retain the existing webhook tombstone and review-operation safety rules;
- dry-run, bounded batch, backup confirmation, commit reconciliation, and integrity metrics remain
  mandatory;
- repository configuration cannot alter retention.

Deletion of GitHub comments or reviews is not implemented.

## 16. Security and privacy

- Stable identity scopes every query and unique key; names are diagnostic only.
- Fingerprints and hidden markers reveal no source or tenant identifier.
- Evidence is exact, bounded, validated against submitted diff, and capped at 2 KiB.
- Current files are ephemeral and absent from DB/logs/errors/metrics.
- Generated prose is excluded from primary identity and secondary suppression authority.
- GraphQL pagination, variables, node IDs, response sizes, timeouts, redirects, rate limits, and error
  bodies receive the same validation/redaction posture as existing REST calls.
- Repository content cannot select prompts/models/endpoints or instruct revalidation behavior.
- Production publishing remains rejected at startup.

## 17. Test plan

### 17.1 Fingerprints and diff hashes

- deterministic primary/secondary fingerprint vectors and version separation;
- length-delimited collision resistance for field-boundary variations;
- Unicode NFC, whitespace/newline, case, path, rule/category, symbol, and snippet normalization;
- line movement does not change identity; path/category/rule/symbol/evidence changes do;
- title/explanation/remediation/confidence/line are absent from primary fingerprint;
- evidence must occur in submitted diff and obey byte/code-point bounds;
- repository/installation/provider isolation and repository rename stability;
- deterministic complete diff hash; order/content/status/side changes alter it;
- incomplete inputs never authorize unchanged-run reuse.

### 17.2 Selection and evidence

- zero, below-limit, at-limit, and over-limit deterministic selection;
- changed-path and exact-current-occurrence priority with stable tie-breaking;
- selected/unselected decisions are durable under concurrency/restart;
- exact latest current head is used, never base/event/branch ref;
- head race before read and before resolution supersedes safely;
- missing, deleted, renamed, excluded, protected, oversized, invalid UTF-8, transient, permission,
  truncation, ref mismatch, and aggregate-limit outcomes are `uncertain`;
- no fetched file content persists or logs.

### 17.3 Revalidation ProviderCalls

- exactly one durable identity per actual accepted revalidation attempt;
- crash before reserve, after reserve, after attempt start, after observed response, after results, and
  after normal artifact;
- ambiguous call never retransmits; observed call usage is exact; unknown usage stays unknown;
- malformed, missing, duplicate, unknown-ID, refusal, timeout, max-token, and safe-rejection behavior;
- token-fit suffix reduction marks removed findings uncertain;
- fixed/still-present/uncertain results survive restart without reinterpretation.

### 17.4 Deduplication and publication

- same primary fingerprint on later heads is not reposted;
- changed wording/title remains duplicate when primary identity is stable;
- same title with different evidence is not suppressed;
- secondary-only match never suppresses;
- cross-repository and cross-PR findings remain isolated;
- all-duplicate outcome creates no review POST and completes the Check deterministically;
- mixed new/duplicate artifacts publish only new findings;
- partial and configuration-warning conclusions remain deterministic;
- concurrent workers create one finding record/occurrence/projection.

### 17.5 Thread discovery and resolution

- complete GraphQL pagination and exact per-finding marker match;
- zero, duplicate, conflicting, foreign-review, incomplete, malformed, rate-limit, and timeout results;
- summary findings never claim a thread link or auto-resolution;
- crash after review POST reuses publication and performs discovery only;
- already-resolved reconciliation performs no mutation;
- known rejection, observed success, ambiguous mutation, complete reconciliation, and incomplete
  reconciliation matrices;
- ambiguous mutation is never repeated;
- only selected durable fixed findings resolve;
- still-present, uncertain, unselected, unlinked, and newer-head findings remain open.

### 17.6 Migration, retention, and regression

- migration `0001 -> 0002 -> 0003 -> 0004`, current, schema checks, and unsupported downgrade;
- 180-day boundary, open/indeterminate preservation, dry-run, batch, backup, rollback, and commit
  reconciliation;
- raw files/prompts/provider bodies/credentials/generated prose absent from lifecycle tables/logs;
- complete Phase 3 queue, Phase 4 ProviderCall/write ambiguity, and Phase 5 config restart suites;
- Ruff, strict Pyright, full pytest, pre-commit, diff/generated-schema checks;
- Docker build, Compose readiness, and production publishing rejection.

## 18. Manual sandbox validation

Use only `sraj0210/revio-phase4-sandbox` and the dedicated GitHub App.

1. Create a synthetic PR that produces one inline lifecycle-capable finding; record its fingerprint,
   marker, review ID, thread ID, and open state without exposing evidence content.
2. Push an unrelated commit with the issue unchanged; confirm the exact duplicate is not reposted and
   the existing thread remains open.
3. Move the issue to another line without changing identity evidence; confirm no duplicate post and a
   `still_present` result with no reply.
4. Change only generated-title wording conditions; confirm primary identity remains authoritative.
5. Fix the issue; confirm latest-head content was fetched, the result is durably `fixed`, and exactly the
   linked thread resolves.
6. Recreate the issue with different evidence under the same title; confirm it is a new finding.
7. Make current content unavailable/oversized or race a newer head; confirm `uncertain`/superseded and
   no resolution.
8. Exceed the selection limit; confirm selected count is 25 and every unselected thread stays open.
9. Inject an ambiguous thread mutation and incomplete discovery; confirm reconciliation-only behavior,
   no repeated mutation, and an open/indeterminate record when proof is unavailable.
10. Restart after durable selection, revalidation response, artifact, review publication, thread link,
    and resolution attempt; confirm no duplicate Messages call, review POST, or resolution mutation.
11. Search DB/logs for full file content, raw prompts/responses, credentials, and generated prose in
    fingerprints; confirm absence.

Close/delete synthetic branches and PRs after evidence is recorded. Keep the implementation PR open and
unmerged for review.

## 19. Validation gates

Before merge:

- approved Phase 6 plan, fingerprint vectors, and schema examples;
- migration upgrade/current/check;
- Ruff format/check, strict Pyright, full pytest, pre-commit, and diff checks;
- Phase 3 durability regression suite;
- complete Phase 4 initial/repair ProviderCall, artifact, GitHub ambiguity, and supersession matrices;
- Phase 5 configuration restart, exclusion, warning, rollout, and trusted-base matrices;
- Phase 6 fingerprint, selection, revalidation, dedup, discovery, resolution, restart, concurrency,
  retention, and privacy matrices;
- Docker build and isolated Compose readiness;
- production publishing rejection;
- real GitHub/Anthropic sandbox lifecycle demonstration.

## 20. Suggested implementation slices

1. Identity models, normalization, fingerprint vectors, diff hash, and schema/prompt version update.
2. Migration and durable finding/occurrence/revalidation/thread-operation repositories.
3. Deterministic prior selection and exact-latest-head bounded content collection.
4. Anthropic revalidation adapter contract and ambiguity-safe ProviderCall lifecycle.
5. Artifact-to-finding upsert, exact deduplication, and durable publication projection.
6. Per-finding markers, GitHub GraphQL thread discovery, and durable linkage.
7. Ambiguity-safe thread resolution and latest-head race protection.
8. Lifecycle retention, readiness/integrity checks, documentation, and full regression matrix.
9. Docker/Compose and real sandbox validation, followed by focused implementation review.

Every slice stays on one Phase 6 feature branch and one pull request. Do not merge until the complete
validation matrix and real sandbox demonstration pass.

## 21. Completion criteria

Phase 6 is complete only when:

- primary fingerprints contain only stable normalized fields and exclude generated prose;
- exact duplicates never create another finding comment;
- prior selection is durable, deterministic, and never exceeds 25;
- every resolution is backed by a durable selected `fixed` result from the exact latest current head;
- still-present, uncertain, unselected, unlinked, and indeterminate findings remain open;
- ambiguous Messages, review, discovery, and resolution outcomes never cause a repeated unsafe write;
- full file content remains ephemeral and absent from persistence/logging;
- Phase 3–5 safety guarantees remain green;
- production publishing remains prohibited;
- the real sandbox lifecycle sequence passes.
