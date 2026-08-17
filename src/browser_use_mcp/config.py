from __future__ import annotations

import base64
import binascii
import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """Raised when server configuration is unsafe or incomplete."""


class AuthMode(StrEnum):
    NONE = "none"
    BEARER = "bearer"


class ServerTransport(StrEnum):
    HTTP = "http"
    STDIO = "stdio"


@dataclass(frozen=True, slots=True)
class ClientCredential:
    tenant_id: str
    token_hash: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class LLMDefaults:
    base_url: str | None
    api_key: str | None = field(repr=False)
    model: str | None
    allowed_origins: tuple[str, ...] = ()
    allow_insecure_http: bool = False


@dataclass(frozen=True, slots=True)
class SteelSessionPolicy:
    cloud_features: bool
    headless: bool
    width: int
    height: int
    region: str | None
    humanize_interactions: bool
    solve_captcha: bool
    captcha_timeout_seconds: float
    captcha_poll_interval_seconds: float
    network_identity: str
    proxy_url: str | None = field(default=None, repr=False)
    typing_delay_min_ms: float = 45.0
    typing_delay_max_ms: float = 120.0
    click_delay_min_ms: float = 80.0
    click_delay_max_ms: float = 250.0


@dataclass(frozen=True, slots=True)
class Settings:
    transport: ServerTransport
    host: str
    port: int
    state_dir: Path
    auth_mode: AuthMode
    client_credentials: tuple[ClientCredential, ...]
    storage_master_key: bytes = field(repr=False)
    allow_unauthenticated_remote: bool
    tls_terminated: bool
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    allow_private_network: bool
    public_network_egress_enforced: bool
    steel_api_key: str | None = field(repr=False)
    steel_base_url: str
    steel_session: SteelSessionPolicy
    llm: LLMDefaults
    max_sessions: int
    session_timeout_ms: int
    operation_timeout_seconds: float
    profile_lock_timeout_seconds: float
    max_response_characters: int
    max_llm_response_bytes: int

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        transport = _server_transport(
            env.get("BROWSER_USE_MCP_TRANSPORT", ServerTransport.HTTP)
        )
        auth_mode = _auth_mode(env.get("BROWSER_USE_MCP_AUTH_MODE", "none"))
        credentials = _client_credentials(
            env.get("BROWSER_USE_MCP_CLIENT_CREDENTIALS", "")
        )
        storage_master_key = _storage_master_key(
            env.get("BROWSER_USE_MCP_STORAGE_MASTER_KEY")
        )
        if auth_mode is AuthMode.BEARER and not credentials:
            raise ConfigurationError(
                "bearer authentication requires BROWSER_USE_MCP_CLIENT_CREDENTIALS"
            )
        if transport is ServerTransport.STDIO and auth_mode is AuthMode.BEARER:
            raise ConfigurationError(
                "bearer authentication requires BROWSER_USE_MCP_TRANSPORT=http"
            )

        host = env.get("BROWSER_USE_MCP_HOST", "127.0.0.1").strip()
        allow_remote = _boolean(
            env.get("BROWSER_USE_MCP_ALLOW_UNAUTHENTICATED_REMOTE", "false")
        )
        if (
            transport is ServerTransport.HTTP
            and auth_mode is AuthMode.NONE
            and not _is_loopback_bind(host)
            and not allow_remote
        ):
            raise ConfigurationError(
                "unauthenticated mode may only bind to loopback unless it is "
                "explicitly enabled"
            )

        tls_terminated = _boolean(env.get("BROWSER_USE_MCP_TLS_TERMINATED", "false"))
        if (
            transport is ServerTransport.HTTP
            and not _is_loopback_bind(host)
            and not tls_terminated
        ):
            raise ConfigurationError(
                "non-loopback HTTP requires TLS termination; set "
                "BROWSER_USE_MCP_TLS_TERMINATED=true only when a trusted reverse "
                "proxy exposes the service over HTTPS"
            )

        allow_insecure_provider_http = _boolean(
            env.get("BROWSER_USE_MCP_ALLOW_INSECURE_PROVIDER_HTTP", "false")
        )

        steel_base_url = env.get("STEEL_BASE_URL", "https://api.steel.dev").strip()
        _require_http_url(
            steel_base_url,
            "STEEL_BASE_URL",
            allow_insecure_http=allow_insecure_provider_http,
        )

        llm_base_url = _optional(env.get("BROWSER_USE_MCP_LLM_BASE_URL"))
        if llm_base_url is not None:
            _require_http_url(
                llm_base_url,
                "BROWSER_USE_MCP_LLM_BASE_URL",
                allow_insecure_http=allow_insecure_provider_http,
            )
        configured_llm_origins = _csv(
            env.get("BROWSER_USE_MCP_LLM_ALLOWED_ORIGINS", "")
        )
        if configured_llm_origins:
            llm_allowed_origins = tuple(
                _require_http_origin(
                    origin,
                    "BROWSER_USE_MCP_LLM_ALLOWED_ORIGINS",
                    allow_insecure_http=allow_insecure_provider_http,
                )
                for origin in configured_llm_origins
            )
        elif llm_base_url is not None:
            llm_allowed_origins = (_http_origin(llm_base_url),)
        else:
            llm_allowed_origins = ()
        if (
            llm_base_url is not None
            and _http_origin(llm_base_url) not in llm_allowed_origins
        ):
            raise ConfigurationError(
                "BROWSER_USE_MCP_LLM_BASE_URL origin must be included in "
                "BROWSER_USE_MCP_LLM_ALLOWED_ORIGINS"
            )

        operation_timeout_seconds = _floating(
            env,
            "BROWSER_USE_MCP_OPERATION_TIMEOUT_SECONDS",
            60.0,
            minimum=1.0,
            maximum=600.0,
        )
        allow_private_network = _boolean(
            env.get("BROWSER_USE_MCP_ALLOW_PRIVATE_NETWORK", "false")
        )
        public_network_egress_enforced = _boolean(
            env.get("BROWSER_USE_MCP_PUBLIC_NETWORK_EGRESS_ENFORCED", "false")
        )
        if not allow_private_network and not public_network_egress_enforced:
            raise ConfigurationError(
                "private-network blocking requires an authoritative Steel egress "
                "proxy or firewall; set "
                "BROWSER_USE_MCP_PUBLIC_NETWORK_EGRESS_ENFORCED=true only after "
                "that boundary is configured"
            )
        steel_cloud_features = _boolean(
            env.get(
                "BROWSER_USE_MCP_STEEL_CLOUD_FEATURES",
                "true" if _is_steel_cloud(steel_base_url) else "false",
            )
        )
        steel_region = _optional(env.get("BROWSER_USE_MCP_STEEL_REGION"))
        humanize_interactions = _boolean(
            env.get(
                "BROWSER_USE_MCP_STEEL_HUMANIZE_INTERACTIONS",
                "true" if steel_cloud_features else "false",
            )
        )
        solve_captcha = _boolean(
            env.get("BROWSER_USE_MCP_STEEL_SOLVE_CAPTCHA", "false")
        )
        if not steel_cloud_features and (
            steel_region is not None or humanize_interactions or solve_captcha
        ):
            raise ConfigurationError(
                "Steel region, interaction humanization, and CAPTCHA solving require "
                "BROWSER_USE_MCP_STEEL_CLOUD_FEATURES=true"
            )

        steel_proxy_url = _optional(env.get("BROWSER_USE_MCP_STEEL_PROXY_URL"))
        if steel_proxy_url is not None:
            _require_proxy_url(steel_proxy_url, "BROWSER_USE_MCP_STEEL_PROXY_URL")
        network_identity = _optional(env.get("BROWSER_USE_MCP_STEEL_NETWORK_IDENTITY"))
        if steel_proxy_url is not None and network_identity is None:
            raise ConfigurationError(
                "BROWSER_USE_MCP_STEEL_NETWORK_IDENTITY is required when a proxy "
                "is configured"
            )
        if network_identity is None:
            network_identity = f"direct-{steel_region or 'auto'}"
        _require_network_identity(network_identity)

        typing_delay_min_ms = _floating(
            env,
            "BROWSER_USE_MCP_TYPING_DELAY_MIN_MS",
            45.0,
            minimum=0.0,
            maximum=2_000.0,
        )
        typing_delay_max_ms = _floating(
            env,
            "BROWSER_USE_MCP_TYPING_DELAY_MAX_MS",
            120.0,
            minimum=0.0,
            maximum=2_000.0,
        )
        click_delay_min_ms = _floating(
            env,
            "BROWSER_USE_MCP_CLICK_DELAY_MIN_MS",
            80.0,
            minimum=0.0,
            maximum=2_000.0,
        )
        click_delay_max_ms = _floating(
            env,
            "BROWSER_USE_MCP_CLICK_DELAY_MAX_MS",
            250.0,
            minimum=0.0,
            maximum=2_000.0,
        )
        _require_ordered_range(
            typing_delay_min_ms,
            typing_delay_max_ms,
            "BROWSER_USE_MCP_TYPING_DELAY_MIN_MS",
            "BROWSER_USE_MCP_TYPING_DELAY_MAX_MS",
        )
        _require_ordered_range(
            click_delay_min_ms,
            click_delay_max_ms,
            "BROWSER_USE_MCP_CLICK_DELAY_MIN_MS",
            "BROWSER_USE_MCP_CLICK_DELAY_MAX_MS",
        )

        return cls(
            transport=transport,
            host=host,
            port=_integer(env, "BROWSER_USE_MCP_PORT", 8000, minimum=1, maximum=65535),
            state_dir=Path(
                env.get("BROWSER_USE_MCP_STATE_DIR", ".browser-use-mcp")
            ).expanduser(),
            auth_mode=auth_mode,
            client_credentials=credentials,
            storage_master_key=storage_master_key,
            allow_unauthenticated_remote=allow_remote,
            tls_terminated=tls_terminated,
            allowed_hosts=_csv(
                env.get(
                    "BROWSER_USE_MCP_ALLOWED_HOSTS",
                    "localhost:*,127.0.0.1:*,[::1]:*",
                )
            ),
            allowed_origins=_csv(env.get("BROWSER_USE_MCP_ALLOWED_ORIGINS", "")),
            allow_private_network=allow_private_network,
            public_network_egress_enforced=public_network_egress_enforced,
            steel_api_key=_optional(env.get("STEEL_API_KEY")),
            steel_base_url=steel_base_url,
            steel_session=SteelSessionPolicy(
                cloud_features=steel_cloud_features,
                headless=_boolean(env.get("BROWSER_USE_MCP_STEEL_HEADLESS", "false")),
                width=_integer(
                    env,
                    "BROWSER_USE_MCP_STEEL_VIEWPORT_WIDTH",
                    1_440,
                    minimum=800,
                    maximum=7_680,
                ),
                height=_integer(
                    env,
                    "BROWSER_USE_MCP_STEEL_VIEWPORT_HEIGHT",
                    1_000,
                    minimum=600,
                    maximum=4_320,
                ),
                region=steel_region,
                humanize_interactions=humanize_interactions,
                solve_captcha=solve_captcha,
                captcha_timeout_seconds=_floating(
                    env,
                    "BROWSER_USE_MCP_CAPTCHA_TIMEOUT_SECONDS",
                    operation_timeout_seconds,
                    minimum=1.0,
                    maximum=600.0,
                ),
                captcha_poll_interval_seconds=_floating(
                    env,
                    "BROWSER_USE_MCP_CAPTCHA_POLL_INTERVAL_SECONDS",
                    1.0,
                    minimum=0.1,
                    maximum=10.0,
                ),
                network_identity=network_identity,
                proxy_url=steel_proxy_url,
                typing_delay_min_ms=typing_delay_min_ms,
                typing_delay_max_ms=typing_delay_max_ms,
                click_delay_min_ms=click_delay_min_ms,
                click_delay_max_ms=click_delay_max_ms,
            ),
            llm=LLMDefaults(
                base_url=llm_base_url,
                api_key=_optional(env.get("BROWSER_USE_MCP_LLM_API_KEY")),
                model=_optional(env.get("BROWSER_USE_MCP_LLM_MODEL")),
                allowed_origins=llm_allowed_origins,
                allow_insecure_http=allow_insecure_provider_http,
            ),
            max_sessions=_integer(
                env, "BROWSER_USE_MCP_MAX_SESSIONS", 4, minimum=1, maximum=32
            ),
            session_timeout_ms=_integer(
                env,
                "BROWSER_USE_MCP_SESSION_TIMEOUT_MS",
                300_000,
                minimum=30_000,
                maximum=3_600_000,
            ),
            operation_timeout_seconds=operation_timeout_seconds,
            profile_lock_timeout_seconds=_floating(
                env,
                "BROWSER_USE_MCP_PROFILE_LOCK_TIMEOUT_SECONDS",
                5.0,
                minimum=0.1,
                maximum=60.0,
            ),
            max_response_characters=_integer(
                env,
                "BROWSER_USE_MCP_MAX_RESPONSE_CHARACTERS",
                100_000,
                minimum=1_000,
                maximum=1_000_000,
            ),
            max_llm_response_bytes=_integer(
                env,
                "BROWSER_USE_MCP_MAX_LLM_RESPONSE_BYTES",
                2_097_152,
                minimum=65_536,
                maximum=16_777_216,
            ),
        )


def _server_transport(value: str) -> ServerTransport:
    try:
        return ServerTransport(value.strip().lower())
    except ValueError as exc:
        raise ConfigurationError(
            "BROWSER_USE_MCP_TRANSPORT must be http or stdio"
        ) from exc


def _auth_mode(value: str) -> AuthMode:
    try:
        return AuthMode(value.strip().lower())
    except ValueError as exc:
        raise ConfigurationError(
            "BROWSER_USE_MCP_AUTH_MODE must be none or bearer"
        ) from exc


_TENANT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_NETWORK_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _client_credentials(value: str) -> tuple[ClientCredential, ...]:
    credentials: list[ClientCredential] = []
    token_owners: dict[str, str] = {}
    for item in _csv(value):
        tenant_id, separator, token_hash = item.partition("=")
        tenant_id = tenant_id.strip()
        token_hash = token_hash.strip()
        if separator != "=" or _TENANT_ID.fullmatch(tenant_id) is None:
            raise ConfigurationError(
                "client credentials must use tenant-id=sha256-digest entries"
            )
        if (
            len(token_hash) != 64
            or token_hash.lower() != token_hash
            or not _is_hex(token_hash)
        ):
            raise ConfigurationError(
                "client credential digests must be lowercase SHA-256 hex"
            )
        existing_owner = token_owners.get(token_hash)
        if existing_owner is not None:
            raise ConfigurationError(
                "each client credential digest must appear exactly once"
            )
        token_owners[token_hash] = tenant_id
        credentials.append(ClientCredential(tenant_id, token_hash))
    return tuple(credentials)


def _storage_master_key(value: str | None) -> bytes:
    if value is None or not value.strip():
        raise ConfigurationError("BROWSER_USE_MCP_STORAGE_MASTER_KEY is required")
    try:
        key = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ConfigurationError(
            "BROWSER_USE_MCP_STORAGE_MASTER_KEY must be standard Base64"
        ) from exc
    if len(key) != 32:
        raise ConfigurationError(
            "BROWSER_USE_MCP_STORAGE_MASTER_KEY must encode exactly 32 bytes"
        )
    return key


def _boolean(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"invalid boolean value: {value!r}")


def _integer(
    env: dict[str, str] | os._Environ[str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = env.get(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _floating(
    env: dict[str, str] | os._Environ[str],
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = env.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _is_hex(value: str) -> bool:
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


def _is_loopback_bind(host: str) -> bool:
    return host.lower() in {"127.0.0.1", "::1", "localhost"}


def _require_http_url(
    value: str, name: str, *, allow_insecure_http: bool = False
) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError(f"{name} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError(f"{name} must not contain credentials")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ConfigurationError(f"{name} contains an invalid port") from exc
    if parsed.query or parsed.fragment:
        raise ConfigurationError(f"{name} must not contain a query or fragment")
    if (
        parsed.scheme == "http"
        and not _is_loopback_hostname(parsed.hostname)
        and not allow_insecure_http
    ):
        raise ConfigurationError(
            f"{name} must use HTTPS outside loopback; set "
            "BROWSER_USE_MCP_ALLOW_INSECURE_PROVIDER_HTTP=true only for an "
            "isolated trusted network"
        )


def _require_http_origin(value: str, name: str, *, allow_insecure_http: bool) -> str:
    _require_http_url(value, name, allow_insecure_http=allow_insecure_http)
    parsed = urlsplit(value)
    if parsed.path not in {"", "/"}:
        raise ConfigurationError(f"{name} entries must not contain a path")
    return _http_origin(value)


def _http_origin(value: str) -> str:
    parsed = urlsplit(value)
    hostname = parsed.hostname
    if hostname is None:
        raise ConfigurationError("HTTP origin must include a host")
    host = f"[{hostname}]" if ":" in hostname else hostname
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError("HTTP origin contains an invalid port") from exc
    default_port = 443 if parsed.scheme == "https" else 80
    authority = host if port in {None, default_port} else f"{host}:{port}"
    return f"{parsed.scheme}://{authority}"


def _is_loopback_hostname(hostname: str | None) -> bool:
    if hostname is None:
        return False
    return hostname.lower() in {"127.0.0.1", "::1", "localhost"}


def _require_proxy_url(value: str, name: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname:
        raise ConfigurationError(f"{name} must be an absolute HTTP(S) or SOCKS5 URL")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ConfigurationError(f"{name} contains an invalid port") from exc
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ConfigurationError(f"{name} must not contain a path, query, or fragment")


def _require_network_identity(value: str) -> None:
    if _NETWORK_IDENTITY.fullmatch(value) is None:
        raise ConfigurationError(
            "BROWSER_USE_MCP_STEEL_NETWORK_IDENTITY must be 1-64 characters "
            "using letters, numbers, '.', '_' or '-'"
        )


def _require_ordered_range(
    minimum: float,
    maximum: float,
    minimum_name: str,
    maximum_name: str,
) -> None:
    if minimum > maximum:
        raise ConfigurationError(f"{minimum_name} must not exceed {maximum_name}")


def _is_steel_cloud(base_url: str) -> bool:
    parsed = urlsplit(base_url)
    return parsed.scheme == "https" and parsed.hostname == "api.steel.dev"
