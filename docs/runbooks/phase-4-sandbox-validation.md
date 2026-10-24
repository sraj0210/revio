# Phase 4 sandbox validation

Phase 4 publication is a sandbox demonstration and must not be enabled in production.

1. Apply migrations with `revio-db upgrade`, then run `revio-db current` and
   `revio-worker check-ready`.
2. Configure a sandbox GitHub App with pull-request and checks write permission, Anthropic through a
   secret file, and a 32-byte-or-longer publication marker key through a secret file.
3. Validate execution-only mode with `REVIO_REVIEW_ENABLED=true` and publishing false. Confirm an
   artifact and usage disposition are durable and no GitHub write occurs.
4. In `REVIO_ENVIRONMENT=sandbox`, enable publishing. Review a synthetic flawed pull request and
   verify one Check Run ID and one marked `COMMENT` review pinned to the validated head.
5. Exercise incomplete input, anchor rejection, a changed head, and old-head neutral supersession.
6. Crash at every initial and repair ProviderCall boundary. A possibly transmitted call must never be
   repeated; a durable artifact must be reused.
7. Simulate timeouts after successful Check Run and review creation. Reconciliation must find the
   existing exact identity without another POST. Incomplete enumeration must fail closed.
8. Exhaust reconciliation and verify an unresolved audit state, neutral status where safely possible,
   and no claim that the provider object is absent.
9. Change the active marker key while an unresolved publish operation exists and verify readiness
   fails.

Never use private repository content or real credentials in fixtures, logs, screenshots, or reports.
