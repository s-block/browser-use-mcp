from __future__ import annotations

import asyncio
import io
import json
import zipfile
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from steel.types.sessions.captcha_status_response import CaptchaStatusResponseItem

from browser_use_mcp.backend import (
    _EXTENSION_ID,
    _EXTENSION_PUBLIC_KEY,
    BrowserBackendError,
    StagehandSteelSession,
    SteelStagehandBackend,
    _extension_id_from_public_key,
    _session_create_params,
    _stagehand_extension_archive,
)
from tests.conftest import make_settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path
    from random import Random

    from stagehand import Stagehand, StagehandBrowser
    from steel import AsyncSteel
    from steel.types.profile_get_response import ProfileGetResponse

    from browser_use_mcp.browser_network import BrowserNetworkGuard
    from browser_use_mcp.config import SteelSessionPolicy


class _FakeLocator:
    def __init__(self, events: list[object]) -> None:
        self._events = events

    async def hover(self) -> None:
        self._events.append("hover")

    async def click(self) -> None:
        self._events.append("click")

    async def fill(self, value: str) -> None:
        self._events.append(("fill", value))

    async def type(self, value: str, *, delay: float | None = None) -> None:
        self._events.append(("type", value, delay))

    async def inner_text(self) -> str:
        self._events.append("inner_text")
        return "text"


class _FakeResponse:
    status = 200


class _FakePage:
    def __init__(self, events: list[object]) -> None:
        self._events = events
        self._locator = _FakeLocator(events)

    async def goto(self, url: str, **options: object) -> _FakeResponse:
        self._events.append(("goto", url, options))
        return _FakeResponse()

    async def url(self) -> str:
        return "https://example.com/account"

    async def title(self) -> str:
        return "Account"

    async def key_press(self, value: str, *, delay: float | None = None) -> None:
        self._events.append(("key_press", value, delay))

    def locator(self, selector: str) -> _FakeLocator:
        self._events.append(("locator", selector))
        return self._locator


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self._page = page

    async def active_page(self) -> _FakePage:
        return self._page


class _FakeBrowser:
    def __init__(self, page: _FakePage) -> None:
        self.context = _FakeContext(page)

    async def close(self) -> None:
        pass


class _FakeStagehand:
    async def close(self) -> None:
        pass


class _FakeNetworkGuard:
    def __init__(self) -> None:
        self.healthy = True

    def ensure_healthy(self) -> None:
        if not self.healthy:
            raise BrowserBackendError("network guard failed")

    async def close(self) -> None:
        pass


class _FakeExtensions:
    def __init__(self) -> None:
        self.uploads: list[object] = []

    async def upload(self, *, file: object) -> SimpleNamespace:
        self.uploads.append(file)
        return SimpleNamespace(id="owned-extension-1")


class _FakeExtensionSteel:
    def __init__(self) -> None:
        self.extensions = _FakeExtensions()


class _FakeCaptchas:
    def __init__(
        self, responses: list[list[CaptchaStatusResponseItem]] | None = None
    ) -> None:
        self._responses = responses or []
        self.calls = 0

    async def status(self, _session_id: str) -> list[CaptchaStatusResponseItem]:
        self.calls += 1
        if not self._responses:
            return []
        return self._responses.pop(0)


class _FakeSessions:
    def __init__(self, captchas: _FakeCaptchas) -> None:
        self.captchas = captchas

    async def release(self, _session_id: str) -> None:
        pass


class _FakeSteel:
    def __init__(self, captchas: _FakeCaptchas) -> None:
        self.sessions = _FakeSessions(captchas)


class _MidpointRandom:
    @staticmethod
    def uniform(minimum: float, maximum: float) -> float:
        return (minimum + maximum) / 2


def _captcha_state(*, active: bool, task_status: str) -> CaptchaStatusResponseItem:
    return CaptchaStatusResponseItem(
        isSolvingCaptcha=active,
        pageId="page-1",
        tasks=[{"status": task_status}],
        url="https://example.com/account",
    )


def _session(
    policy: SteelSessionPolicy,
    *,
    captcha_responses: list[list[CaptchaStatusResponseItem]] | None = None,
) -> tuple[StagehandSteelSession, list[object], list[float], _FakeCaptchas]:
    events: list[object] = []
    sleeps: list[float] = []
    page = _FakePage(events)
    captchas = _FakeCaptchas(captcha_responses)

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    session = StagehandSteelSession(
        steel=cast("AsyncSteel", _FakeSteel(captchas)),
        steel_session_id="session-1",
        provider_profile_id=None,
        browser=cast("StagehandBrowser", _FakeBrowser(page)),
        stagehand=cast("Stagehand", _FakeStagehand()),
        network_guard=cast("BrowserNetworkGuard", _FakeNetworkGuard()),
        session_policy=policy,
        profile_ready_timeout_seconds=1.0,
        rng=cast("Random", _MidpointRandom()),
        sleep=cast("Callable[[float], Awaitable[None]]", record_sleep),
    )
    return session, events, sleeps, captchas


def test_session_parameters_are_explicit_and_do_not_override_user_agent(
    tmp_path: Path,
) -> None:
    policy = replace(
        make_settings(tmp_path).steel_session,
        proxy_url="https://proxy.example:8443",
        network_identity="dedicated-proxy-1",
    )

    params = _session_create_params(
        policy,
        extension_id="extension-1",
        session_timeout_ms=300_000,
        persistent=True,
        profile_id=None,
        profile=None,
    )

    assert params["headless"] is False
    assert params["device_config"] == {"device": "desktop"}
    assert params["dimensions"] == {"width": 1_440, "height": 1_000}
    assert "region" not in params
    assert params["proxy_url"] == policy.proxy_url
    assert params["stealth_config"] == {
        "humanize_interactions": True,
        "skip_fingerprint_injection": False,
    }
    assert params["solve_captcha"] is False
    assert "user_agent" not in params


def test_stagehand_archive_has_a_pinned_chrome_extension_identity() -> None:
    archive = _stagehand_extension_archive()
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))

    assert manifest["key"] == _EXTENSION_PUBLIC_KEY
    assert _extension_id_from_public_key(manifest["key"]) == _EXTENSION_ID


@pytest.mark.asyncio
async def test_stagehand_extension_is_uploaded_without_name_only_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steel = _FakeExtensionSteel()
    backend = SteelStagehandBackend.__new__(SteelStagehandBackend)
    backend._steel = cast("AsyncSteel", steel)
    backend._extension_id = None
    backend._extension_lock = asyncio.Lock()
    monkeypatch.setattr("browser_use_mcp.backend.metadata.version", lambda _: "4.0.0")
    monkeypatch.setattr(
        "browser_use_mcp.backend._stagehand_extension_archive",
        lambda: b"trusted-extension-archive",
    )

    first = await backend._ensure_extension()
    second = await backend._ensure_extension()

    assert first == second == "owned-extension-1"
    assert len(steel.extensions.uploads) == 1


def test_existing_profile_keeps_its_saved_dimensions(tmp_path: Path) -> None:
    profile = cast(
        "ProfileGetResponse",
        SimpleNamespace(dimensions=SimpleNamespace(width=1_366, height=768)),
    )

    params = _session_create_params(
        make_settings(tmp_path).steel_session,
        extension_id="extension-1",
        session_timeout_ms=300_000,
        persistent=True,
        profile_id="profile-1",
        profile=profile,
    )

    assert params["profile_id"] == "profile-1"
    assert params["dimensions"] == {"width": 1_366, "height": 768}


def test_configured_cloud_region_is_forwarded(tmp_path: Path) -> None:
    policy = replace(
        make_settings(tmp_path).steel_session,
        region="configured-region",
    )

    params = _session_create_params(
        policy,
        extension_id="extension-1",
        session_timeout_ms=300_000,
        persistent=False,
        profile_id=None,
        profile=None,
    )

    assert params["region"] == "configured-region"


def test_self_hosted_session_omits_cloud_only_parameters(tmp_path: Path) -> None:
    policy = make_settings(
        tmp_path,
        env_overrides={
            "STEEL_BASE_URL": "http://steel.internal:3000",
            "BROWSER_USE_MCP_ALLOW_INSECURE_PROVIDER_HTTP": "true",
        },
    ).steel_session

    params = _session_create_params(
        policy,
        extension_id="extension-1",
        session_timeout_ms=300_000,
        persistent=False,
        profile_id=None,
        profile=None,
    )

    assert "region" not in params
    assert "solve_captcha" not in params
    assert "stealth_config" not in params


@pytest.mark.asyncio
async def test_direct_click_hovers_and_pauses_before_click(tmp_path: Path) -> None:
    policy = replace(
        make_settings(tmp_path).steel_session,
        click_delay_min_ms=100.0,
        click_delay_max_ms=100.0,
    )
    session, events, sleeps, _captchas = _session(policy)

    await session.control("click", "#submit", None)

    assert events == [("locator", "#submit"), "hover", "click"]
    assert sleeps == [0.1]


@pytest.mark.asyncio
async def test_direct_fill_clears_then_types_with_delay(tmp_path: Path) -> None:
    policy = replace(
        make_settings(tmp_path).steel_session,
        typing_delay_min_ms=75.0,
        typing_delay_max_ms=75.0,
        click_delay_min_ms=100.0,
        click_delay_max_ms=100.0,
    )
    session, events, sleeps, _captchas = _session(policy)

    await session.control("fill", "#account", "12345678")

    assert events == [
        ("locator", "#account"),
        "hover",
        "click",
        ("fill", ""),
        ("type", "12345678", 75.0),
    ]
    assert sleeps == [0.1]


@pytest.mark.asyncio
async def test_direct_type_focuses_with_pointer_and_types_with_delay(
    tmp_path: Path,
) -> None:
    policy = replace(
        make_settings(tmp_path).steel_session,
        typing_delay_min_ms=60.0,
        typing_delay_max_ms=60.0,
        click_delay_min_ms=90.0,
        click_delay_max_ms=90.0,
    )
    session, events, sleeps, _captchas = _session(policy)

    await session.control("type", "#search", "query")

    assert events == [
        ("locator", "#search"),
        "hover",
        "click",
        ("type", "query", 60.0),
    ]
    assert sleeps == [0.09]


@pytest.mark.asyncio
async def test_navigation_waits_for_automatic_captcha_solver(tmp_path: Path) -> None:
    policy = replace(
        make_settings(tmp_path).steel_session,
        solve_captcha=True,
        captcha_poll_interval_seconds=0.25,
    )
    session, _events, sleeps, captchas = _session(
        policy,
        captcha_responses=[
            [_captcha_state(active=True, task_status="solving")],
            [_captcha_state(active=False, task_status="solved")],
        ],
    )

    result = await session.navigate("https://example.com/account", 1_000)

    assert result.status == 200
    assert captchas.calls == 2
    assert sleeps == [0.25, 0.25]


@pytest.mark.asyncio
async def test_captcha_failure_stops_browser_operation(tmp_path: Path) -> None:
    policy = replace(make_settings(tmp_path).steel_session, solve_captcha=True)
    session, _events, _sleeps, _captchas = _session(
        policy,
        captcha_responses=[
            [_captcha_state(active=False, task_status="failed_to_solve")]
        ],
    )

    with pytest.raises(BrowserBackendError, match="CAPTCHA solving failed"):
        await session.control("title", None, None)
