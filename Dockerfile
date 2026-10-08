# Production image: one always-on process serving the dashboard, the API and /mcp.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY alembic.ini ./
RUN uv sync --locked --no-dev

RUN useradd --create-home --uid 10001 app
USER app

ENV ENV=production \
    HOST=0.0.0.0 \
    PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz')"
CMD ["/app/.venv/bin/forecast-mcp"]
