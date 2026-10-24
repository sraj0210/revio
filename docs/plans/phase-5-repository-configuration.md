# Phase 5 — Repository configuration and rollout basics

## Status

Approved and implemented on the Phase 5 feature branch. Merge remains subject to the validation gates below.

Phase 5 starts only after this plan is approved. It must preserve all Phase 3 durability and Phase 4
provider/write safety guarantees. Production review publishing remains prohibited; removing that gate
belongs to Phase 7.

## 1. Goal

Allow a repository to narrow Revio's administrator-approved review behavior through a strict,
versioned `.revio.yml` loaded from the exact current pull request base commit.

Phase 5 also adds an administrator rollout allow-list and an explicit policy controlling whether a
repository may opt out. Repository configuration never expands service capabilities or weakens
administrator security policy.

## 2. Frozen policy decisions

1. Draft pull requests continue to be reviewed by default, preserving Phase 4 behavior. A future
   administrator setting may disable draft review globally; `.revio.yml` cannot control it in v1.
2. Rollout is denied unless the stable repository identity is present in the administrator allow-list.
3. Repository opt-out is denied by default. `enabled: false` is honored only when the administrator
   explicitly permits repository opt-out.
4. Missing `.revio.yml` uses secure service defaults without a warning.
5. Invalid, oversized, unavailable, or malicious trusted-base configuration never suppresses an
   otherwise allowed review. Revio uses secure defaults and publishes a bounded visible warning.
6. The warning is prepended to the review summary and Check Run summary. It does not create a
   standalone GitHub object. If the review is otherwise partial, the existing neutral conclusion is
   retained; configuration invalidity alone does not change a successful review to neutral.
7. Repository configuration is read only from the exact current base SHA. A `.revio.yml` edit in a
   pull request cannot affect that same pull request.

## 3. Scope

Phase 5 includes:

- strict `.revio.yml` v1 schema;
- trusted-base file loading through the provider-neutral repository-content port;
- deterministic parsing, validation, normalization, and merge policy;
- durable normalized configuration resolution per durable queue job;
- administrator repository rollout allow-list;
- administrator repository-opt-out policy;
- repository-added protected path exclusions;
- repository-selected focus areas;
- repository-raised confidence thresholds;
- bounded visible warnings for invalid configuration;
- exact-base-SHA process-local caching;
- GitHub adapter and end-to-end sandbox validation.

## 4. Explicit non-goals

Phase 5 does not add:

- organization configuration or organization policy storage;
- arbitrary prompts or prompt fragments;
- arbitrary model IDs, providers, endpoints, or credentials;
- repository-controlled token, byte, file, finding, retry, or timeout ceilings;
- repository-controlled publication mode, marker behavior, or reconciliation behavior;
- repository-controlled webhook, queue, persistence, or retention behavior;
- negated exclusion patterns or a general-purpose policy language;
- agent tools, repository-wide search, finding lifecycle, or deduplication;
- production publishing;
- an administrative UI;
- Phase 6 functionality.

## 5. `.revio.yml` v1 schema

The only accepted top-level schema is:

```yaml
version: 1
enabled: true
review:
  focus_areas:
    - correctness
    - security
  exclude:
    - "docs/**"
    - "**/*.generated.py"
  confidence:
    summary: 0.70
    inline: 0.85
```

All fields other than `version` are optional. Unknown fields are errors. Duplicate mapping keys,
aliases, anchors, explicit YAML tags, merge keys, multiple documents, and non-UTF-8 input are errors.

### 5.1 Fields

- `version`: required integer, exactly `1`.
- `enabled`: optional strict boolean, default `true`.
- `review`: optional mapping.
- `review.focus_areas`: optional non-empty unique list drawn from `correctness`, `security`,
  `reliability`, and `maintainability`. Omission means all administrator-approved focus areas.
- `review.exclude`: optional unique list of repository-relative glob patterns.
- `review.confidence.summary`: optional finite number in `[0, 1]`.
- `review.confidence.inline`: optional finite number in `[0, 1]`.

The effective summary threshold must not exceed the effective inline threshold.

### 5.2 Bounds

- configuration file: 32 KiB maximum before parsing;
- focus areas: at most 4;
- exclusion patterns: at most 100;
- exclusion pattern: at most 256 Unicode code points;
- normalized warning codes: at most 8;
- visible warning text: service-owned and at most 512 characters.

The YAML parser runs in safe mode with aliases and custom tags disabled. Parsing is synchronous only
after the byte bound is enforced.

## 6. Exclusion semantics

Exclusions apply only to normalized repository-relative POSIX paths in the already bounded diff.

- Patterns use documented `pathlib.PurePosixPath`-compatible glob semantics implemented by a small
  deterministic matcher owned by the core.
- Absolute paths, empty segments, `.`/`..`, NUL, backslash, control characters, and negation (`!`) are
  invalid.
- Matching is case-sensitive; no platform filesystem semantics are consulted.
- Repository exclusions are additive. They are unioned with administrator protected exclusions and
  can never re-include an administrator-excluded path.
- `.revio.yml` is always protected and never sent as review content.
- A file excluded by effective policy is omitted before token counting and generation.
- If all changed files are excluded, Revio publishes a deterministic no-review-needed summary and
  completes the same Check Run successfully without an Anthropic Messages call.
- Provider collection completeness is evaluated before exclusions. Exclusions cannot make an
  incomplete provider response appear complete.

No raw paths or source snapshots are added to configuration persistence.

## 7. Confidence semantics

Repository thresholds may only narrow publication:

- effective summary threshold is `max(service_summary, repository_summary)`;
- effective inline threshold is `max(service_inline, repository_inline)`;
- effective summary must remain less than or equal to effective inline.

A repository cannot lower either administrator threshold. Invalid combinations fall back to service
defaults with a visible warning; they do not partially apply.

## 8. Focus-area semantics

Focus areas are a subset of the administrator-approved fixed vocabulary. They are normalized into the
provider-neutral `ReviewRequest` and rendered only by the reviewed, versioned core prompt resource.
Repository text is never inserted as prompt instructions.

An omitted list means all approved focus areas. An empty list is invalid rather than meaning “review
nothing.”

## 9. Administrator policy

Add settings with fail-closed defaults:

- `REVIO_REVIEW_ROLLOUT_REPOSITORIES`: comma-separated stable repository identities; empty means no
  repositories are eligible when publishing is enabled.
- `REVIO_REVIEW_ALLOW_REPOSITORY_OPTOUT=false`.
- `REVIO_REVIEW_DRAFTS_ENABLED=true`.
- `REVIO_REVIEW_PROTECTED_EXCLUSIONS`: optional administrator patterns merged with the mandatory
  `.revio.yml` exclusion.

The rollout identity is provider-neutral and includes provider ID, installation/account identity, and
provider repository ID. Repository names are diagnostic only and never authorize rollout.

Precedence, from strongest to weakest:

1. hard-coded safety invariants and production prohibition;
2. administrator security and rollout settings;
3. future mandatory organization/installation policy (not implemented in Phase 5);
4. valid trusted-base repository configuration;
5. service defaults.

`enabled: false` with opt-out denied produces warning code `repository_optout_denied` and continues
with defaults. With opt-out allowed it completes the durable queue job as `repository_disabled`
without creating a Check Run, calling Anthropic, or publishing a review.

A repository absent from the rollout allow-list completes as `rollout_disabled` before Phase 4
external writes. This is an administrator decision and does not produce a repository warning.

## 10. Trusted-base loading

The worker already re-fetches the current `ChangeRequest`. Phase 5 uses that same current object:

1. require event head SHA to match the current head SHA under Phase 3 rules;
2. evaluate administrator rollout from stable normalized repository identity;
3. fetch `.revio.yml` at exactly `current.base_sha` through `RepositoryContentReadPort.get_file`;
4. reject any adapter result whose resolved ref is not the requested immutable SHA;
5. enforce byte and encoding bounds before parsing;
6. parse and validate the whole document atomically;
7. merge only narrowing repository values into administrator policy;
8. durably persist the normalized resolution against the queue job before review-run creation,
   Check Run creation, or Anthropic work;
9. continue Phase 4 using the durable effective policy.

The head SHA is never used to load configuration. A branch name is never used as the ref.

GitHub `404` for `.revio.yml` means missing configuration. Authentication, permission, rate-limit,
timeout, truncation, ref mismatch, and malformed-response outcomes are not treated as missing; they
produce a bounded warning and secure-default resolution, subject to the existing provider retry rules
when the read is provably transient and no external review write has begun.

## 11. Durable model and migration

Add migration `0003_phase5_repository_configuration` with one normalized record per durable review
queue job:

`review_configurations`:

- `job_id` primary/foreign key;
- optional unique `review_run_id` foreign key, linked when an eligible review run is created;
- provider/repository stable identity components;
- exact base SHA;
- config status: `missing`, `valid`, `invalid`, `unavailable`, `optout_denied`;
- schema version when valid;
- normalized enabled decision;
- normalized focus-area JSON;
- normalized repository-exclusion JSON;
- effective summary and inline thresholds;
- stable warning-code JSON;
- SHA-256 digest of the bounded source bytes when a file was observed;
- normalized-policy digest;
- created/updated timestamps.

Never persist raw YAML, rendered prompts, source/diff content, provider response bodies, credentials,
or unsafe parser exception text.

Rollout-disabled and permitted-opt-out jobs terminate before a review run is created. Their queue
terminal reason records only the stable service-owned code.

### 11.1 Restart rules

- Once a normalized resolution is durable, restart reuses it and does not re-fetch or reinterpret
  `.revio.yml` for that queue job.
- A run with no durable resolution may fetch from the same exact base SHA again because the immutable
  read is idempotent.
- Configuration never changes a durable ProviderCall identity or authorizes another Messages call.
- A durable artifact remains authoritative; restart never regenerates it because configuration was
  later changed or deleted.
- Existing Phase 4 publishing/reconciliation states remain unchanged.

## 12. Warning contract

Warnings use stable internal codes and service-owned text. Initial codes:

- `config_invalid`;
- `config_oversized`;
- `config_unavailable`;
- `config_ref_mismatch`;
- `repository_optout_denied`.

Published text:

> Repository review configuration could not be applied; secure service defaults were used.

For denied opt-out:

> Repository opt-out is not permitted by administrator policy; the review used service defaults.

No parser input, path content, YAML value, provider error body, or exception detail enters the warning.
Warnings are included in the normalized artifact digest so restart and reconciliation publish the same
content.

## 13. Application integration

Add provider-neutral components:

- immutable `RepositoryReviewConfigurationV1` input model;
- immutable `EffectiveReviewPolicy` model;
- `RepositoryConfigurationLoader` for bounded trusted-base reads;
- `RepositoryPolicyResolver` for strict merge/precedence;
- `ConfigurationResolution` durable model;
- exclusion matcher with explicit limits;
- artifact warning composition owned by the core.

The queue processor performs rollout, trusted-base resolution, and permitted-opt-out decisions before
invoking the Phase 4 review executor. The review executor receives only a durable
`EffectiveReviewPolicy`; it does not parse YAML or inspect environment settings. When it creates the
review run, it links that run to the already-durable job configuration record.

The effective policy is applied in this order:

1. diff completeness validation;
2. administrator and repository exclusions;
3. existing service byte/line/file limits;
4. focus-area request construction;
5. existing token admission;
6. generation and local artifact guards;
7. effective confidence routing;
8. existing current-head validation and ambiguity-safe publication.

## 14. SCM adapter changes

Use the existing repository-content capability. GitHub must:

- request `/repos/{owner}/{repo}/contents/.revio.yml?ref={exact_base_sha}`;
- accept only a regular file with bounded Base64 content;
- validate the provider-reported object identity/ref where available;
- distinguish missing from permission/transient/malformed results;
- never follow content URLs or redirects to foreign origins;
- never log raw content;
- retain existing pagination, auth, rate-limit, and origin protections.

No GitHub-specific type crosses into the configuration core.

## 15. Caching

An optional process-local bounded cache keys by stable repository identity plus exact base SHA and
stores only normalized resolution. It has:

- maximum 1,000 entries;
- 15-minute TTL;
- deterministic least-recently-used eviction;
- no cross-process correctness requirement;
- no caching by branch name;
- no caching of raw YAML;
- no negative cache for transient/unavailable reads.

Durable per-run resolution, not the cache, is the restart authority.

## 16. Test plan

### Domain and parser

- missing and minimal valid v1 configuration;
- complete valid configuration;
- strict booleans/numbers and unknown fields;
- duplicate keys, aliases, anchors, tags, merge keys, and multiple documents;
- invalid UTF-8, NUL, control characters, oversized documents, and collection bombs;
- field-count, list-count, and string bounds;
- threshold boundaries, non-finite numbers, and summary/inline ordering;
- focus vocabulary, duplicates, and empty list;
- exclusion normalization, traversal, absolute paths, negation, backslash, and glob boundaries;
- deterministic normalized-policy digest and warning codes.

### Policy precedence

- absent/present rollout identity;
- stable ID authorization despite repository rename;
- repository opt-out allowed and denied;
- administrator-protected exclusions cannot be removed;
- repository thresholds can rise but never fall;
- repository focus areas narrow only;
- draft default and administrator draft disable;
- future mandatory policy seam remains stronger than repository policy.

### Trusted-base behavior

- config exists only on head: inactive for that PR;
- config merged to base: active for the next PR;
- exact base SHA passed to the adapter;
- changed base before processing uses newly re-fetched current base;
- stale event head remains superseded before config application;
- missing versus 404/403/429/5xx/timeout/malformed/ref-mismatch classification;
- cache isolation by provider, installation, repository, and base SHA;
- cache bounds, TTL, and transient-read behavior.

### Durability and restart

- migration upgrade `0001 -> 0002 -> 0003`, current, downgrade policy, and schema checks;
- normalized resolution commits before Check Run/Anthropic writes;
- crash before resolution safely reloads exact immutable base;
- crash after resolution reuses it without another config read;
- crash after artifact never regenerates or reinterprets configuration;
- concurrent workers create one resolution;
- no raw YAML or unsafe error text persists;
- Phase 4 ProviderCall and GitHub ambiguity matrices remain unchanged.

### Review behavior

- excluded files never reach token count or Messages payload;
- all-excluded diff performs no Messages call and completes deterministically;
- incomplete provider diff cannot be hidden by exclusions;
- focus areas reach the provider-neutral request only;
- confidence routing uses effective raised thresholds;
- invalid config reviews with defaults and visible warning;
- warning survives artifact restart/reconciliation;
- permitted opt-out and rollout-disabled paths perform no GitHub/Anthropic writes;
- Check Run conclusions and queue terminal reasons remain deterministic.

### Security and regression

- raw config, prompt, diff, provider response, and credentials absent from DB/logs;
- malicious YAML cannot instantiate objects or trigger network/filesystem reads;
- repository configuration cannot select model/provider/endpoint/prompt or raise ceilings;
- full Phase 3 durability and Phase 4 crash/restart suites;
- Ruff, strict Pyright, full pytest, pre-commit, and diff checks;
- Docker build and sandbox Compose readiness;
- production publishing remains rejected.

## 17. Manual sandbox validation

Use only `sraj0210/revio-phase4-sandbox` and the dedicated GitHub App.

1. Establish a base without `.revio.yml`; confirm secure defaults.
2. Open a PR adding `.revio.yml`; confirm that PR still uses its old base configuration.
3. Merge the config-only PR in the sandbox repository.
4. Open a later synthetic PR; confirm the merged config is loaded from its exact base SHA.
5. Verify a repository-added exclusion and raised confidence threshold.
6. Push invalid config to the sandbox base; confirm defaults plus bounded visible warning.
7. Test `enabled: false` with opt-out denied, then with administrator opt-out allowed.
8. Remove the repository from rollout; confirm no Check Run, Messages call, or review occurs.
9. Restart after durable resolution; confirm no config refetch changes the run and no provider call repeats.
10. Search DB/logs for raw YAML, source, prompts, provider bodies, and secrets.

Do not use production repositories or enable production publishing.

## 18. Validation gates

Before merge:

- approved plan and schema examples;
- migration upgrade/current/check;
- Ruff format/check;
- strict Pyright;
- full pytest and pre-commit;
- diff and generated-schema checks;
- Phase 3 durability regression suite;
- complete Phase 4 ProviderCall, artifact, GitHub ambiguity, and supersession regression matrices;
- configuration crash/restart and concurrency matrix;
- Docker build;
- sandbox Compose health/readiness;
- production publishing rejection;
- real GitHub sandbox trusted-base activation demonstration.

## 19. Suggested implementation slices

1. Domain schema, strict YAML parser, exclusion matcher, and policy resolver.
2. Administrator rollout/opt-out settings and stable repository identity policy.
3. SCM trusted-base loader and GitHub exact-ref content behavior.
4. Migration and durable normalized configuration repository.
5. Queue/review integration, warnings, exclusions, focus, and thresholds.
6. Restart/concurrency/security tests and documentation.
7. Docker/sandbox validation and focused implementation review.

Each slice must remain on one Phase 5 feature branch and one pull request. Do not merge until the
complete Phase 5 validation matrix and real sandbox demonstration pass.

## 20. Completion criteria

Phase 5 is complete only when:

- every configuration is loaded from the exact current base SHA;
- head-only configuration is proven inactive until merge;
- invalid configuration reviews with secure defaults and a visible bounded warning;
- rollout and opt-out decisions are controlled by stable administrator policy;
- repository settings only narrow behavior;
- normalized resolution is durable and restart-safe without raw YAML persistence;
- Phase 3 and Phase 4 safety guarantees remain green;
- production publishing remains prohibited;
- the real sandbox activation sequence passes.
