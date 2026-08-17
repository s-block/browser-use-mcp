from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import httpx
import pytest
from stagehand import LLMMessageGenerateParams

from browser_use_mcp.config import LLMDefaults
from browser_use_mcp.llm import (
    LLMConfig,
    LLMConfigResolver,
    LLMConfigurationError,
    OpenAICompatibleStagehandModel,
    llm_request_scope,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_llm_header_configuration_overrides_environment_defaults() -> None:
    resolver = LLMConfigResolver(
        LLMDefaults(
            base_url="https://default.example/v1",
            api_key="default-key",  # pragma: allowlist secret
            model="default-model",
            allowed_origins=(
                "https://default.example",
                "https://request.example",
            ),
        )
    )

    result = resolver.resolve(
        {
            "x-browser-llm-base-url": "https://request.example/openai/v1/",
            "X-Browser-LLM-API-Key": "request-key",
            "X-Browser-LLM-Model": "request-model",
        }
    )

    assert result == LLMConfig(
        base_url="https://request.example/openai/v1",
        api_key="request-key",  # pragma: allowlist secret
        model="request-model",
    )


def test_llm_configuration_requires_base_url_and_model() -> None:
    with pytest.raises(LLMConfigurationError):
        LLMConfigResolver(LLMDefaults(None, None, None)).resolve(None)


def test_llm_request_origin_must_be_operator_allowed() -> None:
    resolver = LLMConfigResolver(
        LLMDefaults(
            base_url="https://default.example/v1",
            api_key=None,
            model="default-model",
            allowed_origins=("https://default.example",),
        )
    )

    with pytest.raises(LLMConfigurationError, match="origin is not allowed"):
        resolver.resolve({"X-Browser-LLM-Base-URL": "https://attacker.example/v1"})


@pytest.mark.asyncio
async def test_stagehand_model_uses_async_openai_compatible_request(
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "done"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = OpenAICompatibleStagehandModel(
            client,
            allow_private_network=True,
            timeout_seconds=1,
            max_response_bytes=10_000,
        )
        params = LLMMessageGenerateParams.model_validate(
            {"messages": [{"role": "user", "content": {"type": "text", "text": "go"}}]}
        )
        with llm_request_scope(
            LLMConfig("http://llm.internal/v1", "model-a", "llm-secret")
        ):
            result = await model(params)

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "model-a"
    assert captured["authorization"] == "Bearer llm-secret"
    assert result.model_dump()["content"][0]["text"] == "done"
    assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []
