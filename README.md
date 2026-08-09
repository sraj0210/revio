# Revio

Revio is a provider-neutral code-review platform. Phase 3 adds durable webhook and queue orchestration around the read-only GitHub adapter. AI review generation and GitHub repository writes remain unimplemented.

The approved architecture and phased roadmap are documented in [the implementation plan](docs/architecture/implementation-plan.md).

## Current capabilities

- A Python 3.12 package using the `src` layout
- A minimal FastAPI application
- `GET /health` for process liveness
- `GET /ready` for SQLite capability, migration, and integrity readiness
- Provider-neutral identities, review models, capability descriptions, and segregated ports
- Administrator-controlled provider and model-profile registries
- A fake-only review orchestration contract harness
- A read-only GitHub App adapter and sandbox validation CLI
- Signed sandbox or durable GitHub webhook ingress selected by one mode setting
- SQLite WAL delivery, installation-state, queue-job, and attempt persistence
- Atomic leasing, retry/dead-job recovery, current-head validation, and stale supersession
- Offline terminal-history retention with permanent delivery tombstones
- Local development and container tooling
- Automated formatting, linting, type checking, and tests

`/health` intentionally checks process liveness only. `/ready` checks the mandatory SQLite 3.35 capability, migration revision, foreign keys, and delivery/tombstone integrity.

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Docker with Docker Compose for container validation

## Local development

Install all locked development dependencies:

```bash
uv sync --frozen --all-groups
```

Apply the migration, then run the API and worker separately:

```bash
REVIO_DATABASE_PATH=/absolute/path/revio.db uv run revio-db upgrade
REVIO_DATABASE_PATH=/absolute/path/revio.db uv run revio-worker check-ready
REVIO_DATABASE_PATH=/absolute/path/revio.db uv run revio-worker run
uv run uvicorn revio.main:app --reload
```

The service listens at <http://127.0.0.1:8000>. Check liveness with:

```bash
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/ready
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

Phase 3 returns `202` in durable mode only after the delivery and any canonical active job commit atomically. Opened/synchronize events at one head coalesce while active; reopened is occurrence-specific. Workers check durable installation suspension/deletion before the only provider read, fetch current pull-request metadata, and never fetch a diff in Phase 3.

Durable ingress uses `503 {"status":"not_accepted"}` only when persistence is confirmed absent. `500 {"status":"indeterminate"}` means commit disposition could not be established; an exact later redelivery is safe because delivery identity and payload hash remain idempotently enforced. Sanitized integrity failures use `500 {"status":"integrity_error"}` and make readiness fail while contradictory durable facts remain.

Exactly one worker process is enforced by a process-lifetime lock next to the SQLite
database. `revio-worker check-ready` is a pre-start check; a running worker exposes
the separate `revio-worker health` command for container health checks.

AI calls, publishing, review comments, statuses, Check Runs, `.revio.yml`, PostgreSQL, Redis, and multiple workers remain out of scope. The only GitHub POST is still installation-token exchange.

When `REVIO_GITHUB_ENABLED=true`, application bootstrap validates the RSA key and registers the GitHub read and repository-content ports. Operational worker readiness requires this adapter. An intentionally idle local-development worker additionally requires `REVIO_GITHUB_ALLOW_IDLE_WORKER=true`; production and durable modes reject it. The sandbox CLI uses the same adapter-private composition factory. Changed-file and tree output includes explicit completeness values; any value other than `complete` is partial and must not be interpreted as a complete repository view.

## Contributing and security

Read [CONTRIBUTING.md](CONTRIBUTING.md) before proposing changes. Report vulnerabilities according to [SECURITY.md](SECURITY.md). Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
