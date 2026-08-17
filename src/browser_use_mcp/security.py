from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from browser_use_mcp.audit import audit_event
from browser_use_mcp.config import AuthMode, Settings

if TYPE_CHECKING:
    from collections.abc import Mapping

    from starlette.types import ASGIApp, Receive, Scope, Send

_MINIMUM_TOKEN_CHARACTERS = 32
_MAXIMUM_TOKEN_CHARACTERS = 1_024


class AuthenticationError(ValueError):
    """Raised for absent or invalid client credentials."""


@dataclass(frozen=True, slots=True)
class Principal:
    tenant_id: str
    namespace_id: str


class Authenticator:
    """Maps accepted bearer credentials to stable application tenant IDs."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._credentials = tuple(
            (bytes.fromhex(item.token_hash), item.tenant_id)
            for item in settings.client_credentials
        )

    async def principal(self, headers: Mapping[str, str] | None) -> Principal:
        if self._settings.auth_mode is AuthMode.NONE:
            return _principal("local")

        token = _bearer_token(headers)
        if not _MINIMUM_TOKEN_CHARACTERS <= len(token) <= _MAXIMUM_TOKEN_CHARACTERS:
            raise AuthenticationError("invalid bearer token")
        token_bytes = token.encode("utf-8")
        digest = hashlib.sha256(token_bytes, usedforsecurity=True).digest()
        tenant_id = None
        for allowed, owner in self._credentials:
            if hmac.compare_digest(digest, allowed):
                tenant_id = owner
        if tenant_id is None:
            raise AuthenticationError("invalid bearer token")
        return _principal(tenant_id)


class ClientSecretAuthMiddleware:
    """Protects MCP HTTP routes with the configured bearer client secret."""

    def __init__(
        self,
        app: ASGIApp,
        authenticator: Authenticator,
        *,
        protected_path: str = "/mcp",
    ) -> None:
        self._app = app
        self._authenticator = authenticator
        self._protected_path = protected_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and str(scope.get("path", "")).startswith(
            self._protected_path
        ):
            try:
                principal = await self._authenticator.principal(Headers(scope=scope))
            except AuthenticationError:
                audit_event(
                    "authentication",
                    outcome="denied",
                    reason="invalid_credential",
                )
                response = JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
            audit_event(
                "authentication",
                outcome="succeeded",
                tenant_namespace=principal.namespace_id,
            )
        await self._app(scope, receive, send)


def _bearer_token(headers: Mapping[str, str] | None) -> str:
    if headers is None:
        raise AuthenticationError("bearer authentication requires HTTP transport")
    authorization = next(
        (value for key, value in headers.items() if key.lower() == "authorization"),
        None,
    )
    if authorization is None:
        raise AuthenticationError("missing bearer token")
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token.strip():
        raise AuthenticationError("invalid authorization header")
    return token.strip()


def _principal(tenant_id: str) -> Principal:
    namespace = hashlib.sha256(
        f"browser-use-mcp/tenant/{tenant_id}".encode(), usedforsecurity=True
    ).hexdigest()
    return Principal(tenant_id=tenant_id, namespace_id=namespace)
