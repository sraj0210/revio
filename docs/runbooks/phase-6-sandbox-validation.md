# Phase 6 sandbox validation — deduplication and finding lifecycle

Use only `sraj0210/revio-phase4-sandbox` and the dedicated **Revio Phase 4 Sandbox** GitHub App
(App ID `5450619`, installation `174693302`, repository `1328857147`, stable identity
`github:174693302:1328857147`). Never use a production repository and never enable production
publishing.

## Prerequisites

- Secret files under `~/.config/revio/`: `github-app.pem`, `github-webhook-secret`,
  `anthropic-api-key`, `publish-marker.key`.
- GitHub App permissions: pull requests read/write, checks read/write, contents read. Phase 6 adds
  no new REST permission; review-thread discovery and resolution use GraphQL with the same
  installation token.
- An isolated Compose project and a disposable volume. Apply migrations through
  `revio-db upgrade`; confirm `revio-db current` reports `0004_phase6_finding_lifecycle` and
  `revio-db check` passes.
- Start a fresh tunnel to local port 8000 and update the dedicated App webhook URL through the
  GitHub App API, preserving the existing webhook secret.

## Environment

```text
REVIO_ENVIRONMENT=sandbox
REVIO_REVIEW_ENABLED=true
REVIO_REVIEW_PUBLISH_ENABLED=true
REVIO_REVIEW_ROLLOUT_REPOSITORIES=github:174693302:1328857147
```

Publishing enabled in `sandbox` activates the full lifecycle; the same configuration must fail
startup in `production`.

## Scenarios

1. Open a synthetic PR that produces one inline lifecycle-capable finding. Record only safe IDs and
   digests: the finding-record id, the per-finding marker token, the review id, the thread node id,
   and the open lifecycle state. Never record evidence content.
2. Push an unrelated commit. Confirm the exact duplicate is not reposted (no new inline comment) and
   the existing thread stays open; the Check Run finishes successfully with
   `No new findings were published; matching findings are already tracked.`
3. Move the same issue to another line. Confirm it remains the same finding (same primary
   fingerprint), stays open, and is not reposted.
4. Change only the generated title wording. Confirm the primary fingerprint and the finding record
   are unchanged (wording cannot override identity).
5. Fix the issue. Confirm the latest-head file was fetched at the exact current head, the
   revalidation result is durably `fixed`, and exactly the linked thread is resolved.
6. Recreate a similar title with different evidence. Confirm it becomes a new finding.
7. Make the current file unavailable/oversized, or race a newer head during revalidation or before
   resolution. Confirm an `uncertain`/superseded outcome with no resolution.
8. Exceed 25 prior open findings. Confirm exactly 25 are selected and every unselected thread stays
   open.
9. Inject an ambiguous resolution mutation and an incomplete thread discovery. Confirm
   reconciliation-only behavior, no repeated mutation, and an open/indeterminate record when proof
   is unavailable.
10. Restart the worker after each durable boundary (selection, revalidation response, artifact,
    review publication, thread link, resolution attempt). Confirm no repeated Messages call, review
    POST, or resolution mutation.
11. Search the database and logs for full file content, raw prompts, provider response bodies,
    generated fingerprint prose, and secrets. Confirm absence. Only bounded (<=2 KiB) validated
    evidence, digests, versions, and safe IDs are present.

## Retention

Confirmed-closed finding history is pruned offline under the exclusive maintenance lock:

```text
revio-db prune-findings --dry-run
revio-db prune-findings --execute --backup-confirmed
```

Default eligibility is 180 days after resolution (`REVIO_RETENTION_CLOSED_FINDING_AGE_DAYS`). Open
and resolution-indeterminate findings and unresolved operations are never deleted.

## Teardown

Close and delete synthetic branches and PRs after recording evidence. Stop the temporary stack and
tunnel and remove the disposable volume. Keep the implementation PR open and unmerged for review.
