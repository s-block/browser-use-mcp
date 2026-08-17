from __future__ import annotations

import pytest

from browser_use_mcp.network import UnsafeURLError, validate_outbound_url


@pytest.mark.asyncio
async def test_private_network_destinations_are_blocked_by_default() -> None:
    with pytest.raises(UnsafeURLError):
        await validate_outbound_url(
            "http://127.0.0.1/admin", allow_private_network=False
        )


@pytest.mark.asyncio
async def test_private_network_destinations_can_be_explicitly_enabled() -> None:
    assert (
        await validate_outbound_url(
            "http://127.0.0.1/admin", allow_private_network=True
        )
        == "http://127.0.0.1/admin"
    )


@pytest.mark.asyncio
async def test_credentials_in_urls_are_rejected() -> None:
    with pytest.raises(UnsafeURLError):
        await validate_outbound_url(
            "https://user:password@example.com",  # pragma: allowlist secret
            allow_private_network=True,
        )


@pytest.mark.asyncio
async def test_invalid_ports_are_rejected_as_unsafe_urls() -> None:
    with pytest.raises(UnsafeURLError, match="invalid port"):
        await validate_outbound_url(
            "https://example.com:invalid",
            allow_private_network=True,
        )
