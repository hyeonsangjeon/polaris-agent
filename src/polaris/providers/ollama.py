"""Native Ollama provider using its tags, show, and chat APIs."""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

import httpx

from ._http import endpoint, map_http_status, map_transport_error
from .base import (
    CompletionResult,
    JsonObject,
    JsonValue,
    Message,
    OfflineViolation,
    Provider,
    ProviderConfig,
    ProviderProtocolError,
    ToolCall,
    Usage,
)


def _offline_url_allowed(
    url: str,
    allowed_hosts: tuple[str, ...],
    allow_private_ips: bool,
) -> bool:
    parsed = urlparse(url)
    hostname = parsed.hostname
    if parsed.scheme not in {"http", "https"} or hostname is None:
        return False
    host = hostname.lower().rstrip(".")
    if host in allowed_hosts or host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or (
        allow_private_ips and (address.is_private or address.is_link_local)
    )


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


class OllamaProvider(Provider):
    """Provider for a local Ollama endpoint."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isinstance(config, ProviderConfig):
            raise TypeError("config must be ProviderConfig")
        if config.offline and not _offline_url_allowed(
            config.base_url,
            config.offline_allowed_hosts,
            config.offline_allow_private_ips,
        ):
            raise OfflineViolation("offline policy does not allow this Ollama endpoint")
        if client is not None and transport is not None:
            raise ValueError("pass either client or transport, not both")
        self.config = config
        self._client = client or httpx.AsyncClient(
            transport=transport,
            timeout=config.timeout_seconds,
            trust_env=False,
        )

    async def _request(
        self,
        method: str,
        path: str,
        operation: str,
        body: Mapping[str, JsonValue] | None = None,
    ) -> Mapping[str, Any]:
        try:
            response = await self._client.request(
                method,
                endpoint(self.config.base_url, path),
                headers=dict(self.config.headers),
                json=body,
            )
        except httpx.HTTPError as exc:
            map_transport_error(exc, operation)
        map_http_status(response, operation)
        try:
            data = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderProtocolError(f"{operation} returned invalid JSON") from exc
        if not isinstance(data, Mapping):
            raise ProviderProtocolError(f"{operation} returned invalid JSON object")
        return data

    async def list_models(self) -> tuple[str, ...]:
        data = await self._request("GET", "api/tags", "Ollama model listing")
        raw_models = data.get("models")
        if not isinstance(raw_models, list):
            raise ProviderProtocolError("Ollama tags returned invalid models")
        models: set[str] = set()
        for item in raw_models:
            if isinstance(item, Mapping):
                name = item.get("name") or item.get("model")
                if isinstance(name, str) and name:
                    models.add(name)
        return tuple(sorted(models))

    async def show_model(self) -> Mapping[str, Any]:
        return await self._request(
            "POST",
            "api/show",
            "Ollama model inspection",
            {"model": self.config.model},
        )

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, JsonValue]] | None = None,
        response_schema: Mapping[str, JsonValue] | None = None,
    ) -> CompletionResult:
        if not all(isinstance(message, Message) for message in messages):
            raise TypeError("messages must contain only Message values")
        body: JsonObject = {
            "model": self.config.model,
            "messages": [message.to_dict() for message in messages],
            "stream": False,
        }
        if tools is not None:
            body["tools"] = [
                dict(tool)
                if tool.get("type") == "function"
                else {"type": "function", "function": dict(tool)}
                for tool in tools
            ]
        if response_schema is not None:
            body["format"] = dict(response_schema)
        data = await self._request("POST", "api/chat", "Ollama chat", body)
        raw_message = data.get("message")
        if not isinstance(raw_message, Mapping):
            raise ProviderProtocolError("Ollama chat returned invalid message")
        content = raw_message.get("content")
        if content is not None and not isinstance(content, str):
            raise ProviderProtocolError("Ollama chat returned invalid content")
        calls = self._parse_tool_calls(raw_message.get("tool_calls"))
        model = data.get("model", self.config.model)
        if not isinstance(model, str) or not model:
            raise ProviderProtocolError("Ollama chat returned invalid model")
        prompt = _nonnegative_int(data.get("prompt_eval_count")) or 0
        completion = _nonnegative_int(data.get("eval_count")) or 0
        return CompletionResult(
            message=Message(role="assistant", content=content, tool_calls=calls),
            model=model,
            usage=Usage(
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=prompt + completion,
                total_duration_ns=_nonnegative_int(data.get("total_duration")),
                load_duration_ns=_nonnegative_int(data.get("load_duration")),
                prompt_eval_duration_ns=_nonnegative_int(data.get("prompt_eval_duration")),
                eval_duration_ns=_nonnegative_int(data.get("eval_duration")),
            ),
            finish_reason=data.get("done_reason")
            if isinstance(data.get("done_reason"), str)
            else ("stop" if data.get("done") is True else None),
        )

    @staticmethod
    def _parse_tool_calls(value: object) -> tuple[ToolCall, ...]:
        if value is None:
            return ()
        if not isinstance(value, list):
            raise ProviderProtocolError("Ollama chat returned invalid tool calls")
        calls: list[ToolCall] = []
        for index, item in enumerate(value):
            if not isinstance(item, Mapping):
                raise ProviderProtocolError("Ollama chat returned invalid tool call")
            function = item.get("function")
            if not isinstance(function, Mapping):
                raise ProviderProtocolError("Ollama chat returned invalid tool function")
            name = function.get("name")
            arguments = function.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, Mapping):
                raise ProviderProtocolError("Ollama chat returned incomplete tool call")
            call_id = item.get("id")
            if not isinstance(call_id, str) or not call_id:
                call_id = f"ollama-tool-{index}"
            calls.append(ToolCall(call_id, name, dict(arguments)))
        return tuple(calls)

    async def doctor(self) -> Mapping[str, JsonValue]:
        models = await self.list_models()
        available = self.config.model in models
        details = await self.show_model() if available else {}
        capabilities = details.get("capabilities", [])
        if not isinstance(capabilities, list):
            capabilities = []
        model_info = details.get("model_info", {})
        context_length: int | None = None
        if isinstance(model_info, Mapping):
            candidates = [
                value
                for key, value in model_info.items()
                if isinstance(key, str) and key.endswith(".context_length")
            ]
            context_length = next(
                (
                    value
                    for value in candidates
                    if isinstance(value, int) and not isinstance(value, bool)
                ),
                None,
            )
        return {
            "ok": available,
            "endpoint": self.config.base_url,
            "model": self.config.model,
            "model_available": available,
            "context_length": context_length,
            "tools": "tools" in capabilities,
            "capabilities": [
                capability for capability in capabilities if isinstance(capability, str)
            ],
        }

    async def aclose(self) -> None:
        await self._client.aclose()
