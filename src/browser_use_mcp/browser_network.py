from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, Protocol, cast

from websockets.asyncio.client import ClientConnection, connect

from browser_use_mcp.audit import audit_event
from browser_use_mcp.network import UnsafeURLError, validate_outbound_url

_INTERCEPTED_TARGET_TYPES = {"iframe", "page", "service_worker", "shared_worker"}
_TARGET_FILTER = [
    {"type": target_type, "exclude": False}
    for target_type in sorted(_INTERCEPTED_TARGET_TYPES)
] + [{"exclude": True}]
_REQUEST_PATTERNS = [
    {"urlPattern": "http://*/*", "requestStage": "Request"},
    {"urlPattern": "https://*/*", "requestStage": "Request"},
]


class BrowserNetworkGuardError(RuntimeError):
    """Raised when the browser request boundary cannot be enforced."""


class _CDPCall(Protocol):
    async def __call__(
        self,
        method: str,
        params: dict[str, object] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]: ...


class BrowserNetworkGuard:
    """Intercepts every HTTP(S) request before remote Chromium sends it."""

    def __init__(
        self,
        websocket: ClientConnection,
        *,
        allow_private_network: bool,
        max_pending_requests: int = 32,
    ) -> None:
        self._websocket = websocket
        self._allow_private_network = allow_private_network
        self._request_capacity = asyncio.Semaphore(max_pending_requests)
        self._events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=max_pending_requests * 4
        )
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._request_tasks: set[asyncio.Task[None]] = set()
        self._target_sessions: dict[str, str] = {}
        self._attaching_targets: set[str] = set()
        self._configured_sessions: set[str] = set()
        self._target_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._next_request_id = 0
        self._failure: BaseException | None = None
        self._failure_close_task: asyncio.Task[None] | None = None
        self._closed = False
        self._reader_task = asyncio.create_task(
            self._read_messages(), name="browser-network-cdp-reader"
        )
        self._event_task = asyncio.create_task(
            self._process_events(), name="browser-network-cdp-events"
        )

    @classmethod
    async def create(
        cls, websocket_url: str, *, allow_private_network: bool
    ) -> BrowserNetworkGuard:
        try:
            websocket = await connect(
                websocket_url,
                open_timeout=10,
                close_timeout=5,
                max_size=1_048_576,
            )
        except (OSError, TimeoutError, ValueError, TypeError) as exc:
            raise BrowserNetworkGuardError(
                "browser network guard could not connect"
            ) from exc
        guard = cls(
            websocket,
            allow_private_network=allow_private_network,
        )
        try:
            await guard._call(
                "Target.setDiscoverTargets",
                {"discover": True, "filter": _TARGET_FILTER},
            )
            async with asyncio.timeout(10):
                await guard._events.join()
            await guard._call(
                "Target.setAutoAttach",
                {
                    "autoAttach": True,
                    "waitForDebuggerOnStart": True,
                    "flatten": True,
                    "filter": _TARGET_FILTER,
                },
            )
            async with asyncio.timeout(10):
                await guard._events.join()
            targets_response = await guard._call(
                "Target.getTargets", {"filter": _TARGET_FILTER}
            )
            targets = targets_response.get("result", {}).get("targetInfos", [])
            if not isinstance(targets, list):
                raise BrowserNetworkGuardError("browser target list is invalid")
            for target in targets:
                if isinstance(target, dict):
                    await guard._attach_target(cast("dict[str, Any]", target))
            async with asyncio.timeout(10):
                await guard._events.join()
            guard.ensure_healthy()
        except asyncio.CancelledError:
            await guard.close()
            raise
        except Exception:
            await guard.close()
            raise BrowserNetworkGuardError(
                "browser network guard could not be enabled"
            ) from None
        return guard

    def ensure_healthy(self) -> None:
        if self._closed or self._failure is not None:
            raise BrowserNetworkGuardError("browser network guard is unavailable")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = [self._event_task, self._reader_task, *self._request_tasks]
        if self._failure_close_task is not None:
            tasks.append(self._failure_close_task)
        for task in tasks:
            task.cancel()
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
        cancellation: asyncio.CancelledError | None = None
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError as exc:
            cancellation = exc
        try:
            with contextlib.suppress(Exception):
                await self._websocket.close()
        except asyncio.CancelledError as exc:
            cancellation = exc
        if cancellation is not None:
            raise cancellation

    async def _read_messages(self) -> None:
        failure: BaseException | None = None
        try:
            while True:
                raw = await self._websocket.recv()
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise TypeError("CDP message must be an object")
                request_id = message.get("id")
                if isinstance(request_id, int):
                    future = self._pending.get(request_id)
                    if future is not None and not future.done():
                        future.set_result(cast("dict[str, Any]", message))
                    continue
                if isinstance(message.get("method"), str):
                    await self._events.put(cast("dict[str, Any]", message))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            failure = exc
        finally:
            if failure is not None and self._failure is None and not self._closed:
                self._failure = failure
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(
                        BrowserNetworkGuardError(
                            "browser network guard connection failed"
                        )
                    )
            with contextlib.suppress(asyncio.QueueFull):
                self._events.put_nowait(None)

    async def _process_events(self) -> None:
        try:
            while True:
                event = await self._events.get()
                try:
                    if event is None:
                        return
                    await self._process_event(event)
                finally:
                    self._events.task_done()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if self._failure is None and not self._closed:
                self._failure = exc
            with contextlib.suppress(Exception):
                await self._websocket.close()

    async def _process_event(self, event: dict[str, Any]) -> None:
        method = event.get("method")
        params = event.get("params")
        if not isinstance(params, dict):
            return
        if method == "Target.attachedToTarget":
            await self._configure_target(cast("dict[str, Any]", params))
            return
        if method == "Target.targetCreated":
            target = params.get("targetInfo")
            if isinstance(target, dict):
                await self._attach_target(cast("dict[str, Any]", target))
            return
        if method in {"Target.detachedFromTarget", "Target.targetDestroyed"}:
            await self._forget_target(cast("dict[str, Any]", params))
            return
        if method != "Fetch.requestPaused":
            return
        await self._request_capacity.acquire()
        task = asyncio.create_task(
            _continue_or_block_request(
                self._call,
                cast("dict[str, Any]", params),
                session_id=_optional_string(event.get("sessionId")),
                allow_private_network=self._allow_private_network,
            ),
            name="browser-network-request-check",
        )
        self._request_tasks.add(task)
        task.add_done_callback(self._request_finished)

    async def _configure_target(self, params: dict[str, Any]) -> None:
        session_id = _optional_string(params.get("sessionId"))
        target = params.get("targetInfo")
        if session_id is None or not isinstance(target, dict):
            raise BrowserNetworkGuardError("browser target metadata is invalid")
        target_id = _optional_string(target.get("targetId"))
        if target_id is None:
            raise BrowserNetworkGuardError("browser target identifier is invalid")
        async with self._target_lock:
            self._target_sessions[target_id] = session_id
            if session_id in self._configured_sessions:
                return
            target_type = target.get("type")
            target_url = str(target.get("url", ""))
            should_intercept = target_type in _INTERCEPTED_TARGET_TYPES and (
                target_type in {"iframe", "page"}
                or target_url.startswith(("http://", "https://"))
            )
            if should_intercept:
                await self._call(
                    "Fetch.enable",
                    {"patterns": _REQUEST_PATTERNS, "handleAuthRequests": False},
                    session_id=session_id,
                )
                await self._call(
                    "Target.setAutoAttach",
                    {
                        "autoAttach": True,
                        "waitForDebuggerOnStart": True,
                        "flatten": True,
                        "filter": _TARGET_FILTER,
                    },
                    session_id=session_id,
                )
            await self._call(
                "Runtime.runIfWaitingForDebugger",
                session_id=session_id,
            )
            self._configured_sessions.add(session_id)

    async def _attach_target(self, target: dict[str, Any]) -> None:
        target_id = _optional_string(target.get("targetId"))
        if target_id is None or target.get("type") not in _INTERCEPTED_TARGET_TYPES:
            return
        async with self._target_lock:
            if (
                target_id in self._target_sessions
                or target_id in self._attaching_targets
            ):
                return
            self._attaching_targets.add(target_id)
        try:
            attached = await self._call(
                "Target.attachToTarget",
                {"targetId": target_id, "flatten": True},
            )
            session_id = _optional_string(attached.get("result", {}).get("sessionId"))
            if session_id is None:
                raise BrowserNetworkGuardError("browser target could not be attached")
            await self._configure_target(
                {"sessionId": session_id, "targetInfo": target}
            )
        finally:
            async with self._target_lock:
                self._attaching_targets.discard(target_id)

    async def _forget_target(self, params: dict[str, Any]) -> None:
        target_id = _optional_string(params.get("targetId"))
        session_id = _optional_string(params.get("sessionId"))
        async with self._target_lock:
            if target_id is not None:
                removed_session = self._target_sessions.pop(target_id, None)
                if removed_session is not None:
                    self._configured_sessions.discard(removed_session)
            if session_id is not None:
                self._configured_sessions.discard(session_id)
                stale_targets = [
                    item_target_id
                    for item_target_id, item_session_id in self._target_sessions.items()
                    if item_session_id == session_id
                ]
                for stale_target in stale_targets:
                    self._target_sessions.pop(stale_target, None)

    def _request_finished(self, task: asyncio.Task[None]) -> None:
        self._request_tasks.discard(task)
        self._request_capacity.release()
        if task.cancelled():
            return
        failure = task.exception()
        if failure is not None and self._failure is None and not self._closed:
            self._failure = failure
            self._failure_close_task = asyncio.create_task(
                self._websocket.close(), name="browser-network-failure-close"
            )

    async def _call(
        self,
        method: str,
        params: dict[str, object] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_healthy()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        async with self._send_lock:
            self._next_request_id += 1
            request_id = self._next_request_id
            self._pending[request_id] = future
            request: dict[str, object] = {"id": request_id, "method": method}
            if params is not None:
                request["params"] = params
            if session_id is not None:
                request["sessionId"] = session_id
            try:
                await self._websocket.send(json.dumps(request))
            except BaseException:
                self._pending.pop(request_id, None)
                raise
        try:
            response = await future
        finally:
            self._pending.pop(request_id, None)
        if "error" in response:
            raise BrowserNetworkGuardError("browser rejected a network guard command")
        return response


async def _continue_or_block_request(
    call: _CDPCall,
    params: dict[str, Any],
    *,
    session_id: str | None,
    allow_private_network: bool,
) -> None:
    request_id = _optional_string(params.get("requestId"))
    request = params.get("request")
    if request_id is None or not isinstance(request, dict):
        raise BrowserNetworkGuardError("browser request metadata is invalid")
    url = request.get("url")
    if not isinstance(url, str):
        raise BrowserNetworkGuardError("browser request URL is invalid")
    try:
        await validate_outbound_url(
            url,
            allow_private_network=allow_private_network,
        )
    except UnsafeURLError:
        audit_event(
            "browser_request",
            outcome="blocked",
            reason="network_policy",
        )
        await call(
            "Fetch.failRequest",
            {"requestId": request_id, "errorReason": "BlockedByClient"},
            session_id=session_id,
        )
        return
    await call(
        "Fetch.continueRequest",
        {"requestId": request_id},
        session_id=session_id,
    )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None
