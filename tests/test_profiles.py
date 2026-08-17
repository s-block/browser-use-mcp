from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from browser_use_mcp.profiles import (
    ProfileBusyError,
    ProfileError,
    ProfileLeaseManager,
    ProfileStore,
    ProfileTamperedError,
)
from browser_use_mcp.security import Authenticator
from tests.conftest import make_settings
from tests.test_security import TOKEN_A, TOKEN_B

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_encrypted_profile_persists_without_plaintext(tmp_path: Path) -> None:
    state_dir = tmp_path / "new-state-directory"
    settings = make_settings(state_dir, bearer_tokens=(TOKEN_A,))
    principal = await Authenticator(settings).principal(
        {"Authorization": f"Bearer {TOKEN_A}"}
    )
    store = await ProfileStore.open(state_dir, settings.storage_master_key)

    written = await store.put(
        principal,
        "work",
        "steel-profile-sensitive-id",
        network_identity="direct-auto",
    )
    await store.aclose()
    reopened = await ProfileStore.open(state_dir, settings.storage_master_key)
    loaded = await reopened.get(principal, "work")
    await reopened.aclose()

    assert loaded == written
    raw = await asyncio.to_thread(_profile_storage_bytes, state_dir)
    assert b"steel-profile-sensitive-id" not in raw
    assert b"direct-auto" not in raw
    assert b'"work"' not in raw


@pytest.mark.asyncio
async def test_invalid_profile_record_is_rejected(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, bearer_tokens=(TOKEN_A,))
    principal = await Authenticator(settings).principal(
        {"Authorization": f"Bearer {TOKEN_A}"}
    )
    store = await ProfileStore.open(tmp_path, settings.storage_master_key)
    await store._store.tenant(principal.tenant_id).set_json("work", {"name": "work"})

    with pytest.raises(ProfileTamperedError):
        await store.get(principal, "work")
    await store.aclose()


@pytest.mark.asyncio
async def test_cross_client_profile_isolation(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, bearer_tokens=(TOKEN_A, TOKEN_B))
    authenticator = Authenticator(settings)
    first = await authenticator.principal({"Authorization": f"Bearer {TOKEN_A}"})
    second = await authenticator.principal({"Authorization": f"Bearer {TOKEN_B}"})
    store = await ProfileStore.open(tmp_path, settings.storage_master_key)

    await store.put(
        first,
        "same-name",
        "first-provider-profile",
        network_identity="direct-auto",
    )

    assert await store.get(second, "same-name") is None
    assert await store.list(second) == []
    await store.aclose()


@pytest.mark.asyncio
async def test_profile_listing_rejects_more_than_safety_limit(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, bearer_tokens=(TOKEN_A,))
    principal = await Authenticator(settings).principal(
        {"Authorization": f"Bearer {TOKEN_A}"}
    )
    store = await ProfileStore.open(tmp_path, settings.storage_master_key)
    keys = (f"profile-{index}" for index in range(1_001))
    await store._store.tenant(principal.tenant_id).set_many(dict.fromkeys(keys, b"x"))

    with pytest.raises(
        ProfileError, match="profile count exceeds the configured safety limit"
    ):
        await store.list(principal)
    await store.aclose()


@pytest.mark.asyncio
async def test_rotated_credential_keeps_profile_access(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        bearer_tokens=(TOKEN_A, TOKEN_B),
        tenant_ids=("customer", "customer"),
    )
    authenticator = Authenticator(settings)
    old_credential = await authenticator.principal(
        {"Authorization": f"Bearer {TOKEN_A}"}
    )
    new_credential = await authenticator.principal(
        {"Authorization": f"Bearer {TOKEN_B}"}
    )
    store = await ProfileStore.open(tmp_path, settings.storage_master_key)
    await store.put(
        old_credential,
        "work",
        "provider-profile",
        network_identity="direct-auto",
    )

    rotated = await store.get(new_credential, "work")

    assert rotated is not None
    assert rotated.provider_profile_id == "provider-profile"
    await store.aclose()


@pytest.mark.asyncio
async def test_profile_lease_prevents_concurrent_writers(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path, bearer_tokens=(TOKEN_A,), profile_lock_timeout=0.1
    )
    principal = await Authenticator(settings).principal(
        {"Authorization": f"Bearer {TOKEN_A}"}
    )
    locator = ProfileStore.locator("work")
    first_manager = ProfileLeaseManager(tmp_path, 0.1)
    second_manager = ProfileLeaseManager(tmp_path, 0.1)
    lease = await first_manager.acquire(principal, locator)
    try:
        with pytest.raises(ProfileBusyError):
            await second_manager.acquire(principal, locator)
    finally:
        await lease.release()

    second_lease = await second_manager.acquire(principal, locator)
    await second_lease.release()


def _profile_storage_bytes(state_dir: Path) -> bytes:
    return b"".join(path.read_bytes() for path in state_dir.glob("profiles.vaultlet*"))
