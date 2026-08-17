from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from browser_use_mcp.backend import (
    BackendAct,
    BackendNavigation,
    BackendObservation,
    BrowserBackendSession,
)
from browser_use_mcp.llm import LLMConfig
from browser_use_mcp.models import BrowserControlCommand
from browser_use_mcp.profiles import (
    ProfileBusyError,
    ProfileLeaseManager,
    ProfileStore,
)
from browser_use_mcp.security import Authenticator
from browser_use_mcp.sessions import (
    BrowserOperationError,
    BrowserSessionService,
    SessionNotFoundError,
)
from tests.conftest import make_settings
from tests.test_security import TOKEN_A, TOKEN_B

if TYPE_CHECKING:
    from pathlib import Path


class FakeSession:
    def __init__(self, profile_id: str | None) -> None:
        self._profile_id = profile_id
        self.closed = False
        self.close_failures_remaining = 0
        self.active_controls = 0
        self.maximum_active_controls = 0

    @property
    def profile_id(self) -> str | None:
        return self._profile_id

    async def navigate(self, url: str, timeout_ms: int) -> BackendNavigation:
        assert timeout_ms > 0
        return BackendNavigation(url=url, title="Example", status=200)

    async def act(self, instruction: str, timeout_seconds: float) -> BackendAct:
        assert instruction
        assert timeout_seconds > 0
        return BackendAct(True)

    async def observe(
        self, instruction: str | None, timeout_seconds: float
    ) -> list[BackendObservation]:
        assert timeout_seconds > 0
        return [BackendObservation("#submit", instruction or "submit", "click")]

    async def extract(self, instruction: str, timeout_seconds: float) -> str:
        assert instruction
        assert timeout_seconds > 0
        return "extracted"

    async def control(
        self, command: str, selector: str | None, value: str | None
    ) -> str | None:
        del command, selector, value
        self.active_controls += 1
        self.maximum_active_controls = max(
            self.maximum_active_controls, self.active_controls
        )
        await asyncio.sleep(0.01)
        self.active_controls -= 1
        return "controlled"

    async def close(self) -> None:
        if self.close_failures_remaining:
            self.close_failures_remaining -= 1
            raise RuntimeError("provider release failed")
        self.closed = True


class FakeBackend:
    def __init__(self) -> None:
        self.starts: list[tuple[str | None, bool]] = []
        self.sessions: list[FakeSession] = []
        self.closed = False

    async def start(
        self, *, profile_id: str | None, persistent: bool
    ) -> BrowserBackendSession:
        self.starts.append((profile_id, persistent))
        assigned = profile_id
        if persistent and assigned is None:
            assigned = f"profile-{len(self.starts)}"
        session = FakeSession(assigned)
        self.sessions.append(session)
        return session

    async def close(self) -> None:
        self.closed = True


class FailOnceBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self._failed = False

    async def start(
        self, *, profile_id: str | None, persistent: bool
    ) -> BrowserBackendSession:
        if not self._failed:
            self._failed = True
            raise RuntimeError("provider test failure")
        return await super().start(profile_id=profile_id, persistent=persistent)


async def _service(
    tmp_path: Path, *, tokens: tuple[str, ...] = (TOKEN_A,)
) -> tuple[BrowserSessionService, FakeBackend, Authenticator]:
    settings = make_settings(tmp_path, bearer_tokens=tokens)
    backend = FakeBackend()
    profiles = await ProfileStore.open(tmp_path, settings.storage_master_key)
    return (
        BrowserSessionService(
            settings,
            backend,
            profiles,
            ProfileLeaseManager(tmp_path, 0.1),
        ),
        backend,
        Authenticator(settings),
    )


@pytest.mark.asyncio
async def test_persistent_profile_is_reused_after_session_restart(
    tmp_path: Path,
) -> None:
    service, backend, authenticator = await _service(tmp_path)
    principal = await authenticator.principal({"Authorization": f"Bearer {TOKEN_A}"})

    first = await service.start(principal, "work")
    await service.close(principal, first.session_id)
    second = await service.start(principal, "work")

    assert backend.starts == [(None, True), ("profile-1", True)]
    profiles = await service.list_profiles(principal)
    assert [profile.name for profile in profiles.profiles] == ["work"]
    await service.close(principal, second.session_id)
    await service.shutdown()


@pytest.mark.asyncio
async def test_persistent_profile_rejects_changed_network_identity(
    tmp_path: Path,
) -> None:
    first_settings = make_settings(tmp_path, bearer_tokens=(TOKEN_A,))
    first_backend = FakeBackend()
    first_profiles = await ProfileStore.open(
        tmp_path, first_settings.storage_master_key
    )
    first_service = BrowserSessionService(
        first_settings,
        first_backend,
        first_profiles,
        ProfileLeaseManager(tmp_path, 0.1),
    )
    principal = await Authenticator(first_settings).principal(
        {"Authorization": f"Bearer {TOKEN_A}"}
    )
    started = await first_service.start(principal, "work")
    await first_service.close(principal, started.session_id)
    await first_service.shutdown()

    changed_settings = make_settings(
        tmp_path,
        bearer_tokens=(TOKEN_A,),
        env_overrides={"BROWSER_USE_MCP_STEEL_NETWORK_IDENTITY": "dedicated-proxy-2"},
    )
    changed_backend = FakeBackend()
    changed_profiles = await ProfileStore.open(
        tmp_path, changed_settings.storage_master_key
    )
    changed_service = BrowserSessionService(
        changed_settings,
        changed_backend,
        changed_profiles,
        ProfileLeaseManager(tmp_path, 0.1),
    )

    with pytest.raises(BrowserOperationError, match="different network identity"):
        await changed_service.start(principal, "work")

    assert changed_backend.starts == []
    await changed_service.shutdown()


@pytest.mark.asyncio
async def test_temporary_sessions_do_not_create_profiles(tmp_path: Path) -> None:
    service, backend, authenticator = await _service(tmp_path)
    principal = await authenticator.principal({"Authorization": f"Bearer {TOKEN_A}"})

    session = await service.start(principal, None)

    assert backend.starts == [(None, False)]
    assert (await service.list_profiles(principal)).profiles == []
    await service.close(principal, session.session_id)
    await service.shutdown()


@pytest.mark.asyncio
async def test_named_profile_cannot_have_two_writers(tmp_path: Path) -> None:
    service, _backend, authenticator = await _service(tmp_path)
    principal = await authenticator.principal({"Authorization": f"Bearer {TOKEN_A}"})
    first = await service.start(principal, "work")

    with pytest.raises(ProfileBusyError):
        await service.start(principal, "work")

    await service.close(principal, first.session_id)
    second = await service.start(principal, "work")
    await service.close(principal, second.session_id)
    await service.shutdown()


@pytest.mark.asyncio
async def test_failed_start_releases_capacity_and_profile_lease(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, bearer_tokens=(TOKEN_A,), max_sessions=1)
    backend = FailOnceBackend()
    profiles = await ProfileStore.open(tmp_path, settings.storage_master_key)
    service = BrowserSessionService(
        settings,
        backend,
        profiles,
        ProfileLeaseManager(tmp_path, 0.1),
    )
    principal = await Authenticator(settings).principal(
        {"Authorization": f"Bearer {TOKEN_A}"}
    )

    with pytest.raises(BrowserOperationError, match="browser operation failed"):
        await service.start(principal, "work")
    started = await service.start(principal, "work")

    await service.close(principal, started.session_id)
    await service.shutdown()


@pytest.mark.asyncio
async def test_failed_close_keeps_profile_lease_until_retry(tmp_path: Path) -> None:
    service, backend, authenticator = await _service(tmp_path)
    principal = await authenticator.principal({"Authorization": f"Bearer {TOKEN_A}"})
    started = await service.start(principal, "work")
    backend.sessions[0].close_failures_remaining = 1

    with pytest.raises(BrowserOperationError, match="browser operation failed"):
        await service.close(principal, started.session_id)
    with pytest.raises(ProfileBusyError):
        await service.start(principal, "work")

    await service.close(principal, started.session_id)
    replacement = await service.start(principal, "work")
    await service.close(principal, replacement.session_id)
    await service.shutdown()


@pytest.mark.asyncio
async def test_browser_operations_are_serialized_per_session(tmp_path: Path) -> None:
    service, backend, authenticator = await _service(tmp_path)
    principal = await authenticator.principal({"Authorization": f"Bearer {TOKEN_A}"})
    started = await service.start(principal, None)

    await asyncio.gather(
        service.control(
            principal,
            started.session_id,
            BrowserControlCommand.TITLE,
            None,
            None,
        ),
        service.control(
            principal,
            started.session_id,
            BrowserControlCommand.URL,
            None,
            None,
        ),
    )

    assert backend.sessions[0].maximum_active_controls == 1
    await service.close(principal, started.session_id)
    await service.shutdown()


@pytest.mark.asyncio
async def test_cross_client_session_access_is_hidden(tmp_path: Path) -> None:
    service, _backend, authenticator = await _service(
        tmp_path, tokens=(TOKEN_A, TOKEN_B)
    )
    first = await authenticator.principal({"Authorization": f"Bearer {TOKEN_A}"})
    second = await authenticator.principal({"Authorization": f"Bearer {TOKEN_B}"})
    started = await service.start(first, None)

    with pytest.raises(SessionNotFoundError):
        await service.navigate(second, started.session_id, "https://8.8.8.8")

    await service.close(first, started.session_id)
    await service.shutdown()


@pytest.mark.asyncio
async def test_navigation_results_remove_sensitive_url_components(
    tmp_path: Path,
) -> None:
    service, _backend, authenticator = await _service(tmp_path)
    principal = await authenticator.principal({"Authorization": f"Bearer {TOKEN_A}"})
    started = await service.start(principal, None)

    result = await service.navigate(
        principal,
        started.session_id,
        "https://example.com/callback?code=sensitive#fragment",
    )

    assert result.url == "https://example.com/callback"
    await service.close(principal, started.session_id)
    await service.shutdown()


@pytest.mark.asyncio
async def test_semantic_browser_behaviour_uses_small_results(tmp_path: Path) -> None:
    service, _backend, authenticator = await _service(tmp_path)
    principal = await authenticator.principal({"Authorization": f"Bearer {TOKEN_A}"})
    started = await service.start(principal, None)
    llm = LLMConfig("http://llm.internal/v1", "model", "key")

    act = await service.act(principal, started.session_id, "click", llm)
    observed = await service.observe(principal, started.session_id, None, llm)
    extracted = await service.extract(principal, started.session_id, "extract", llm)

    assert act.success is True
    assert observed.actions[0].selector == "#submit"
    assert extracted.extraction == "extracted"
    await service.close(principal, started.session_id)
    await service.shutdown()
