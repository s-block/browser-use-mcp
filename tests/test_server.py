from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from browser_use_mcp.server import create_mcp_server
from tests.conftest import make_settings

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_mcp_surface_is_small_and_semantic(tmp_path: Path) -> None:
    server = create_mcp_server(make_settings(tmp_path))

    tools = await server.list_tools()

    assert {tool.name for tool in tools} == {
        "browser_session_start",
        "browser_session_close",
        "browser_navigate",
        "browser_act",
        "browser_observe",
        "browser_extract",
        "browser_control",
        "browser_profiles_list",
    }
    control = next(tool for tool in tools if tool.name == "browser_control")
    assert "javascript" not in str(control.input_schema).lower()
