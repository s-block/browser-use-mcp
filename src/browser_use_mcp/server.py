from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from starlette.responses import JSONResponse
from starlette.routing import Route

from browser_use_mcp.backend import SteelStagehandBackend
from browser_use_mcp.config import Settings
from browser_use_mcp.llm import LLMConfigResolver, OpenAICompatibleStagehandModel
from browser_use_mcp.models import (
    ActResult,
    BrowserControlCommand,
    ControlResult,
    ControlValue,
    ExtractResult,
    Instruction,
    NavigationResult,
    NavigationURL,
    ObserveResult,
    ProfileListResult,
    ProfileName,
    Selector,
    SessionClosed,
    SessionId,
    SessionStarted,
)
from browser_use_mcp.profiles import ProfileLeaseManager, ProfileStore
from browser_use_mcp.security import (
    Authenticator,
    ClientSecretAuthMiddleware,
    Principal,
)
from browser_use_mcp.sessions import BrowserSessionService

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.types import ASGIApp


@dataclass(slots=True)
class AppRuntime:
    authenticator: Authenticator
    llm_configs: LLMConfigResolver
    sessions: BrowserSessionService
    http_client: httpx.AsyncClient

    async def close(self) -> None:
        try:
            await self.sessions.shutdown()
        finally:
            await self.http_client.aclose()


def create_mcp_server(
    settings: Settings, authenticator: Authenticator | None = None
) -> MCPServer[AppRuntime]:
    auth = authenticator or Authenticator(settings)

    @asynccontextmanager
    async def lifespan(_server: MCPServer[AppRuntime]) -> AsyncIterator[AppRuntime]:
        profiles = await ProfileStore.open(
            settings.state_dir, settings.storage_master_key
        )
        http_client = httpx.AsyncClient(
            follow_redirects=False,
            limits=httpx.Limits(max_connections=settings.max_sessions * 2),
        )
        try:
            model = OpenAICompatibleStagehandModel(
                http_client,
                allow_private_network=settings.allow_private_network,
                timeout_seconds=settings.operation_timeout_seconds,
                max_response_bytes=settings.max_llm_response_bytes,
            )
            backend = SteelStagehandBackend(
                steel_api_key=settings.steel_api_key,
                steel_base_url=settings.steel_base_url,
                session_timeout_ms=settings.session_timeout_ms,
                session_policy=settings.steel_session,
                model=model,
                allow_private_network=settings.allow_private_network,
            )
            runtime = AppRuntime(
                authenticator=auth,
                llm_configs=LLMConfigResolver(settings.llm),
                sessions=BrowserSessionService(
                    settings,
                    backend,
                    profiles,
                    ProfileLeaseManager(
                        settings.state_dir, settings.profile_lock_timeout_seconds
                    ),
                ),
                http_client=http_client,
            )
        except BaseException:
            await profiles.aclose()
            await http_client.aclose()
            raise
        try:
            yield runtime
        finally:
            await runtime.close()

    mcp = MCPServer[AppRuntime](
        name="browser-use-mcp",
        title="Browser Use MCP",
        description="Authorized Stagehand browser automation on Steel Chromium",
        instructions=(
            "Start a temporary or named persistent session, navigate it, then use "
            "semantic Stagehand tools or the constrained browser_control escape hatch. "
            "Always close sessions when finished."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    @mcp.tool(
        description=(
            "Start a temporary Steel browser session, or a writable persistent session "
            "when profile_name is supplied."
        ),
        annotations=ToolAnnotations(open_world_hint=True),
    )
    async def browser_session_start(
        ctx: Context[AppRuntime], profile_name: ProfileName | None = None
    ) -> SessionStarted:
        runtime, principal = await _request(ctx)
        return await runtime.sessions.start(principal, profile_name)

    @mcp.tool(
        description="Release a browser session and its persistent-profile lease.",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    async def browser_session_close(
        session_id: SessionId, ctx: Context[AppRuntime]
    ) -> SessionClosed:
        runtime, principal = await _request(ctx)
        return await runtime.sessions.close(principal, session_id)

    @mcp.tool(
        description="Navigate the active page to an authorized HTTP(S) URL.",
        annotations=ToolAnnotations(open_world_hint=True),
    )
    async def browser_navigate(
        session_id: SessionId,
        url: NavigationURL,
        ctx: Context[AppRuntime],
    ) -> NavigationResult:
        runtime, principal = await _request(ctx)
        return await runtime.sessions.navigate(principal, session_id, url)

    @mcp.tool(
        description="Use Stagehand to perform one semantic browser action.",
        annotations=ToolAnnotations(destructive_hint=True, open_world_hint=True),
    )
    async def browser_act(
        session_id: SessionId,
        instruction: Instruction,
        ctx: Context[AppRuntime],
    ) -> ActResult:
        runtime, principal = await _request(ctx)
        llm_config = runtime.llm_configs.resolve(ctx.headers)
        return await runtime.sessions.act(
            principal, session_id, instruction, llm_config
        )

    @mcp.tool(
        description="Ask Stagehand for relevant actions on the active page.",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    async def browser_observe(
        session_id: SessionId,
        ctx: Context[AppRuntime],
        instruction: Instruction | None = None,
    ) -> ObserveResult:
        runtime, principal = await _request(ctx)
        llm_config = runtime.llm_configs.resolve(ctx.headers)
        return await runtime.sessions.observe(
            principal, session_id, instruction, llm_config
        )

    @mcp.tool(
        description=(
            "Use Stagehand to extract a bounded text result from the active page."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    async def browser_extract(
        session_id: SessionId,
        instruction: Instruction,
        ctx: Context[AppRuntime],
    ) -> ExtractResult:
        runtime, principal = await _request(ctx)
        llm_config = runtime.llm_configs.resolve(ctx.headers)
        return await runtime.sessions.extract(
            principal, session_id, instruction, llm_config
        )

    @mcp.tool(
        description=(
            "Constrained lower-level control: click, fill, type, key_press, text, "
            "title, or url. Arbitrary JavaScript is intentionally unavailable."
        ),
        annotations=ToolAnnotations(destructive_hint=True, open_world_hint=True),
    )
    async def browser_control(
        session_id: SessionId,
        command: BrowserControlCommand,
        ctx: Context[AppRuntime],
        selector: Selector | None = None,
        value: ControlValue | None = None,
    ) -> ControlResult:
        runtime, principal = await _request(ctx)
        return await runtime.sessions.control(
            principal, session_id, command, selector, value
        )

    @mcp.tool(
        description="List named persistent profiles in the authenticated namespace.",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def browser_profiles_list(
        ctx: Context[AppRuntime],
    ) -> ProfileListResult:
        runtime, principal = await _request(ctx)
        return await runtime.sessions.list_profiles(principal)

    return mcp


def create_app(settings: Settings) -> ASGIApp:
    authenticator = Authenticator(settings)
    mcp = create_mcp_server(settings, authenticator)
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(settings.allowed_hosts),
        allowed_origins=list(settings.allowed_origins),
    )
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=1_048_576,
        transport_security=transport_security,
        host=settings.host,
    )
    app.routes.append(Route("/healthz", _health, methods=["GET"]))
    return ClientSecretAuthMiddleware(app, authenticator)


async def _request(
    ctx: Context[AppRuntime],
) -> tuple[AppRuntime, Principal]:
    runtime = ctx.request_context.lifespan_context
    principal = await runtime.authenticator.principal(ctx.headers)
    return runtime, principal


async def _health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})
