from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import SplitResult, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from stagehand import (
    LLMGenerateInput,
    LLMGenerateOutput,
    LLMMessageGenerateParams,
    LLMMessageGenerateResult,
    LLMStructuredGenerateParams,
    LLMStructuredGenerateResult,
)

from browser_use_mcp.audit import audit_event
from browser_use_mcp.network import UnsafeURLError, validate_outbound_url

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from browser_use_mcp.config import LLMDefaults

LLM_BASE_URL_HEADER = "X-Browser-LLM-Base-URL"
LLM_API_KEY_HEADER = "X-Browser-LLM-API-Key"  # pragma: allowlist secret
LLM_MODEL_HEADER = "X-Browser-LLM-Model"


class LLMConfigurationError(RuntimeError):
    pass


class LLMProviderError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LLMConfig:
    base_url: str
    model: str
    api_key: str | None = field(default=None, repr=False)


class LLMConfigResolver:
    def __init__(self, defaults: LLMDefaults) -> None:
        self._defaults = defaults

    def resolve(self, headers: Mapping[str, str] | None) -> LLMConfig:
        base_url = _header(headers, LLM_BASE_URL_HEADER) or self._defaults.base_url
        api_key = _header(headers, LLM_API_KEY_HEADER) or self._defaults.api_key
        model = _header(headers, LLM_MODEL_HEADER) or self._defaults.model
        if base_url is None or model is None:
            raise LLMConfigurationError(
                "semantic browser tools require an LLM base URL and model"
            )
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or len(base_url) > 2_048
        ):
            raise LLMConfigurationError("LLM base URL is invalid")
        if (
            parsed.scheme == "http"
            and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            and not self._defaults.allow_insecure_http
        ):
            raise LLMConfigurationError("LLM base URL must use HTTPS")
        if _origin(parsed) not in self._defaults.allowed_origins:
            raise LLMConfigurationError("LLM base URL origin is not allowed")
        if len(model) > 256:
            raise LLMConfigurationError("LLM model identifier is too long")
        if api_key is not None and len(api_key) > 8_192:
            raise LLMConfigurationError("LLM API key is too long")
        return LLMConfig(base_url=base_url.rstrip("/"), api_key=api_key, model=model)


_current_config: ContextVar[LLMConfig | None] = ContextVar(
    "browser_use_mcp_llm_config", default=None
)


@contextmanager
def llm_request_scope(config: LLMConfig) -> Iterator[None]:
    token = _current_config.set(config)
    try:
        yield
    finally:
        _current_config.reset(token)


class OpenAICompatibleStagehandModel:
    """Stagehand model callback backed by async OpenAI Chat Completions."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        allow_private_network: bool,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> None:
        self._client = client
        self._allow_private_network = allow_private_network
        self._timeout = timeout_seconds
        self._max_response_bytes = max_response_bytes

    async def __call__(self, params: LLMGenerateInput) -> LLMGenerateOutput:
        config = _current_config.get()
        if config is None:
            raise LLMConfigurationError(
                "LLM configuration is only available during an MCP tool request"
            )
        endpoint = f"{config.base_url}/chat/completions"
        try:
            await validate_outbound_url(
                endpoint, allow_private_network=self._allow_private_network
            )
        except UnsafeURLError:
            audit_event(
                "llm_egress",
                outcome="blocked",
                reason="network_policy",
            )
            raise LLMProviderError("LLM provider destination is blocked") from None
        payload = _openai_payload(params, config.model)
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if config.api_key is not None:
            headers["Authorization"] = f"Bearer {config.api_key}"

        try:
            audit_event("llm_egress", outcome="attempted")
            raw = await self._request(endpoint, headers, payload)
            response = _ChatResponse.model_validate_json(raw)
            result = _stagehand_result(params, response)
            audit_event("llm_egress", outcome="succeeded")
            return result
        except LLMProviderError:
            audit_event(
                "llm_egress",
                outcome="failed",
                reason="provider_error",
            )
            raise
        except (httpx.HTTPError, ValidationError, ValueError, TypeError) as exc:
            audit_event(
                "llm_egress",
                outcome="failed",
                reason="invalid_response",
            )
            raise LLMProviderError("LLM provider returned an invalid response") from exc

    async def _request(
        self,
        endpoint: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> bytes:
        try:
            async with self._client.stream(
                "POST",
                endpoint,
                headers=headers,
                json=payload,
                timeout=self._timeout,
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise LLMProviderError(
                        "LLM provider request failed with status "
                        f"{response.status_code}"
                    )
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self._max_response_bytes:
                        raise LLMProviderError(
                            "LLM provider response exceeded the limit"
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError as exc:
            raise LLMProviderError("LLM provider request failed") from exc


class _ToolFunction(BaseModel):
    name: str
    arguments: str


class _ToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: _ToolFunction


class _AssistantMessage(BaseModel):
    content: str | None = None
    tool_calls: list[_ToolCall] = Field(default_factory=list)


class _Choice(BaseModel):
    message: _AssistantMessage
    finish_reason: str | None = None


class _Usage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class _ChatResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    choices: list[_Choice] = Field(min_length=1)
    usage: _Usage | None = None


def _openai_payload(params: LLMGenerateInput, model: str) -> dict[str, Any]:
    raw = params.model_dump(mode="json", by_alias=True, exclude_none=True)
    payload: dict[str, Any] = {
        "model": model,
        "messages": _openai_messages(raw),
    }
    if params.system_prompt:
        payload["messages"].insert(
            0, {"role": "system", "content": params.system_prompt}
        )
    if params.temperature is not None:
        payload["temperature"] = params.temperature
    if params.stop_sequences:
        payload["stop"] = params.stop_sequences

    if isinstance(params, LLMMessageGenerateParams):
        if params.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": tool.input_schema,
                    },
                }
                for tool in params.tools
            ]
        if params.tool_choice is not None and params.tool_choice.mode is not None:
            payload["tool_choice"] = params.tool_choice.mode
    else:
        response_format = raw["response_format"]
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": response_format["name"],
                "description": response_format.get("description", ""),
                "schema": response_format["schema"],
                "strict": True,
            },
        }
    return payload


def _openai_messages(raw: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for message in cast("list[dict[str, Any]]", raw["messages"]):
        role = cast("str", message["role"])
        content = message["content"]
        blocks = content if isinstance(content, list) else [content]
        normal_blocks = [block for block in blocks if block["type"] != "tool_result"]
        tool_results = [block for block in blocks if block["type"] == "tool_result"]
        if normal_blocks:
            converted = _convert_content(normal_blocks)
            item: dict[str, Any] = {"role": role, "content": converted}
            tool_calls = [
                block for block in normal_blocks if block["type"] == "tool_use"
            ]
            if tool_calls:
                item["tool_calls"] = [
                    {
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(
                                block["input"], separators=(",", ":")
                            ),
                        },
                    }
                    for block in tool_calls
                ]
            output.append(item)
        for result in tool_results:
            result_content = _convert_content(result.get("content", []))
            if result.get("structured_content") is not None:
                result_content = json.dumps(
                    result["structured_content"], separators=(",", ":")
                )
            output.append(
                {
                    "role": "tool",
                    "tool_call_id": result["tool_use_id"],
                    "content": result_content,
                }
            )
    return output


def _convert_content(blocks: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for block in blocks:
        if block["type"] == "text":
            converted.append({"type": "text", "text": block["text"]})
        elif block["type"] == "image":
            converted.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{block['mime_type']};base64,{block['data']}"
                    },
                }
            )
    if not converted:
        return ""
    if len(converted) == 1 and converted[0]["type"] == "text":
        return cast("str", converted[0]["text"])
    return converted


def _stagehand_result(
    params: LLMGenerateInput, response: _ChatResponse
) -> LLMGenerateOutput:
    choice = response.choices[0]
    content: list[dict[str, Any]] = []
    if choice.message.content:
        content.append({"type": "text", "text": choice.message.content})
    for tool_call in choice.message.tool_calls:
        arguments = json.loads(tool_call.function.arguments)
        if not isinstance(arguments, dict):
            raise LLMProviderError("LLM tool arguments must be a JSON object")
        content.append(
            {
                "type": "tool_use",
                "id": tool_call.id,
                "name": tool_call.function.name,
                "input": arguments,
            }
        )
    if not content:
        content.append({"type": "text", "text": ""})
    usage = response.usage or _Usage()
    usage_data = {
        "input_tokens": usage.prompt_tokens,
        "output_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }
    if isinstance(params, LLMStructuredGenerateParams):
        if choice.message.content is None:
            raise LLMProviderError("LLM provider omitted structured content")
        structured = json.loads(choice.message.content)
        if not isinstance(structured, dict):
            raise LLMProviderError("LLM structured response must be a JSON object")
        return LLMStructuredGenerateResult.model_validate(
            {
                "role": "assistant",
                "content": content,
                "stop_reason": choice.finish_reason,
                "usage": usage_data,
                "output_format": "json_schema",
                "structured_content": structured,
            }
        )
    return LLMMessageGenerateResult.model_validate(
        {
            "role": "assistant",
            "content": content,
            "stop_reason": choice.finish_reason,
            "usage": usage_data,
            "output_format": "text",
        }
    )


def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    if headers is None:
        return None
    value = next(
        (value for key, value in headers.items() if key.lower() == name.lower()), None
    )
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _origin(parsed: SplitResult) -> str:
    hostname = parsed.hostname
    if hostname is None:
        raise LLMConfigurationError("LLM base URL must include a host")
    host = f"[{hostname}]" if ":" in hostname else hostname
    try:
        port = parsed.port
    except ValueError as exc:
        raise LLMConfigurationError("LLM base URL contains an invalid port") from exc
    default_port = 443 if parsed.scheme == "https" else 80
    authority = host if port in {None, default_port} else f"{host}:{port}"
    return f"{parsed.scheme}://{authority}"
