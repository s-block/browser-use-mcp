FROM ghcr.io/astral-sh/uv:0.11.31@sha256:ecd4de2f060c64bea0ff8ecb182ddf46ba3fcccdc8a60cfdbaf20d1a047d7437 AS uv

FROM python:3.14.7-alpine3.23@sha256:6b8f06d04d5305c1d1288435388df9165ab41e681fae6439d6349d8053cc3f83 AS builder

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ ./src/

RUN uv sync --frozen --no-dev --no-editable

FROM python:3.14.7-alpine3.23@sha256:6b8f06d04d5305c1d1288435388df9165ab41e681fae6439d6349d8053cc3f83 AS runtime

LABEL org.opencontainers.image.source="https://github.com/s-block/browser-use-mcp" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="0.1.0"

WORKDIR /app
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv

RUN mkdir -m 0700 /data && chown 10001:10001 /data

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BROWSER_USE_MCP_STATE_DIR=/data

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import os, urllib.request; transport = os.environ.get('BROWSER_USE_MCP_TRANSPORT', 'http').strip().lower(); port = os.environ.get('BROWSER_USE_MCP_PORT', '8000'); transport == 'stdio' or urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=2).read()"]

USER 10001:10001

CMD ["/app/.venv/bin/browser-use-mcp"]
