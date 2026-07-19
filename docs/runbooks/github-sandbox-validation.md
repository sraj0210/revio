# GitHub sandbox validation

Use a dedicated private sandbox repository and a GitHub App installed only on that repository. Grant Pull requests read and Contents read.

1. Configure `REVIO_ENVIRONMENT=sandbox` and enable the GitHub adapter.
2. Leave the sandbox webhook disabled initially.
3. Run `uv run revio-github-sandbox --installation-id ID --repository OWNER/REPO --pull-request NUMBER`.
4. Confirm current PR/base/head metadata, bounded changed-file metadata, and bounded non-recursive tree metadata.
   `changed_files_completeness` and `tree.completeness` must be `complete` before treating the listing as exhaustive. Each changed file includes a `patch_state`; unavailable, malformed, binary, and provider-truncated patches are explicit.
5. Add `--path PATH --ref SHA` to validate file access. Source content is never printed.
6. Enable the sandbox webhook only for signed fixture or tunnel validation.
7. Confirm no GitHub write operation occurs.
8. Disable the route and revoke sandbox credentials after validation.

Do not treat this workflow as production deployment.
