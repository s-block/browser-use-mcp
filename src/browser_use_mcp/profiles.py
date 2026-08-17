from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import os
import re
import time
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self, cast

import vaultlet
from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from pathlib import Path

    from browser_use_mcp.security import Principal

_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_RECORD_BYTES = 65_536
_MAX_PROFILE_COUNT = 1_000


class ProfileError(RuntimeError):
    """Base class for safe profile-store errors."""


class InvalidProfileNameError(ProfileError):
    pass


class ProfileTamperedError(ProfileError):
    pass


class ProfileBusyError(ProfileError):
    pass


class ProfileRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    provider_profile_id: str = Field(repr=False)
    network_identity: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    created_at: datetime
    updated_at: datetime


class ProfileSummary(BaseModel):
    name: str
    created_at: datetime
    updated_at: datetime


class ProfileStore:
    """Stores profile locators in one encrypted, multi-process Vaultlet database."""

    def __init__(self, store: vaultlet.Vaultlet) -> None:
        self._store = store

    @classmethod
    async def open(cls, state_dir: Path, master_key: bytes) -> Self:
        await asyncio.to_thread(
            state_dir.mkdir, mode=0o700, parents=True, exist_ok=True
        )
        try:
            store = await vaultlet.Vaultlet.open(
                vaultlet.FileBackend(state_dir / "profiles.vaultlet"),
                key=vaultlet.MasterKey.from_bytes(master_key),
            )
        except vaultlet.VaultletError as exc:
            raise ProfileError("encrypted profile storage could not be opened") from exc
        return cls(store)

    async def aclose(self) -> None:
        await self._store.aclose()

    async def get(self, principal: Principal, name: str) -> ProfileRecord | None:
        validate_profile_name(name)
        try:
            value = await self._tenant(principal).get_json(name)
        except vaultlet.VaultletError as exc:
            raise _storage_error(exc) from exc
        if value is None:
            return None
        return _profile_record(value, expected_name=name)

    async def put(
        self,
        principal: Principal,
        name: str,
        provider_profile_id: str,
        *,
        network_identity: str,
    ) -> ProfileRecord:
        validate_profile_name(name)
        existing = await self.get(principal, name)
        now = datetime.now(UTC)
        record = ProfileRecord(
            name=name,
            provider_profile_id=provider_profile_id,
            network_identity=network_identity,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
        )
        if len(record.model_dump_json().encode()) > _MAX_RECORD_BYTES:
            raise ProfileError("profile record exceeds the safety limit")
        value = cast("dict[str, vaultlet.JsonInput]", record.model_dump(mode="json"))
        try:
            await self._tenant(principal).set_json(name, value)
        except vaultlet.VaultletError as exc:
            raise _storage_error(exc) from exc
        return record

    async def list(self, principal: Principal) -> list[ProfileSummary]:
        tenant = self._tenant(principal)
        try:
            listing = await tenant.keys(limit=_MAX_PROFILE_COUNT)
            if listing.has_more:
                raise ProfileError("profile count exceeds the configured safety limit")
            values = await tenant.get_many_json(listing.keys)
        except vaultlet.VaultletError as exc:
            raise _storage_error(exc) from exc
        records = [
            _profile_record(values[key], expected_name=key)
            for key in listing.keys
            if key in values
        ]
        return sorted(
            [
                ProfileSummary(
                    name=record.name,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                )
                for record in records
            ],
            key=lambda item: item.name,
        )

    @staticmethod
    def locator(name: str) -> str:
        validate_profile_name(name)
        return hashlib.sha256(
            f"browser-use-mcp/profile-lease/v1/{name}".encode(),
            usedforsecurity=True,
        ).hexdigest()

    def _tenant(self, principal: Principal) -> vaultlet.TenantStore:
        try:
            return self._store.tenant(principal.tenant_id)
        except vaultlet.VaultletError as exc:
            raise _storage_error(exc) from exc


@dataclass(slots=True)
class ProfileLease:
    _descriptor: int
    _process_lock: asyncio.Lock
    _released: bool = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            await asyncio.to_thread(_unlock_and_close, self._descriptor)
        finally:
            self._process_lock.release()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.release()


class ProfileLeaseManager:
    """Combines per-process async locks with non-blocking OS file leases."""

    def __init__(self, state_dir: Path, timeout_seconds: float) -> None:
        self._root = state_dir / "profile-locks"
        self._timeout = timeout_seconds
        self._locks: weakref.WeakValueDictionary[Path, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._locks_guard = asyncio.Lock()

    async def acquire(self, principal: Principal, locator: str) -> ProfileLease:
        path = self._root / principal.namespace_id / f"{locator}.lock"
        async with self._locks_guard:
            process_lock = self._locks.setdefault(path, asyncio.Lock())
        try:
            await asyncio.wait_for(process_lock.acquire(), timeout=self._timeout)
        except TimeoutError as exc:
            raise ProfileBusyError("profile is already in use") from exc

        descriptor = -1
        try:
            descriptor = await asyncio.to_thread(_open_lock_file, path)
            await self._acquire_file_lock(descriptor)
        except BaseException:
            if descriptor >= 0:
                await asyncio.to_thread(os.close, descriptor)
            process_lock.release()
            raise
        return ProfileLease(descriptor, process_lock)

    async def _acquire_file_lock(self, descriptor: int) -> None:
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                await asyncio.to_thread(
                    fcntl.flock, descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB
                )
                return
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise ProfileBusyError("profile is already in use") from exc
                await asyncio.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def validate_profile_name(name: str) -> None:
    if _PROFILE_NAME.fullmatch(name) is None:
        raise InvalidProfileNameError(
            "profile name must be 1-64 characters using letters, numbers, '.', "
            "'_' or '-'"
        )


def _profile_record(value: vaultlet.JsonValue, *, expected_name: str) -> ProfileRecord:
    try:
        record = ProfileRecord.model_validate(value)
    except (TypeError, ValidationError) as exc:
        raise ProfileTamperedError("profile record authentication failed") from exc
    if record.name != expected_name:
        raise ProfileTamperedError("profile record name does not match its key")
    return record


def _storage_error(error: vaultlet.VaultletError) -> ProfileError:
    if isinstance(
        error,
        (
            vaultlet.IntegrityError,
            vaultlet.SerializationError,
            vaultlet.TypeMismatchError,
        ),
    ):
        return ProfileTamperedError("profile record authentication failed")
    return ProfileError("encrypted profile storage operation failed")


def _open_lock_file(path: Path) -> int:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        if exc.errno == errno.EACCES:
            raise ProfileError("profile lock directory is not writable") from exc
        raise


def _unlock_and_close(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
