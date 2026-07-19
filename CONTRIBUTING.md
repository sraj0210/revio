# Contributing to Revio

Thank you for contributing.

## Workflow

1. Create a focused feature branch from the latest `main`.
2. Do not commit directly to `main`.
3. Add or update tests and documentation with the change.
4. Run all local quality checks.
5. Use focused commits and open a pull request.
6. Do not merge a phase until it has been explicitly approved.

Phase boundaries in the architecture plan are mandatory. Do not implement future providers or phases prematurely.

## Development setup

```bash
uv sync --frozen --all-groups
uv run pre-commit install
```

## Required checks

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
docker compose build
docker compose up --detach --wait
docker compose down
```

## Pull requests

Explain the problem, scope, tests, security implications, and known limitations. Keep fixtures synthetic and sanitized. Never commit credentials, internal URLs, private webhook payloads, or proprietary source code.

By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
