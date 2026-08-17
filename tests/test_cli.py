from __future__ import annotations

import asyncio
import base64
import os
import sys
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from browser_use_mcp import cli
from tests.conftest import make_settings

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_serve_runs_stdio_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(
        tmp_path,
        env_overrides={"BROWSER_USE_MCP_TRANSPORT": "stdio"},
    )
    run_stdio = AsyncMock()

    class StubServer:
        run_stdio_async = run_stdio

    monkeypatch.setattr(cli, "create_mcp_server", lambda _settings: StubServer())
    create_app = AsyncMock()
    monkeypatch.setattr(cli, "create_app", create_app)

    await cli.serve(settings)

    run_stdio.assert_awaited_once_with()
    create_app.assert_not_called()


@pytest.mark.asyncio
async def test_stdio_transport_completes_mcp_handshake(tmp_path: Path) -> None:
    env = dict(os.environ)
    env.update(
        {
            "BROWSER_USE_MCP_TRANSPORT": "stdio",
            "BROWSER_USE_MCP_STATE_DIR": str(tmp_path),
            "BROWSER_USE_MCP_STORAGE_MASTER_KEY": base64.b64encode(
                bytes(range(32))
            ).decode(),
            "BROWSER_USE_MCP_ALLOW_PRIVATE_NETWORK": "true",
        }
    )
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "browser_use_mcp"],
        env=env,
    )

    async with asyncio.timeout(10):
        async with stdio_client(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                tools = await session.list_tools()

    assert initialized.server_info.name == "browser-use-mcp"
    assert {tool.name for tool in tools.tools} == {
        "browser_session_start",
        "browser_session_close",
        "browser_navigate",
        "browser_act",
        "browser_observe",
        "browser_extract",
        "browser_control",
        "browser_profiles_list",
    }
