# Revio

Revio is a provider-neutral, AI-powered code-review platform. The project is in its engineering-foundation phase; provider integrations and review behavior are not implemented yet.

The approved architecture and phased roadmap are documented in [the implementation plan](docs/architecture/implementation-plan.md).

## Current capabilities

- A Python 3.12 package using the `src` layout
- A minimal FastAPI application
- `GET /health` for process liveness
- Provider-neutral identities, review models, capability descriptions, and segregated ports
- Administrator-controlled provider and model-profile registries
- A fake-only review orchestration contract harness
- A read-only GitHub App adapter and sandbox validation CLI
- A signed, non-durable GitHub webhook endpoint for local/sandbox validation only
- Local development and container tooling
- Automated formatting, linting, type checking, and tests

`/health` intentionally checks process liveness only. It does not check databases, providers, queues, or other dependencies.

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Docker with Docker Compose for container validation

## Local development

Install all locked development dependencies:

```bash
uv sync --frozen --all-groups
```

Run the application:

```bash
uv run uvicorn revio.main:app --reload
```

The service listens at <http://127.0.0.1:8000>. Check liveness with:

```bash
curl --fail http://127.0.0.1:8000/health
```

Run the complete local validation suite:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
docker compose build
docker compose up --detach --wait
docker compose down
```

## Configuration

Copy `.env.example` to `.env` for optional local overrides. The example contains no credentials. Revio does not load repository-level configuration in Phase 2.

## Project status

Phase 2 provides read-only GitHub App authentication, SCM reads, and sandbox webhook normalization. The webhook is non-durable: a `202` response does not mean the event was stored. Enabling it in production is prohibited until Phase 3 adds atomic delivery and queue-job persistence. AI calls, persistence, queues, publishing, review comments, statuses, and Check Runs remain out of scope.

When `REVIO_GITHUB_ENABLED=true`, application bootstrap validates the RSA key and registers the GitHub read and repository-content ports. Disabled GitHub configuration constructs no adapter and requires no credentials. The sandbox CLI uses the same adapter-private composition factory. Changed-file and tree output includes explicit completeness values; any value other than `complete` is partial and must not be interpreted as a complete repository view.

## Contributing and security

Read [CONTRIBUTING.md](CONTRIBUTING.md) before proposing changes. Report vulnerabilities according to [SECURITY.md](SECURITY.md). Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
