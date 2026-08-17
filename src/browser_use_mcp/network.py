from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import SplitResult, urlsplit


class UnsafeURLError(ValueError):
    """Raised when an outbound URL crosses the configured network boundary."""


async def validate_outbound_url(url: str, *, allow_private_network: bool) -> str:
    parsed = urlsplit(url)
    _validate_parsed_url(parsed)
    if allow_private_network:
        return url

    hostname = parsed.hostname
    if hostname is None:
        raise UnsafeURLError("URL must include a host")
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        addresses = await _resolve(hostname, parsed.port)
    if not addresses or any(_is_private(address) for address in addresses):
        raise UnsafeURLError(
            "private and non-routable network destinations are disabled"
        )
    return url


def _validate_parsed_url(parsed: SplitResult) -> None:
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise UnsafeURLError("URL must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeURLError("credentials are not allowed in URLs")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise UnsafeURLError("URL contains an invalid port") from exc


async def _resolve(
    hostname: str, port: int | None
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    loop = asyncio.get_running_loop()
    try:
        async with asyncio.timeout(5):
            results = await loop.getaddrinfo(
                hostname,
                port or 443,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
    except (OSError, TimeoutError) as exc:
        raise UnsafeURLError("URL host could not be resolved") from exc
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for result in results:
        address = ipaddress.ip_address(result[4][0])
        if address not in addresses:
            addresses.append(address)
    return addresses


def _is_private(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not address.is_global
