"""Raw-httpx OpenAI-compatible chat and responses provider."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

import httpx

from ._http import endpoint, map_http_status, map_transport_error
from .base import (
    CompletionResult,
    JsonObject,
    JsonValue,
    Message,
    Provider,
    ProviderAuthenticationError,
    ProviderConfig,
    ProviderConfigurationError,
    ProviderError,
    ProviderProtocolError,
    ToolCall,
    Usage,
)

HeaderProvider = Callable[[], Mapping[str, str] | Awaitable[Mapping[str, str]]]


def _object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProviderProtocolError(f"provider returned invalid {label}")
    return value


def _integer(value: object, default: int = 0) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return default


def _parse_arguments(value: object) -> dict[str, JsonValue]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProviderProtocolError("provider returned invalid tool arguments") from exc
    if not isinstance(value, Mapping):
        raise ProviderProtocolError("provider returned non-object tool arguments")
    return dict(value)


def _normalize_chat_tools(
    tools: Sequence[Mapping[str, JsonValue]] | None,
) -> list[Mapping[str, JsonValue]] | None:
    if tools is None:
        return None
    result: list[Mapping[str, JsonValue]] = []
    for tool in tools:
        if tool.get("type") == "function" and isinstance(tool.get("function"), Mapping):
            result.append(tool)
        else:
            result.append({"type": "function", "function": dict(tool)})
    return result


def _normalize_response_tools(
    tools: Sequence[Mapping[str, JsonValue]] | None,
) -> list[Mapping[str, JsonValue]] | None:
    if tools is None:
        return None
    result: list[Mapping[str, JsonValue]] = []
    for tool in tools:
        function = tool.get("function")
        raw = function if tool.get("type") == "function" and isinstance(function, Mapping) else tool
        item: dict[str, JsonValue] = {"type": "function"}
        for key in ("name", "description", "parameters", "strict"):
            value = raw.get(key)
            if value is not None:
                item[key] = value
        if not isinstance(item.get("name"), str):
            raise ProviderConfigurationError("tool definitions require a name")
        result.append(item)
    return result


def _responses_input(messages: Sequence[Message]) -> list[JsonValue]:
    items: list[JsonValue] = []
    for message in messages:
        if message.role == "tool":
            if isinstance(message.content, str):
                output = message.content
            elif message.content is None:
                output = "null"
            else:
                output = json.dumps(
                    [dict(part) for part in message.content],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": output,
                }
            )
            continue
        if message.content is not None:
            content: JsonValue
            if isinstance(message.content, str):
                content = message.content
            else:
                content = [dict(part) for part in message.content]
            items.append({"role": message.role, "content": content})
        for call in message.tool_calls:
            items.append(
                {
                    "type": "function_call",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": json.dumps(
                        dict(call.arguments),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                }
            )
    return items


class OpenAICompatibleProvider(Provider):
    """Provider for APIs implementing OpenAI chat completions or responses."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        header_provider: HeaderProvider | None = None,
    ) -> None:
        if not isinstance(config, ProviderConfig):
            raise TypeError("config must be ProviderConfig")
        if client is not None and transport is not None:
            raise ProviderConfigurationError("pass either client or transport, not both")
        self.config = config
        self._header_provider = header_provider
        self._client = client or httpx.AsyncClient(
            transport=transport,
            timeout=config.timeout_seconds,
        )

    async def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        headers.update(self.config.headers)
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        if self._header_provider is not None:
            supplied = self._header_provider()
            if inspect.isawaitable(supplied):
                supplied = await supplied
            if not isinstance(supplied, Mapping) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in supplied.items()
            ):
                raise ProviderConfigurationError("header provider returned invalid headers")
            headers.update(supplied)
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json_body: Mapping[str, JsonValue] | None = None,
    ) -> Mapping[str, Any]:
        try:
            headers = await self._headers()
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderAuthenticationError(
                f"request authentication failed: {type(exc).__name__}"
            ) from None
        try:
            response = await self._client.request(
                method,
                endpoint(self.config.base_url, path),
                headers=headers,
                json=json_body,
            )
        except httpx.HTTPError as exc:
            map_transport_error(exc, operation)
        map_http_status(response, operation)
        try:
            data = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderProtocolError(f"{operation} returned invalid JSON") from exc
        return _object(data, "JSON object")

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, JsonValue]] | None = None,
        response_schema: Mapping[str, JsonValue] | None = None,
    ) -> CompletionResult:
        if not all(isinstance(message, Message) for message in messages):
            raise TypeError("messages must contain only Message values")
        if response_schema is not None and not isinstance(response_schema, Mapping):
            raise TypeError("response_schema must be a mapping or None")
        if self.config.api_mode == "responses":
            return await self._complete_responses(messages, tools, response_schema)
        return await self._complete_chat(messages, tools, response_schema)

    async def _complete_chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, JsonValue]] | None,
        response_schema: Mapping[str, JsonValue] | None,
    ) -> CompletionResult:
        body: JsonObject = {
            "model": self.config.model,
            "messages": [message.to_dict() for message in messages],
        }
        normalized_tools = _normalize_chat_tools(tools)
        if normalized_tools is not None:
            body["tools"] = [dict(tool) for tool in normalized_tools]
        if response_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "strict": True,
                    "schema": dict(response_schema),
                },
            }
        data = await self._request(
            "POST", "chat/completions", operation="chat completion", json_body=body
        )
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderProtocolError("chat completion returned no choices")
        choice = _object(choices[0], "chat choice")
        raw_message = _object(choice.get("message"), "chat message")
        content = raw_message.get("content")
        if content is not None and not isinstance(content, str):
            raise ProviderProtocolError("chat completion returned invalid content")
        calls = self._parse_chat_tool_calls(raw_message.get("tool_calls"))
        model = data.get("model", self.config.model)
        if not isinstance(model, str) or not model:
            raise ProviderProtocolError("chat completion returned invalid model")
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            finish_reason = None
        return CompletionResult(
            message=Message(role="assistant", content=content, tool_calls=calls),
            model=model,
            usage=self._parse_usage(data.get("usage")),
            finish_reason=finish_reason,
            response_id=data.get("id") if isinstance(data.get("id"), str) else None,
        )

    @staticmethod
    def _parse_chat_tool_calls(value: object) -> tuple[ToolCall, ...]:
        if value is None:
            return ()
        if not isinstance(value, list):
            raise ProviderProtocolError("chat completion returned invalid tool calls")
        calls: list[ToolCall] = []
        for item in value:
            raw = _object(item, "tool call")
            function = _object(raw.get("function"), "tool function")
            call_id = raw.get("id")
            name = function.get("name")
            if not isinstance(call_id, str) or not isinstance(name, str):
                raise ProviderProtocolError("chat completion returned incomplete tool call")
            calls.append(ToolCall(call_id, name, _parse_arguments(function.get("arguments"))))
        return tuple(calls)

    async def _complete_responses(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, JsonValue]] | None,
        response_schema: Mapping[str, JsonValue] | None,
    ) -> CompletionResult:
        body: JsonObject = {
            "model": self.config.model,
            "input": _responses_input(messages),
        }
        normalized_tools = _normalize_response_tools(tools)
        if normalized_tools is not None:
            response_tools: list[JsonValue] = [dict(tool) for tool in normalized_tools]
            body["tools"] = response_tools
        if response_schema is not None:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "response",
                    "strict": True,
                    "schema": dict(response_schema),
                }
            }
        data = await self._request("POST", "responses", operation="response", json_body=body)
        output = data.get("output")
        if not isinstance(output, list):
            raise ProviderProtocolError("response returned invalid output")
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        finish_reason: str | None = None
        for raw_item in output:
            item = _object(raw_item, "response output item")
            item_type = item.get("type")
            if item_type == "function_call":
                call_id = item.get("call_id") or item.get("id")
                name = item.get("name")
                if not isinstance(call_id, str) or not isinstance(name, str):
                    raise ProviderProtocolError("response returned incomplete function call")
                calls.append(ToolCall(call_id, name, _parse_arguments(item.get("arguments"))))
            elif item_type == "message":
                status = item.get("status")
                if isinstance(status, str):
                    finish_reason = status
                content = item.get("content")
                if not isinstance(content, list):
                    raise ProviderProtocolError("response returned invalid message content")
                for raw_part in content:
                    part = _object(raw_part, "response content part")
                    if part.get("type") in {"output_text", "text"}:
                        text = part.get("text")
                        if not isinstance(text, str):
                            raise ProviderProtocolError("response returned invalid output text")
                        text_parts.append(text)
        model = data.get("model", self.config.model)
        if not isinstance(model, str) or not model:
            raise ProviderProtocolError("response returned invalid model")
        return CompletionResult(
            message=Message(
                role="assistant",
                content="".join(text_parts) if text_parts else None,
                tool_calls=tuple(calls),
            ),
            model=model,
            usage=self._parse_usage(data.get("usage"), responses=True),
            finish_reason=finish_reason,
            response_id=data.get("id") if isinstance(data.get("id"), str) else None,
        )

    @staticmethod
    def _parse_usage(value: object, *, responses: bool = False) -> Usage:
        if not isinstance(value, Mapping):
            return Usage()
        prompt_key = "input_tokens" if responses else "prompt_tokens"
        completion_key = "output_tokens" if responses else "completion_tokens"
        prompt = _integer(value.get(prompt_key))
        completion = _integer(value.get(completion_key))
        return Usage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=_integer(value.get("total_tokens"), prompt + completion),
        )

    async def list_models(self) -> tuple[str, ...]:
        data = await self._request("GET", "models", operation="model listing")
        items = data.get("data")
        if not isinstance(items, list):
            raise ProviderProtocolError("model listing returned invalid data")
        models = {
            item["id"]
            for item in items
            if isinstance(item, Mapping) and isinstance(item.get("id"), str) and item["id"]
        }
        return tuple(sorted(models))

    async def doctor(self) -> Mapping[str, JsonValue]:
        models = await self.list_models()
        return {
            "ok": True,
            "endpoint": self.config.base_url,
            "model": self.config.model,
            "model_available": self.config.model in models,
            "api_mode": self.config.api_mode,
        }

    async def aclose(self) -> None:
        await self._client.aclose()
