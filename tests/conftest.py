from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING

from browser_use_mcp.config import Settings

if TYPE_CHECKING:
    from pathlib import Path


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8"), usedforsecurity=True).hexdigest()


def make_settings(
    state_dir: Path,
    *,
    bearer_tokens: tuple[str, ...] = (),
    tenant_ids: tuple[str, ...] | None = None,
    max_sessions: int = 4,
    profile_lock_timeout: float = 0.1,
    env_overrides: dict[str, str] | None = None,
) -> Settings:
    env = {
        "BROWSER_USE_MCP_STATE_DIR": str(state_dir),
        "BROWSER_USE_MCP_MAX_SESSIONS": str(max_sessions),
        "BROWSER_USE_MCP_PROFILE_LOCK_TIMEOUT_SECONDS": str(profile_lock_timeout),
        "BROWSER_USE_MCP_OPERATION_TIMEOUT_SECONDS": "2",
        "BROWSER_USE_MCP_ALLOW_PRIVATE_NETWORK": "true",
        "BROWSER_USE_MCP_STORAGE_MASTER_KEY": base64.b64encode(
            bytes(range(32))
        ).decode(),
    }
    if bearer_tokens:
        assigned_tenants = tenant_ids or tuple(
            f"client-{index}" for index in range(len(bearer_tokens))
        )
        if len(assigned_tenants) != len(bearer_tokens):
            raise ValueError("tenant_ids must match bearer_tokens")
        env["BROWSER_USE_MCP_AUTH_MODE"] = "bearer"
        env["BROWSER_USE_MCP_CLIENT_CREDENTIALS"] = ",".join(
            f"{tenant_id}={token_hash(token)}"
            for tenant_id, token in zip(assigned_tenants, bearer_tokens, strict=True)
        )
    if env_overrides is not None:
        env.update(env_overrides)
    return Settings.from_env(env)
