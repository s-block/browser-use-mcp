from __future__ import annotations

import asyncio
import logging

import uvicorn

from browser_use_mcp.config import ServerTransport, Settings
from browser_use_mcp.server import create_app, create_mcp_server


async def serve(settings: Settings) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if settings.transport is ServerTransport.STDIO:
        await create_mcp_server(settings).run_stdio_async()
        return

    config = uvicorn.Config(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="info",
        access_log=False,
        proxy_headers=False,
        server_header=False,
    )
    await uvicorn.Server(config).serve()


def main() -> None:
    asyncio.run(serve(Settings.from_env()))
