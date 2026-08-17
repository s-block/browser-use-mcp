from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar
from urllib.parse import urlsplit, urlunsplit

from browser_use_mcp.audit import audit_event
from browser_use_mcp.backend import (
    BrowserBackend,
    BrowserBackendError,
    BrowserBackendSession,
)
from browser_use_mcp.llm import LLMConfig, llm_request_scope
from browser_use_mcp.models import (
    ActResult,
    BrowserControlCommand,
    ControlResult,
    ExtractResult,
    NavigationResult,
    ObservedAction,
    ObserveResult,
    ProfileInfo,
    ProfileListResult,
    SessionClosed,
    SessionStarted,
)
from browser_use_mcp.network import UnsafeURLError, validate_outbound_url

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from browser_use_mcp.config import Settings
    from browser_use_mcp.profiles import (
        ProfileLease,
        ProfileLeaseManager,
        ProfileStore,
    )
    from browser_use_mcp.security import Principal

T = TypeVar("T")


class BrowserServiceError(RuntimeError):
    pass


class SessionNotFoundError(BrowserServiceError):
    pass


class SessionCapacityError(BrowserServiceError):
    pass


class BrowserOperationError(BrowserServiceError):
    pass


class BrowserOperationTimedOutError(BrowserOperationError):
    pass


@dataclass(slots=True)
class _SessionEntry:
    namespace_id: str
    backend: BrowserBackendSession
    lease: ProfileLease | None
    operation_lock: asyncio.Lock
    closing: bool = False
    closed: bool = False


@dataclass(slots=True)
class _OrphanedSession:
    backend: BrowserBackendSession
    lease: ProfileLease | None


class BrowserSessionService:
    """Owns bounded browser sessions and enforces client/profile isolation."""

    def __init__(
        self,
        settings: Settings,
        backend: BrowserBackend,
        profiles: ProfileStore,
        leases: ProfileLeaseManager,
    ) -> None:
        self._settings = settings
        self._backend = backend
        self._profiles = profiles
        self._leases = leases
        self._capacity = asyncio.Semaphore(settings.max_sessions)
        self._sessions: dict[str, _SessionEntry] = {}
        self._orphaned: list[_OrphanedSession] = []
        self._sessions_lock = asyncio.Lock()

    async def start(
        self, principal: Principal, profile_name: str | None
    ) -> SessionStarted:
        try:
            await asyncio.wait_for(
                self._capacity.acquire(),
                timeout=self._settings.operation_timeout_seconds,
            )
        except TimeoutError as exc:
            audit_event(
                "session_start",
                outcome="denied",
                tenant_namespace=principal.namespace_id,
                reason="capacity",
                persistent=profile_name is not None,
            )
            raise SessionCapacityError("browser session capacity is exhausted") from exc

        lease: ProfileLease | None = None
        backend_session: BrowserBackendSession | None = None
        try:
            provider_profile_id = None
            if profile_name is not None:
                locator = self._profiles.locator(profile_name)
                lease = await self._leases.acquire(principal, locator)
                record = await self._profiles.get(principal, profile_name)
                if record is not None:
                    if (
                        record.network_identity is not None
                        and record.network_identity
                        != self._settings.steel_session.network_identity
                    ):
                        raise BrowserOperationError(
                            "persistent browser profile is bound to a different "
                            "network identity"
                        )
                    provider_profile_id = record.provider_profile_id

            backend_session = await self._run(
                lambda: self._backend.start(
                    profile_id=provider_profile_id,
                    persistent=profile_name is not None,
                )
            )
            if profile_name is not None:
                if backend_session.profile_id is None:
                    raise BrowserOperationError(
                        "browser provider did not create a persistent profile"
                    )
                await self._profiles.put(
                    principal,
                    profile_name,
                    backend_session.profile_id,
                    network_identity=self._settings.steel_session.network_identity,
                )

            session_id = secrets.token_urlsafe(24)
            entry = _SessionEntry(
                namespace_id=principal.namespace_id,
                backend=backend_session,
                lease=lease,
                operation_lock=asyncio.Lock(),
            )
            async with self._sessions_lock:
                self._sessions[session_id] = entry
            audit_event(
                "session_start",
                outcome="succeeded",
                tenant_namespace=principal.namespace_id,
                persistent=profile_name is not None,
            )
            return SessionStarted(
                session_id=session_id,
                persistent=profile_name is not None,
                profile_name=profile_name,
            )
        except BaseException:
            audit_event(
                "session_start",
                outcome="failed",
                tenant_namespace=principal.namespace_id,
                reason="operation_error",
                persistent=profile_name is not None,
            )
            cleanup_succeeded = backend_session is None
            try:
                if backend_session is not None:
                    cleanup_succeeded = await self._best_effort_backend_close(
                        backend_session
                    )
            finally:
                if not cleanup_succeeded and backend_session is not None:
                    self._orphaned.append(_OrphanedSession(backend_session, lease))
                else:
                    try:
                        if lease is not None:
                            await lease.release()
                    finally:
                        self._capacity.release()
            raise

    async def close(self, principal: Principal, session_id: str) -> SessionClosed:
        entry = await self._entry(principal, session_id)
        await self._close_entry(session_id, entry)
        audit_event(
            "session_close",
            outcome="succeeded",
            tenant_namespace=principal.namespace_id,
        )
        return SessionClosed()

    async def navigate(
        self, principal: Principal, session_id: str, url: str
    ) -> NavigationResult:
        try:
            await validate_outbound_url(
                url, allow_private_network=self._settings.allow_private_network
            )
        except UnsafeURLError:
            audit_event(
                "navigation",
                outcome="blocked",
                tenant_namespace=principal.namespace_id,
                reason="network_policy",
            )
            raise
        entry = await self._entry(principal, session_id)
        async with entry.operation_lock:
            self._ensure_open(entry)
            result = await self._run(
                lambda: entry.backend.navigate(
                    url, int(self._settings.operation_timeout_seconds * 1000)
                )
            )
        return NavigationResult(
            url=self._truncate(_sanitize_url(result.url))[0],
            title=self._truncate(result.title)[0],
            status=result.status,
        )

    async def act(
        self,
        principal: Principal,
        session_id: str,
        instruction: str,
        llm_config: LLMConfig,
    ) -> ActResult:
        entry = await self._entry(principal, session_id)
        async with entry.operation_lock:
            self._ensure_open(entry)
            with llm_request_scope(llm_config):
                result = await self._run(
                    lambda: entry.backend.act(
                        instruction, self._settings.operation_timeout_seconds
                    )
                )
        return ActResult(
            success=result.success,
            message=(
                "Stagehand completed the requested action"
                if result.success
                else "Stagehand could not complete the requested action"
            ),
        )

    async def observe(
        self,
        principal: Principal,
        session_id: str,
        instruction: str | None,
        llm_config: LLMConfig,
    ) -> ObserveResult:
        entry = await self._entry(principal, session_id)
        async with entry.operation_lock:
            self._ensure_open(entry)
            with llm_request_scope(llm_config):
                result = await self._run(
                    lambda: entry.backend.observe(
                        instruction, self._settings.operation_timeout_seconds
                    )
                )
        truncated = len(result) > 100
        remaining = self._settings.max_response_characters
        actions: list[ObservedAction] = []
        for item in result[:100]:
            selector, selector_truncated = _take(item.selector, remaining)
            remaining -= len(selector)
            description, description_truncated = _take(item.description, remaining)
            remaining -= len(description)
            method, method_truncated = _take(item.method or "", remaining)
            remaining -= len(method)
            actions.append(
                ObservedAction(
                    selector=selector,
                    description=description,
                    method=method or None,
                )
            )
            truncated = (
                truncated
                or selector_truncated
                or description_truncated
                or method_truncated
            )
            if remaining == 0:
                truncated = truncated or len(actions) < len(result)
                break
        return ObserveResult(actions=actions, truncated=truncated)

    async def extract(
        self,
        principal: Principal,
        session_id: str,
        instruction: str,
        llm_config: LLMConfig,
    ) -> ExtractResult:
        entry = await self._entry(principal, session_id)
        async with entry.operation_lock:
            self._ensure_open(entry)
            with llm_request_scope(llm_config):
                result = await self._run(
                    lambda: entry.backend.extract(
                        instruction, self._settings.operation_timeout_seconds
                    )
                )
        extraction, truncated = self._truncate(result)
        return ExtractResult(extraction=extraction, truncated=truncated)

    async def control(
        self,
        principal: Principal,
        session_id: str,
        command: BrowserControlCommand,
        selector: str | None,
        value: str | None,
    ) -> ControlResult:
        _validate_control(command, selector, value)
        entry = await self._entry(principal, session_id)
        async with entry.operation_lock:
            self._ensure_open(entry)
            result = await self._run(
                lambda: entry.backend.control(command.value, selector, value)
            )
        if result is None:
            return ControlResult()
        if command is BrowserControlCommand.URL:
            result = _sanitize_url(result)
        output, truncated = self._truncate(result)
        return ControlResult(value=output, truncated=truncated)

    async def list_profiles(self, principal: Principal) -> ProfileListResult:
        profiles = await self._profiles.list(principal)
        return ProfileListResult(
            profiles=[
                ProfileInfo(
                    name=profile.name,
                    created_at=profile.created_at.isoformat(),
                    updated_at=profile.updated_at.isoformat(),
                )
                for profile in profiles
            ]
        )

    async def shutdown(self) -> None:
        async with self._sessions_lock:
            entries = list(self._sessions.items())
        for session_id, entry in entries:
            await self._close_entry(session_id, entry, best_effort=True)
        orphaned, self._orphaned = self._orphaned, []
        for orphan in orphaned:
            try:
                await self._best_effort_backend_close(orphan.backend)
            finally:
                try:
                    if orphan.lease is not None:
                        await orphan.lease.release()
                finally:
                    self._capacity.release()
        try:
            await self._backend.close()
        finally:
            await self._profiles.aclose()

    async def _entry(self, principal: Principal, session_id: str) -> _SessionEntry:
        async with self._sessions_lock:
            entry = self._sessions.get(session_id)
        if entry is None or not secrets.compare_digest(
            entry.namespace_id, principal.namespace_id
        ):
            audit_event(
                "session_access",
                outcome="denied",
                tenant_namespace=principal.namespace_id,
                reason="not_found_or_wrong_tenant",
            )
            raise SessionNotFoundError("browser session does not exist")
        return entry

    async def _close_entry(
        self,
        session_id: str,
        entry: _SessionEntry,
        *,
        best_effort: bool = False,
    ) -> None:
        close_error: BaseException | None = None
        async with entry.operation_lock:
            if entry.closed:
                return
            entry.closing = True
            try:
                await self._run(entry.backend.close)
            except BaseException as exc:
                close_error = exc
                if not best_effort:
                    raise
            entry.closed = True
            async with self._sessions_lock:
                self._sessions.pop(session_id, None)
            try:
                if entry.lease is not None:
                    await entry.lease.release()
            finally:
                self._capacity.release()
        if isinstance(close_error, asyncio.CancelledError):
            raise close_error

    async def _run(self, operation: Callable[[], Awaitable[T]]) -> T:
        try:
            async with asyncio.timeout(self._settings.operation_timeout_seconds):
                return await operation()
        except TimeoutError as exc:
            raise BrowserOperationTimedOutError("browser operation timed out") from exc
        except BrowserBackendError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise BrowserOperationError("browser operation failed") from None

    async def _best_effort_backend_close(
        self, backend_session: BrowserBackendSession
    ) -> bool:
        try:
            await self._run(backend_session.close)
        except (BrowserServiceError, BrowserBackendError):
            return False
        return True

    def _truncate(self, value: str) -> tuple[str, bool]:
        return _take(value, self._settings.max_response_characters)

    @staticmethod
    def _ensure_open(entry: _SessionEntry) -> None:
        if entry.closed or entry.closing:
            raise SessionNotFoundError("browser session does not exist")


def _validate_control(
    command: BrowserControlCommand, selector: str | None, value: str | None
) -> None:
    if (
        command
        in {
            BrowserControlCommand.CLICK,
            BrowserControlCommand.FILL,
            BrowserControlCommand.TYPE,
            BrowserControlCommand.TEXT,
        }
        and selector is None
    ):
        raise BrowserOperationError(f"{command.value} requires selector")
    if (
        command
        in {
            BrowserControlCommand.FILL,
            BrowserControlCommand.TYPE,
            BrowserControlCommand.KEY_PRESS,
        }
        and value is None
    ):
        raise BrowserOperationError(f"{command.value} requires value")


def _sanitize_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        if hostname is None:
            return "redacted"
        rendered_host = f"[{hostname}]" if ":" in hostname else hostname
        netloc = rendered_host
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except ValueError:
        return "redacted"


def _take(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[:limit], True
