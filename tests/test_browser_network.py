from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, cast

import pytest

from browser_use_mcp.browser_network import (
    BrowserNetworkGuard,
    _continue_or_block_request,
)

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection


class _FakeCDPWebSocket:
    def __init__(self) -> None:
        self.messages: asyncio.Queue[str] = asyncio.Queue()
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self.target = {
            "targetId": "page-1",
            "type": "page",
            "url": "about:blank",
        }

    async def send(self, raw: str) -> None:
        request = json.loads(raw)
        self.calls.append(request)
        method = request["method"]
        if method == "Target.setDiscoverTargets":
            await self.emit(
                {
                    "method": "Target.targetCreated",
                    "params": {"targetInfo": self.target},
                }
            )
        result: dict[str, Any] = {}
        if method == "Target.attachToTarget":
            result = {"sessionId": "guard-session-1"}
            await self.emit(
                {
                    "method": "Target.attachedToTarget",
                    "params": {
                        "sessionId": "guard-session-1",
                        "targetInfo": self.target,
                        "waitingForDebugger": False,
                    },
                }
            )
        elif method == "Target.getTargets":
            result = {"targetInfos": [self.target]}
        await self.emit({"id": request["id"], "result": result})

    async def recv(self) -> str:
        return await self.messages.get()

    async def close(self) -> None:
        self.closed = True

    async def emit(self, message: dict[str, Any]) -> None:
        await self.messages.put(json.dumps(message))


@pytest.mark.asyncio
async def test_guard_lifecycle_intercepts_existing_page_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _FakeCDPWebSocket()

    async def fake_connect(_websocket_url: str, **_options: object) -> ClientConnection:
        return cast("ClientConnection", websocket)

    monkeypatch.setattr("browser_use_mcp.browser_network.connect", fake_connect)
    guard = await BrowserNetworkGuard.create(
        "wss://browser.example/devtools",
        allow_private_network=False,
    )
    try:
        await websocket.emit(
            {
                "method": "Fetch.requestPaused",
                "sessionId": "guard-session-1",
                "params": {
                    "requestId": "private-request-1",
                    "request": {"url": "http://127.0.0.1/private"},
                },
            }
        )
        for _ in range(100):
            if any(call["method"] == "Fetch.failRequest" for call in websocket.calls):
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("paused request was not handled")

        methods = [call["method"] for call in websocket.calls]
        assert methods[:2] == ["Target.setDiscoverTargets", "Target.attachToTarget"]
        assert "Fetch.enable" in methods
        assert "Target.setAutoAttach" in methods
        assert methods[-1] == "Fetch.failRequest"
        guard.ensure_healthy()
    finally:
        await guard.close()

    assert websocket.closed is True


class _RecordingGuard(BrowserNetworkGuard):
    def __init__(self) -> None:
        self._target_sessions = {}
        self._attaching_targets = set()
        self._configured_sessions = set()
        self._target_lock = asyncio.Lock()
        self.calls: list[tuple[str, dict[str, object] | None, str | None]] = []

    async def _call(
        self,
        method: str,
        params: dict[str, object] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((method, params, session_id))
        if method == "Target.attachToTarget":
            return {"result": {"sessionId": "guard-session-1"}}
        return {}


@pytest.mark.asyncio
async def test_existing_page_is_explicitly_attached_and_intercepted() -> None:
    guard = _RecordingGuard()

    await guard._attach_target(
        {"targetId": "page-1", "type": "page", "url": "about:blank"}
    )

    assert [call[0] for call in guard.calls] == [
        "Target.attachToTarget",
        "Fetch.enable",
        "Target.setAutoAttach",
        "Runtime.runIfWaitingForDebugger",
    ]
    assert guard._target_sessions == {"page-1": "guard-session-1"}


@pytest.mark.asyncio
async def test_private_redirect_request_is_blocked_before_send(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[tuple[str, dict[str, object] | None, str | None]] = []

    async def call(
        method: str,
        params: dict[str, object] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        calls.append((method, params, session_id))
        return {}

    url = "http://127.0.0.1/admin"
    with caplog.at_level(logging.INFO, logger="browser_use_mcp.audit"):
        await _continue_or_block_request(
            call,
            {
                "requestId": "request-2",
                "redirectedRequestId": "request-1",
                "request": {"url": url},
            },
            session_id="target-1",
            allow_private_network=False,
        )

    assert calls == [
        (
            "Fetch.failRequest",
            {"requestId": "request-2", "errorReason": "BlockedByClient"},
            "target-1",
        )
    ]
    assert "browser_request" in caplog.text
    assert url not in caplog.text


@pytest.mark.asyncio
async def test_public_subresource_request_is_continued() -> None:
    calls: list[tuple[str, dict[str, object] | None, str | None]] = []

    async def call(
        method: str,
        params: dict[str, object] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        calls.append((method, params, session_id))
        return {}

    await _continue_or_block_request(
        call,
        {
            "requestId": "request-3",
            "request": {"url": "https://8.8.8.8/resource.js"},
        },
        session_id="target-2",
        allow_private_network=False,
    )

    assert calls == [
        (
            "Fetch.continueRequest",
            {"requestId": "request-3"},
            "target-2",
        )
    ]
