from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx
import pytest
from starlette.responses import JSONResponse

from browser_use_mcp.config import ConfigurationError, Settings
from browser_use_mcp.security import (
    AuthenticationError,
    Authenticator,
    ClientSecretAuthMiddleware,
)
from tests.conftest import make_settings

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.types import Receive, Scope, Send

TOKEN_A = "client-a-very-long-random-secret-value-1234567890"
TOKEN_B = "client-b-very-long-random-secret-value-0987654321"


def test_storage_master_key_is_required() -> None:
    with pytest.raises(
        ConfigurationError, match="BROWSER_USE_MCP_STORAGE_MASTER_KEY is required"
    ):
        Settings.from_env({})


@pytest.mark.asyncio
async def test_bearer_tokens_create_isolated_namespaces(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, bearer_tokens=(TOKEN_A, TOKEN_B))
    authenticator = Authenticator(settings)

    first = await authenticator.principal({"Authorization": f"Bearer {TOKEN_A}"})
    second = await authenticator.principal({"authorization": f"Bearer {TOKEN_B}"})

    assert first.namespace_id != second.namespace_id
    assert first.tenant_id != second.tenant_id


@pytest.mark.asyncio
async def test_invalid_bearer_tokens_are_rejected(tmp_path: Path) -> None:
    authenticator = Authenticator(make_settings(tmp_path, bearer_tokens=(TOKEN_A,)))

    with pytest.raises(AuthenticationError):
        await authenticator.principal(None)
    with pytest.raises(AuthenticationError):
        await authenticator.principal({"Authorization": "Bearer too-short"})
    with pytest.raises(AuthenticationError):
        await authenticator.principal({"Authorization": f"Bearer {TOKEN_B}"})


@pytest.mark.asyncio
async def test_multiple_rotating_credentials_can_share_one_stable_tenant(
    tmp_path: Path,
) -> None:
    settings = make_settings(
        tmp_path,
        bearer_tokens=(TOKEN_A, TOKEN_B),
        tenant_ids=("customer", "customer"),
    )
    authenticator = Authenticator(settings)
    first = await authenticator.principal({"Authorization": f"Bearer {TOKEN_A}"})
    second = await authenticator.principal({"Authorization": f"Bearer {TOKEN_B}"})

    assert first == second


@pytest.mark.asyncio
async def test_unauthenticated_principal_is_stable(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    assert await Authenticator(settings).principal(None) == await Authenticator(
        settings
    ).principal(None)


@pytest.mark.asyncio
async def test_auth_middleware_protects_only_mcp_route(tmp_path: Path) -> None:
    authenticator = Authenticator(make_settings(tmp_path, bearer_tokens=(TOKEN_A,)))

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse({"ok": True})
        await response(scope, receive, send)

    middleware = ClientSecretAuthMiddleware(app, authenticator)
    transport = httpx.ASGITransport(app=middleware)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unauthorized = await client.post("/mcp", json={})
        authorized = await client.post(
            "/mcp", json={}, headers={"Authorization": f"Bearer {TOKEN_A}"}
        )
        health = await client.get("/healthz")

    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"] == "Bearer"
    assert authorized.status_code == 200
    assert health.status_code == 200


@pytest.mark.asyncio
async def test_auth_audit_events_do_not_log_bearer_tokens(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    authenticator = Authenticator(make_settings(tmp_path, bearer_tokens=(TOKEN_A,)))

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse({"ok": True})
        await response(scope, receive, send)

    middleware = ClientSecretAuthMiddleware(app, authenticator)
    transport = httpx.ASGITransport(app=middleware)
    with caplog.at_level(logging.INFO, logger="browser_use_mcp.audit"):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            await client.post("/mcp", headers={"Authorization": f"Bearer {TOKEN_A}"})
            await client.post("/mcp", headers={"Authorization": f"Bearer {TOKEN_B}"})

    assert "event=authentication outcome=succeeded" in caplog.text
    assert "event=authentication outcome=denied" in caplog.text
    assert TOKEN_A not in caplog.text
    assert TOKEN_B not in caplog.text
