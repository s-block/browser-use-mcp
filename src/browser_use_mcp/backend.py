from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import json
import random
import re
import secrets
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata, resources
from typing import TYPE_CHECKING, Any, Protocol, cast

from stagehand import DefaultExtract, Page, Stagehand, StagehandBrowser, local_browser
from steel import AsyncSteel
from websockets.asyncio.client import connect

from browser_use_mcp.audit import audit_event
from browser_use_mcp.browser_network import BrowserNetworkGuard

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from importlib.resources.abc import Traversable

    from stagehand import Locator
    from steel.types.profile_get_response import ProfileGetResponse
    from steel.types.session_create_params import SessionCreateParams, StealthConfig
    from steel.types.sessions.captcha_status_response import CaptchaStatusResponseItem
    from websockets.asyncio.client import ClientConnection

    from browser_use_mcp.config import SteelSessionPolicy
    from browser_use_mcp.llm import OpenAICompatibleStagehandModel

_EXTENSION_URL = re.compile(r"^chrome-extension://([a-p]{32})/service-worker\.js$")
_EXTENSION_PUBLIC_KEY = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA+NRJ"  # pragma: allowlist secret
    "X/zvjhLuQF5+OwYNyB6X+p0tJ7AhsfdmvgRp/2eOoVJEOqWP"  # pragma: allowlist secret
    "D15YSRTDyuDXb5ZupPofSQKl7xLs7IZlklevYbLa1KaE7LR3"  # pragma: allowlist secret
    "z74I51JaCkLJtFX1vKtuqbvZO4lrGYz1hDqOFRnVbOdNYF4/"  # pragma: allowlist secret
    "VPOsTllJnR35f4e/JDQFw2YNOXdPFwxhvaX2Q4tRZRwcQqyS"  # pragma: allowlist secret
    "M7mNSz3IHnOX0lMztPj3qmN4yLb4Gtoml8MEWJxTDa7/hpay"  # pragma: allowlist secret
    "QIqfXOLt3H+ZL9F3wCLU60+khr8U5+SPmMsGWDvjvXDhCqH1"  # pragma: allowlist secret
    "8qChZQJ0dwSkRw0565ZEEgIjJDoBmrjSIIsyppFwV9qWT0y5"  # pragma: allowlist secret
    "LwIDAQAB"  # pragma: allowlist secret
)
_EXTENSION_ID = "blhehghljpeabggdmcjdmgggeojakila"


class BrowserBackendError(RuntimeError):
    """A secret-safe browser-provider failure."""


@dataclass(frozen=True, slots=True)
class BackendNavigation:
    url: str
    title: str
    status: int | None


@dataclass(frozen=True, slots=True)
class BackendAct:
    success: bool


@dataclass(frozen=True, slots=True)
class BackendObservation:
    selector: str
    description: str
    method: str | None


class BrowserBackendSession(Protocol):
    @property
    def profile_id(self) -> str | None: ...

    async def navigate(self, url: str, timeout_ms: int) -> BackendNavigation: ...

    async def act(self, instruction: str, timeout_seconds: float) -> BackendAct: ...

    async def observe(
        self, instruction: str | None, timeout_seconds: float
    ) -> list[BackendObservation]: ...

    async def extract(self, instruction: str, timeout_seconds: float) -> str: ...

    async def control(
        self, command: str, selector: str | None, value: str | None
    ) -> str | None: ...

    async def close(self) -> None: ...


class BrowserBackend(Protocol):
    async def start(
        self, *, profile_id: str | None, persistent: bool
    ) -> BrowserBackendSession: ...

    async def close(self) -> None: ...


class SteelStagehandBackend:
    """Creates Steel Chromium sessions and attaches the Stagehand async SDK."""

    def __init__(
        self,
        *,
        steel_api_key: str | None,
        steel_base_url: str,
        session_timeout_ms: int,
        session_policy: SteelSessionPolicy,
        model: OpenAICompatibleStagehandModel,
        allow_private_network: bool,
    ) -> None:
        self._steel = AsyncSteel(
            steel_api_key=steel_api_key,
            base_url=steel_base_url,
            timeout=session_timeout_ms / 1000,
            max_retries=0,
        )
        self._session_timeout_ms = session_timeout_ms
        self._session_policy = session_policy
        self._model = model
        self._allow_private_network = allow_private_network
        self._extension_id: str | None = None
        self._extension_lock = asyncio.Lock()

    async def start(
        self, *, profile_id: str | None, persistent: bool
    ) -> BrowserBackendSession:
        steel_extension_id = await self._ensure_extension()
        session = None
        browser = None
        stagehand = None
        network_guard = None
        try:
            profile = None
            if profile_id is not None:
                profile = await _wait_for_profile_ready(
                    self._steel,
                    profile_id,
                    timeout_seconds=min(60.0, self._session_timeout_ms / 1000),
                )
            session = await self._steel.sessions.create(
                **_session_create_params(
                    self._session_policy,
                    extension_id=steel_extension_id,
                    session_timeout_ms=self._session_timeout_ms,
                    persistent=persistent,
                    profile_id=profile_id,
                    profile=profile,
                )
            )
            chrome_extension_id = await _discover_stagehand_extension(
                session.websocket_url
            )
            network_guard = await BrowserNetworkGuard.create(
                session.websocket_url,
                allow_private_network=self._allow_private_network,
            )
            browser = await local_browser.connect(
                cdp_url=session.websocket_url,
                extension_id=chrome_extension_id,
            )
            stagehand = await Stagehand.create(
                browser=browser,
                model=self._model,
                logging={"level": "off"},
            )
            return StagehandSteelSession(
                steel=self._steel,
                steel_session_id=session.id,
                provider_profile_id=session.profile_id,
                browser=browser,
                stagehand=stagehand,
                network_guard=network_guard,
                session_policy=self._session_policy,
                profile_ready_timeout_seconds=min(
                    60.0, self._session_timeout_ms / 1000
                ),
            )
        except asyncio.CancelledError:
            if stagehand is not None:
                await _quiet_close(stagehand.close)
            if browser is not None:
                await _quiet_close(browser.close)
            if network_guard is not None:
                await _quiet_close(network_guard.close)
            if session is not None:
                await _quiet_release(self._steel, session.id)
            raise
        except Exception:
            if stagehand is not None:
                await _quiet_close(stagehand.close)
            if browser is not None:
                await _quiet_close(browser.close)
            if network_guard is not None:
                await _quiet_close(network_guard.close)
            if session is not None:
                await _quiet_release(self._steel, session.id)
            raise BrowserBackendError(
                "browser provider could not start a session"
            ) from None

    async def close(self) -> None:
        try:
            if self._extension_id is not None:
                try:
                    await self._steel.extensions.delete(self._extension_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    audit_event(
                        "extension_cleanup",
                        outcome="failed",
                        reason="provider_error",
                    )
        finally:
            await self._steel.close()

    async def _ensure_extension(self) -> str:
        if self._extension_id is not None:
            return self._extension_id
        async with self._extension_lock:
            if self._extension_id is not None:
                return self._extension_id
            try:
                version = await asyncio.to_thread(metadata.version, "stagehand")
                archive = await asyncio.to_thread(_stagehand_extension_archive)
                digest = hashlib.sha256(archive, usedforsecurity=True).hexdigest()[:16]
                name = (
                    f"stagehand-runtime-{version}-{digest}-{secrets.token_hex(6)}.zip"
                )
                uploaded = await self._steel.extensions.upload(
                    file=(name, archive, "application/zip")
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                raise BrowserBackendError(
                    "Stagehand runtime extension could not be provisioned"
                ) from None
            self._extension_id = uploaded.id
            return uploaded.id


class StagehandSteelSession:
    def __init__(
        self,
        *,
        steel: AsyncSteel,
        steel_session_id: str,
        provider_profile_id: str | None,
        browser: StagehandBrowser,
        stagehand: Stagehand,
        network_guard: BrowserNetworkGuard,
        session_policy: SteelSessionPolicy,
        profile_ready_timeout_seconds: float,
        rng: random.Random | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._steel = steel
        self._steel_session_id = steel_session_id
        self._profile_id = provider_profile_id
        self._browser = browser
        self._stagehand = stagehand
        self._network_guard = network_guard
        self._session_policy = session_policy
        self._profile_ready_timeout_seconds = profile_ready_timeout_seconds
        self._rng = rng or random.SystemRandom()
        self._sleep = sleep
        self._provider_released = False
        self._closed = False

    @property
    def profile_id(self) -> str | None:
        return self._profile_id

    async def navigate(self, url: str, timeout_ms: int) -> BackendNavigation:
        self._network_guard.ensure_healthy()
        page = await self._page()
        response = await page.goto(
            url, wait_until="domcontentloaded", timeout=timeout_ms
        )
        await self._wait_for_captcha_idle(detection_grace=True)
        self._network_guard.ensure_healthy()
        return BackendNavigation(
            url=await page.url(),
            title=await page.title(),
            status=None if response is None else response.status,
        )

    async def act(self, instruction: str, timeout_seconds: float) -> BackendAct:
        self._network_guard.ensure_healthy()
        await self._wait_for_captcha_idle()
        result = await self._stagehand.act(instruction, timeout=timeout_seconds * 1000)
        await self._wait_for_captcha_idle(detection_grace=True)
        self._network_guard.ensure_healthy()
        return BackendAct(success=result.data.success)

    async def observe(
        self, instruction: str | None, timeout_seconds: float
    ) -> list[BackendObservation]:
        self._network_guard.ensure_healthy()
        await self._wait_for_captcha_idle()
        result = await self._stagehand.observe(
            instruction, timeout=timeout_seconds * 1000
        )
        self._network_guard.ensure_healthy()
        return [
            BackendObservation(
                selector=action.selector,
                description=action.description,
                method=action.method,
            )
            for action in result.data
        ]

    async def extract(self, instruction: str, timeout_seconds: float) -> str:
        self._network_guard.ensure_healthy()
        await self._wait_for_captcha_idle()
        result = await self._stagehand.extract(
            instruction, DefaultExtract, timeout=timeout_seconds * 1000
        )
        self._network_guard.ensure_healthy()
        return result.data.extraction

    async def control(
        self, command: str, selector: str | None, value: str | None
    ) -> str | None:
        self._network_guard.ensure_healthy()
        await self._wait_for_captcha_idle()
        page = await self._page()
        if command == "title":
            return await page.title()
        if command == "url":
            return await page.url()
        if command == "key_press":
            if value is None:
                raise ValueError("key_press requires value")
            await page.key_press(value, delay=self._typing_delay_ms())
            await self._wait_for_captcha_idle(detection_grace=True)
            self._network_guard.ensure_healthy()
            return None
        if selector is None:
            raise ValueError(f"{command} requires selector")
        locator = page.locator(selector)
        if command == "click":
            await self._point_and_click(locator)
            await self._wait_for_captcha_idle(detection_grace=True)
            self._network_guard.ensure_healthy()
            return None
        if command == "text":
            return await locator.inner_text()
        if value is None:
            raise ValueError(f"{command} requires value")
        if command == "fill":
            await self._point_and_click(locator)
            await locator.fill("")
            if value:
                await locator.type(value, delay=self._typing_delay_ms())
            await self._wait_for_captcha_idle(detection_grace=True)
            self._network_guard.ensure_healthy()
            return None
        if command == "type":
            await self._point_and_click(locator)
            await locator.type(value, delay=self._typing_delay_ms())
            await self._wait_for_captcha_idle(detection_grace=True)
            self._network_guard.ensure_healthy()
            return None
        raise ValueError("unsupported browser control command")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        cancellation: asyncio.CancelledError | None = None
        for close in (
            self._stagehand.close,
            self._browser.close,
            self._network_guard.close,
        ):
            try:
                with contextlib.suppress(Exception):
                    await close()
            except asyncio.CancelledError as exc:
                cancellation = exc
        if not self._provider_released:
            try:
                await self._steel.sessions.release(self._steel_session_id)
                self._provider_released = True
            except asyncio.CancelledError as exc:
                self._closed = False
                raise exc
            except Exception:
                self._closed = False
                raise BrowserBackendError("browser provider release failed") from None
        try:
            if self._profile_id is not None:
                await _wait_for_profile_ready(
                    self._steel,
                    self._profile_id,
                    timeout_seconds=self._profile_ready_timeout_seconds,
                )
        except asyncio.CancelledError as exc:
            cancellation = exc
        except Exception:
            self._closed = False
            raise BrowserBackendError(
                "persistent browser profile is not ready"
            ) from None
        if cancellation is not None:
            self._closed = False
            raise cancellation

    async def _page(self) -> Page:
        page = await self._browser.context.active_page()
        if page is None:
            raise BrowserBackendError("browser session has no active page")
        return page

    async def _interaction_pause(self) -> None:
        delay_ms = self._rng.uniform(
            self._session_policy.click_delay_min_ms,
            self._session_policy.click_delay_max_ms,
        )
        if delay_ms > 0:
            await self._sleep(delay_ms / 1000)

    async def _point_and_click(self, locator: Locator) -> None:
        await locator.hover()
        await self._interaction_pause()
        await locator.click()

    def _typing_delay_ms(self) -> float:
        return self._rng.uniform(
            self._session_policy.typing_delay_min_ms,
            self._session_policy.typing_delay_max_ms,
        )

    async def _wait_for_captcha_idle(self, *, detection_grace: bool = False) -> None:
        if not self._session_policy.solve_captcha:
            return
        if detection_grace:
            await self._sleep(self._session_policy.captcha_poll_interval_seconds)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._session_policy.captcha_timeout_seconds
        while True:
            try:
                states = await self._steel.sessions.captchas.status(
                    self._steel_session_id
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                raise BrowserBackendError(
                    "automatic CAPTCHA status check failed"
                ) from None
            if _captcha_failed(states):
                raise BrowserBackendError("automatic CAPTCHA solving failed")
            if not any(state.is_solving_captcha for state in states):
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise BrowserBackendError("automatic CAPTCHA solving timed out")
            await self._sleep(
                min(self._session_policy.captcha_poll_interval_seconds, remaining)
            )


def _session_create_params(
    policy: SteelSessionPolicy,
    *,
    extension_id: str,
    session_timeout_ms: int,
    persistent: bool,
    profile_id: str | None,
    profile: ProfileGetResponse | None,
) -> SessionCreateParams:
    width = policy.width
    height = policy.height
    if profile is not None and profile.dimensions is not None:
        width = int(profile.dimensions.width)
        height = int(profile.dimensions.height)

    params: SessionCreateParams = {
        "extension_ids": [extension_id],
        "headless": policy.headless,
        "device_config": {"device": "desktop"},
        "dimensions": {"width": width, "height": height},
        "persist_profile": persistent,
        "api_timeout": session_timeout_ms,
    }
    if profile_id is not None:
        params["profile_id"] = profile_id
    if policy.proxy_url is not None:
        params["proxy_url"] = policy.proxy_url
    if policy.cloud_features:
        stealth_config: StealthConfig = {
            "humanize_interactions": policy.humanize_interactions,
            "skip_fingerprint_injection": False,
        }
        if policy.solve_captcha:
            stealth_config["auto_captcha_solving"] = True
        params["stealth_config"] = stealth_config
        params["solve_captcha"] = policy.solve_captcha
        if policy.region is not None:
            params["region"] = policy.region
    return params


def _captcha_failed(states: list[CaptchaStatusResponseItem]) -> bool:
    failed_statuses = {"error", "failed_to_detect", "failed_to_solve"}
    for state in states:
        for task in state.tasks:
            if isinstance(task, Mapping) and task.get("status") in failed_statuses:
                return True
    return False


async def _discover_stagehand_extension(websocket_url: str) -> str:
    try:
        async with connect(
            websocket_url,
            open_timeout=10,
            close_timeout=5,
            max_size=1_048_576,
        ) as websocket:
            async with asyncio.timeout(10):
                request_id = 0
                while True:
                    request_id += 1
                    targets_response = await _cdp_call(
                        websocket, request_id, "Target.getTargets"
                    )
                    targets = targets_response.get("result", {}).get("targetInfos", [])
                    for target in targets:
                        match = _EXTENSION_URL.fullmatch(str(target.get("url", "")))
                        if match is None or match.group(1) != _EXTENSION_ID:
                            continue
                        request_id += 1
                        attached = await _cdp_call(
                            websocket,
                            request_id,
                            "Target.attachToTarget",
                            {"targetId": target.get("targetId"), "flatten": True},
                        )
                        session_id = attached.get("result", {}).get("sessionId")
                        if not isinstance(session_id, str):
                            continue
                        request_id += 1
                        manifest = await _cdp_call(
                            websocket,
                            request_id,
                            "Runtime.evaluate",
                            {
                                "expression": "chrome.runtime.getManifest()",
                                "returnByValue": True,
                            },
                            session_id=session_id,
                        )
                        manifest_value = (
                            manifest.get("result", {}).get("result", {}).get("value")
                        )
                        if (
                            isinstance(manifest_value, dict)
                            and manifest_value.get("name") == "Stagehand Runtime"
                            and manifest_value.get("key") == _EXTENSION_PUBLIC_KEY
                        ):
                            return _EXTENSION_ID
                    await asyncio.sleep(0.1)
    except (OSError, TimeoutError, ValueError, TypeError):
        pass
    raise BrowserBackendError("Stagehand runtime extension did not start")


async def _cdp_call(
    websocket: ClientConnection,
    request_id: int,
    method: str,
    params: dict[str, object] | None = None,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    request: dict[str, object] = {"id": request_id, "method": method}
    if params is not None:
        request["params"] = params
    if session_id is not None:
        request["sessionId"] = session_id
    await websocket.send(json.dumps(request))
    while True:
        raw = await websocket.recv()
        response = json.loads(raw)
        if not isinstance(response, dict):
            raise TypeError("CDP response must be an object")
        if response.get("id") == request_id:
            return cast("dict[str, Any]", response)


def _stagehand_extension_archive() -> bytes:
    root = resources.files("stagehand").joinpath("_extension")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _add_tree(archive, root, "")
    return buffer.getvalue()


def _add_tree(archive: zipfile.ZipFile, root: Traversable, prefix: str) -> None:
    for child in root.iterdir():
        relative = f"{prefix}{child.name}"
        if child.is_dir():
            _add_tree(archive, child, f"{relative}/")
        elif child.is_file():
            content = child.read_bytes()
            if relative == "manifest.json":
                manifest = json.loads(content)
                if not isinstance(manifest, dict):
                    raise TypeError("Stagehand extension manifest must be an object")
                manifest["key"] = _EXTENSION_PUBLIC_KEY
                content = json.dumps(
                    manifest,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            archive.writestr(relative, content)


def _extension_id_from_public_key(public_key: str) -> str:
    public_key_bytes = base64.b64decode(public_key, validate=True)
    digest = hashlib.sha256(public_key_bytes, usedforsecurity=True).digest()[:16]
    return "".join(
        chr(ord("a") + nibble) for byte in digest for nibble in (byte >> 4, byte & 0x0F)
    )


if _extension_id_from_public_key(_EXTENSION_PUBLIC_KEY) != _EXTENSION_ID:
    raise RuntimeError("configured Stagehand extension identity is invalid")


async def _quiet_close(close: Callable[[], Awaitable[None]]) -> None:
    with contextlib.suppress(Exception):
        await close()


async def _quiet_release(steel: AsyncSteel, session_id: str) -> None:
    with contextlib.suppress(Exception):
        await steel.sessions.release(session_id)


async def _wait_for_profile_ready(
    steel: AsyncSteel, profile_id: str, *, timeout_seconds: float
) -> ProfileGetResponse:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        profile = await steel.profiles.get(profile_id)
        if profile.status == "READY":
            return profile
        if profile.status == "FAILED":
            raise BrowserBackendError("persistent browser profile upload failed")
        if asyncio.get_running_loop().time() >= deadline:
            raise BrowserBackendError("persistent browser profile upload timed out")
        await asyncio.sleep(0.25)
