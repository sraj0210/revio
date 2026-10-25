# Venaroq

**Self-hostable AI code review for GitHub pull requests — powered by Claude or GPT, and built to stay quiet, safe, and yours.**

[![CI](https://github.com/sraj0210/venaroq/actions/workflows/ci.yml/badge.svg)](https://github.com/sraj0210/venaroq/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)

Venaroq reviews your pull requests the way a careful teammate would: it reads the diff, flags real
correctness, security, reliability, and maintainability problems, and posts them back to GitHub as a
check and inline comments. It runs entirely on your own infrastructure, talks only to the AI provider
you choose, and is engineered to **fail closed** — no surprise comments, no leaked secrets, no
duplicate noise on every push.

---

## Why Venaroq

- 🔒 **Self-hosted and private.** Your code and diffs never leave your infrastructure except to the
  one AI provider you configure. No Venaroq-operated cloud, no telemetry, no data retention you didn't
  ask for.
- 🤝 **Bring your own model.** Use Anthropic Claude or OpenAI GPT interchangeably — switch providers
  with a single setting. Nothing in the review pipeline is tied to a vendor.
- 🎯 **Signal over noise.** Findings are deduplicated by a stable fingerprint, so the same issue is
  never posted twice across force-pushes, and findings are automatically resolved once your code
  fixes them.
- 🛡️ **Safe by construction.** Reviewing is read-only until you explicitly opt a repository in.
  Publishing is gated behind an allow-list and an explicit production confirmation. Ambiguous AI or
  network outcomes are never retried in a way that could double-post.
- 🧩 **Configurable per repository.** A committed `.venaroq.yml` lets each repo tune the review — within
  the limits administrators set.
- 📈 **Production-ready operations.** Structured JSON logs with secret redaction, Prometheus metrics
  on a private bind, per-source rate limiting, graceful shutdown, and a durable work queue that
  survives restarts.

## What you get

Open a pull request on an enabled repository and Venaroq will:

1. Post a **Check Run** that reflects review status on the PR.
2. Add a concise **review summary** plus **inline comments** on the exact lines that need attention.
3. Report only **actionable defects** — it prefers staying silent to guessing.
4. **Skip duplicates** it already raised, and **resolve** comment threads once the underlying issue
   is fixed in a later push.

## How it works

```
GitHub PR  ──webhook──▶  Venaroq API  ──persist──▶  Durable queue (SQLite)
                                                        │
                                                        ▼
                                              Worker: fetch diff ──▶ AI review (Claude / GPT)
                                                        │
                                                        ▼
                             GitHub Check Run + review comments  ◀── dedupe + lifecycle
```

The API verifies and durably records each webhook, then returns immediately. A single background
worker leases jobs one at a time, fetches the diff, asks your chosen model for a structured review,
deduplicates against prior findings, and publishes the result. Every external write is reconciled so
a crash or timeout can never create a duplicate review.

## Quickstart

### 1. Run the service

```bash
git clone https://github.com/sraj0210/venaroq.git
cd venaroq
docker compose up --build
```

The API comes up on <http://127.0.0.1:8000>. Confirm it's alive:

```bash
curl --fail http://127.0.0.1:8000/health   # process is up
curl --fail http://127.0.0.1:8000/ready    # database + migrations healthy
```

Out of the box, review execution and publishing are **off**. The steps below turn them on.

### 2. Create a GitHub App

Create a GitHub App for your org with:

- **Permissions:** Pull requests — Read & write · Checks — Read & write · Contents — Read ·
  Metadata — Read
- **Subscribe to events:** Pull request
- **Webhook URL:** `https://<your-host>/webhooks/github` with a strong webhook secret

Install the App on the repositories you want reviewed, and download its private key (`.pem`).

### 3. Configure and launch

Copy the example environment and fill in your credentials (keep secrets in files, not inline):

```bash
cp .env.example .env
```

A minimal review-enabled configuration:

```bash
VENAROQ_ENVIRONMENT=sandbox
VENAROQ_GITHUB_ENABLED=true
VENAROQ_GITHUB_WEBHOOK_MODE=durable
VENAROQ_GITHUB_APP_ID=<your-app-id>
VENAROQ_GITHUB_PRIVATE_KEY_FILE=/secrets/github-app.pem
VENAROQ_GITHUB_WEBHOOK_SECRET=<your-webhook-secret>

VENAROQ_REVIEW_ENABLED=true
VENAROQ_REVIEW_PUBLISH_ENABLED=true

# Pick one provider:
VENAROQ_ANTHROPIC_ENABLED=true
VENAROQ_ANTHROPIC_API_KEY_FILE=/secrets/anthropic.key
# ...or OpenAI instead:
# VENAROQ_OPENAI_ENABLED=true
# VENAROQ_OPENAI_API_KEY_FILE=/secrets/openai.key
# VENAROQ_REVIEW_MODEL_ALIAS=openai-review-default

# Publishing needs a signing key and an explicit per-repo allow-list:
VENAROQ_PUBLISH_MARKER_KEY_FILE=/secrets/marker.key
VENAROQ_REVIEW_ROLLOUT_REPOSITORIES=github:<installation-id>:<repository-id>
```

Generate the publish signing key once (any file of 32+ random bytes works):

```bash
openssl rand -hex 32 > /secrets/marker.key
```

Bring the stack up, then open a pull request on an enabled repository:

```bash
docker compose up --build --detach
```

> **Going to production?** Set `VENAROQ_ENVIRONMENT=production`. Production publishing is fail-closed:
> it additionally requires `VENAROQ_PRODUCTION_PUBLISH_CONFIRMED=true`, a non-empty rollout allow-list,
> and file-based (not inline) secret sources. Terminate TLS at a reverse proxy in front of Venaroq.

## Configuration

Venaroq is configured entirely through environment variables (all prefixed `VENAROQ_`). The most common:

| Variable | Description |
| --- | --- |
| `VENAROQ_ENVIRONMENT` | `local`, `sandbox`, or `production`. Production enforces the strictest gates. |
| `VENAROQ_GITHUB_ENABLED` | Enable the GitHub App adapter. |
| `VENAROQ_GITHUB_APP_ID` / `VENAROQ_GITHUB_PRIVATE_KEY_FILE` | GitHub App credentials. |
| `VENAROQ_GITHUB_WEBHOOK_SECRET` | Secret used to verify incoming webhook signatures. |
| `VENAROQ_REVIEW_ENABLED` | Turn on AI review execution. |
| `VENAROQ_REVIEW_PUBLISH_ENABLED` | Allow Venaroq to post checks and comments back to GitHub. |
| `VENAROQ_REVIEW_MODEL_ALIAS` | Choose the model/provider (`review-default` = Claude, `openai-review-default` = GPT). |
| `VENAROQ_ANTHROPIC_ENABLED` / `VENAROQ_ANTHROPIC_API_KEY_FILE` | Enable and authenticate Claude. |
| `VENAROQ_OPENAI_ENABLED` / `VENAROQ_OPENAI_API_KEY_FILE` | Enable and authenticate GPT. |
| `VENAROQ_REVIEW_ROLLOUT_REPOSITORIES` | Allow-list of `provider:installation-id:repository-id`. Empty denies all publishing. |
| `VENAROQ_PUBLISH_MARKER_KEY_FILE` | Signing key for Venaroq's own comment markers (dedup + lifecycle). |
| `VENAROQ_METRICS_BIND` | Internal bind for the Prometheus `/metrics` endpoint (empty = disabled). |

See [`.env.example`](.env.example) for the complete, documented list.

## Per-repository configuration

Teams can tune reviews by committing a `.venaroq.yml` to a repository's default branch. Repository
settings can only **narrow** what administrators allow — they can never raise limits, enable disabled
providers, remove protected exclusions, or override the rollout allow-list. Missing or invalid
configuration falls back to safe defaults and never silently skips a review.

## Run from source

Requires [Python 3.12](https://www.python.org/) and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --frozen --all-groups

# Apply migrations, then run the worker and API (separate processes):
VENAROQ_DATABASE_PATH=/absolute/path/venaroq.db uv run venaroq-db upgrade
VENAROQ_DATABASE_PATH=/absolute/path/venaroq.db uv run venaroq-worker run
uv run uvicorn venaroq.main:app --reload
```

Run the full check suite the CI uses:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
```

## Security

- **Secrets stay out of logs and the database.** Logs are structured JSON with a defensive redactor;
  raw diffs, prompts, provider responses, and credentials are never persisted.
- **Credentials from files, not inline, in production.** GitHub keys, provider API keys, and the
  marker key must be file-mounted secrets in production.
- **No ambiguous retries.** If an AI call or external write might have taken effect, Venaroq never
  blindly retries it — correctness is preferred over coverage.

Found a vulnerability? Please follow [SECURITY.md](SECURITY.md). Do not open a public issue for
security reports.

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) to get started, and please follow
our [Code of Conduct](CODE_OF_CONDUCT.md). The architecture and design decisions are documented under
[`docs/`](docs/).

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
