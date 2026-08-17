from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from browser_use_mcp.config import ConfigurationError, ServerTransport
from tests.conftest import make_settings

if TYPE_CHECKING:
    from pathlib import Path


def test_http_transport_is_default(tmp_path: Path) -> None:
    assert make_settings(tmp_path).transport is ServerTransport.HTTP


def test_stdio_transport_is_configurable(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        env_overrides={
            "BROWSER_USE_MCP_TRANSPORT": "stdio",
            "BROWSER_USE_MCP_HOST": "192.0.2.1",
        },
    )

    assert settings.transport is ServerTransport.STDIO


def test_invalid_transport_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="TRANSPORT must be http or stdio"):
        make_settings(
            tmp_path,
            env_overrides={"BROWSER_USE_MCP_TRANSPORT": "websocket"},
        )


def test_stdio_transport_rejects_http_bearer_authentication(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match=r"authentication requires.*http"):
        make_settings(
            tmp_path,
            bearer_tokens=("client-a-very-long-random-secret-value-1234567890",),
            env_overrides={"BROWSER_USE_MCP_TRANSPORT": "stdio"},
        )


def test_steel_cloud_session_policy_has_headful_desktop_defaults(
    tmp_path: Path,
) -> None:
    policy = make_settings(tmp_path).steel_session

    assert policy.cloud_features is True
    assert policy.headless is False
    assert (policy.width, policy.height) == (1_440, 1_000)
    assert policy.region is None
    assert policy.humanize_interactions is True
    assert policy.solve_captcha is False
    assert policy.network_identity == "direct-auto"


def test_self_hosted_steel_omits_cloud_only_features(tmp_path: Path) -> None:
    policy = make_settings(
        tmp_path,
        env_overrides={
            "STEEL_BASE_URL": "http://steel.internal:3000",
            "BROWSER_USE_MCP_ALLOW_INSECURE_PROVIDER_HTTP": "true",
        },
    ).steel_session

    assert policy.cloud_features is False
    assert policy.region is None
    assert policy.humanize_interactions is False
    assert policy.solve_captcha is False
    assert policy.network_identity == "direct-auto"


def test_explicit_cloud_region_is_reused_as_direct_network_identity(
    tmp_path: Path,
) -> None:
    policy = make_settings(
        tmp_path,
        env_overrides={"BROWSER_USE_MCP_STEEL_REGION": "configured-region"},
    ).steel_session

    assert policy.region == "configured-region"
    assert policy.network_identity == "direct-configured-region"


def test_proxy_requires_named_network_identity(tmp_path: Path) -> None:
    with pytest.raises(
        ConfigurationError,
        match="STEEL_NETWORK_IDENTITY is required",
    ):
        make_settings(
            tmp_path,
            env_overrides={
                "BROWSER_USE_MCP_STEEL_PROXY_URL": ("https://proxy.example:8443")
            },
        )


def test_proxy_secret_is_not_rendered_in_settings(tmp_path: Path) -> None:
    proxy_url = "https://proxy.example:8443"
    settings = make_settings(
        tmp_path,
        env_overrides={
            "BROWSER_USE_MCP_STEEL_PROXY_URL": proxy_url,
            "BROWSER_USE_MCP_STEEL_NETWORK_IDENTITY": "dedicated-proxy-1",
        },
    )

    assert settings.steel_session.proxy_url == proxy_url
    assert proxy_url not in repr(settings)
    assert "proxy.example" not in repr(settings.steel_session)


def test_self_hosted_steel_rejects_cloud_only_features(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match=r"require.*CLOUD_FEATURES=true"):
        make_settings(
            tmp_path,
            env_overrides={
                "STEEL_BASE_URL": "http://steel.internal:3000",
                "BROWSER_USE_MCP_ALLOW_INSECURE_PROVIDER_HTTP": "true",
                "BROWSER_USE_MCP_STEEL_SOLVE_CAPTCHA": "true",
            },
        )


def test_interaction_delay_range_is_ordered(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="MIN_MS must not exceed"):
        make_settings(
            tmp_path,
            env_overrides={
                "BROWSER_USE_MCP_TYPING_DELAY_MIN_MS": "200",
                "BROWSER_USE_MCP_TYPING_DELAY_MAX_MS": "100",
            },
        )


def test_non_loopback_http_requires_declared_tls_termination(tmp_path: Path) -> None:
    token = "client-a-very-long-random-secret-value-1234567890"
    with pytest.raises(ConfigurationError, match="requires TLS termination"):
        make_settings(
            tmp_path,
            bearer_tokens=(token,),
            env_overrides={"BROWSER_USE_MCP_HOST": "0.0.0.0"},  # noqa: S104
        )

    settings = make_settings(
        tmp_path,
        bearer_tokens=(token,),
        env_overrides={
            "BROWSER_USE_MCP_HOST": "0.0.0.0",  # noqa: S104
            "BROWSER_USE_MCP_TLS_TERMINATED": "true",
        },
    )

    assert settings.tls_terminated is True


def test_non_loopback_provider_http_requires_explicit_override(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="must use HTTPS"):
        make_settings(
            tmp_path,
            env_overrides={"STEEL_BASE_URL": "http://steel.internal:3000"},
        )


def test_private_network_blocking_requires_authoritative_egress(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError, match="authoritative Steel egress"):
        make_settings(
            tmp_path,
            env_overrides={"BROWSER_USE_MCP_ALLOW_PRIVATE_NETWORK": "false"},
        )

    settings = make_settings(
        tmp_path,
        env_overrides={
            "BROWSER_USE_MCP_ALLOW_PRIVATE_NETWORK": "false",
            "BROWSER_USE_MCP_PUBLIC_NETWORK_EGRESS_ENFORCED": "true",
        },
    )

    assert settings.allow_private_network is False
    assert settings.public_network_egress_enforced is True


def test_credential_values_are_not_rendered_in_settings(tmp_path: Path) -> None:
    steel_key = "steel-sensitive-sentinel"  # pragma: allowlist secret
    llm_key = "llm-sensitive-sentinel"  # pragma: allowlist secret
    settings = make_settings(
        tmp_path,
        env_overrides={
            "STEEL_API_KEY": steel_key,
            "BROWSER_USE_MCP_LLM_BASE_URL": "https://llm.example/v1",
            "BROWSER_USE_MCP_LLM_API_KEY": llm_key,
            "BROWSER_USE_MCP_LLM_MODEL": "example-model",
        },
    )

    rendered = repr(settings)
    assert steel_key not in rendered
    assert llm_key not in rendered
    assert llm_key not in repr(settings.llm)
