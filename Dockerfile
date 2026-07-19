FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN groupadd --system revio \
    && useradd --system --gid revio --home-dir /app revio

COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

USER revio

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD ["/app/.venv/bin/python", "-c", "import json, urllib.request; response = urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2); assert response.status == 200; assert json.load(response) == {'status': 'ok'}"]

CMD ["/app/.venv/bin/uvicorn", "revio.main:app", "--host", "0.0.0.0", "--port", "8000"]
