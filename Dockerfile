FROM ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 AS uv

FROM python:3.13.14-alpine3.23@sha256:9fdbf2e3e82628351513560b121e2ee6ce31cac212be9e070c5a5e2769fb5e76 AS builder

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ ./src/

RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13.14-alpine3.23@sha256:9fdbf2e3e82628351513560b121e2ee6ce31cac212be9e070c5a5e2769fb5e76 AS runtime

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
