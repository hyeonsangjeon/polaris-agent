from __future__ import annotations

import json
import sys
import types
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from polaris.providers import (
    AzureFoundryProvider,
    EntraIdentityConfig,
    Message,
    OfflineViolation,
    OllamaProvider,
    OpenAICompatibleProvider,
    ProviderAuthenticationError,
    ProviderConfig,
    ProviderConfigurationError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderTransportError,
    ToolCall,
    build_token_provider,
    reset_credential_cache,
)


def config(
    *,
    mode: str = "chat_completions",
    key: str | None = "secret-key",
    base_url: str = "https://models.example/v1",
    offline: bool = False,
    offline_allowed_hosts: tuple[str, ...] = (),
    offline_allow_private_ips: bool = False,
) -> ProviderConfig:
    assert mode in {"chat_completions", "responses"}
    return ProviderConfig(
        model="chosen-model",
        base_url=base_url,
        api_key=key,
        api_mode=mode,  # type: ignore[arg-type]
        offline=offline,
        offline_allowed_hosts=offline_allowed_hosts,
        offline_allow_private_ips=offline_allow_private_ips,
    )


@pytest.mark.asyncio
async def test_ollama_tags_show_chat_tools_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [{"name": "chosen-model"}, {"model": "other"}]},
            )
        if request.url.path == "/api/show":
            return httpx.Response(
                200,
                json={
                    "capabilities": ["completion", "tools"],
                    "model_info": {"family.context_length": 8192},
                },
            )
        assert request.url.path == "/api/chat"
        body = json.loads(request.content)
        assert body["format"] == {"type": "object"}
        assert body["tools"][0]["type"] == "function"
        return httpx.Response(
            200,
            json={
                "model": "actual-model",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "search", "arguments": {"q": "ice"}}}
                    ],
                },
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 7,
                "eval_count": 3,
                "total_duration": 100,
                "load_duration": 10,
                "prompt_eval_duration": 20,
                "eval_duration": 30,
            },
        )

    provider = OllamaProvider(
        config(key=None, base_url="http://127.0.0.1:11434", offline=True),
        transport=httpx.MockTransport(handler),
    )
    assert provider._client.trust_env is False
    assert await provider.list_models() == ("chosen-model", "other")
    doctor = await provider.doctor()
    assert doctor["ok"] is True
    assert doctor["context_length"] == 8192
    result = await provider.complete(
        [Message("user", "hello")],
        [{"name": "search", "parameters": {"type": "object"}}],
        {"type": "object"},
    )
    assert result.model == "actual-model"
    assert result.tool_calls == (ToolCall("ollama-tool-0", "search", {"q": "ice"}),)
    assert result.usage.prompt_tokens == 7
    assert result.usage.total_duration_ns == 100
    await provider.aclose()
    assert len(requests) == 4


def test_ollama_offline_guard() -> None:
    with pytest.raises(OfflineViolation):
        OllamaProvider(config(key=None, base_url="https://remote.example", offline=True))


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "http://[::1]:11434",
    ],
)
def test_ollama_offline_allows_loopback(base_url: str) -> None:
    provider = OllamaProvider(config(key=None, base_url=base_url, offline=True))
    __import__("asyncio").run(provider.aclose())


def test_ollama_offline_allows_configured_private_and_hostname_endpoints() -> None:
    private = OllamaProvider(
        config(
            key=None,
            base_url="http://10.20.30.40:11434",
            offline=True,
            offline_allow_private_ips=True,
        )
    )
    __import__("asyncio").run(private.aclose())

    hostname = OllamaProvider(
        config(
            key=None,
            base_url="http://ollama.internal:11434",
            offline=True,
            offline_allowed_hosts=("ollama.internal",),
        )
    )
    __import__("asyncio").run(hostname.aclose())


def test_ollama_offline_rejects_public_ip_even_when_private_ips_are_allowed() -> None:
    with pytest.raises(OfflineViolation):
        OllamaProvider(
            config(
                key=None,
                base_url="https://8.8.8.8:11434",
                offline=True,
                offline_allow_private_ips=True,
            )
        )


@pytest.mark.asyncio
async def test_ollama_protocol_and_transport_errors() -> None:
    invalid = OllamaProvider(
        config(key=None, base_url="http://localhost:11434"),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[])),
    )
    with pytest.raises(ProviderProtocolError):
        await invalid.list_models()
    await invalid.aclose()

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("not reachable", request=request)

    unreachable = OllamaProvider(
        config(key=None, base_url="http://localhost:11434"),
        transport=httpx.MockTransport(broken),
    )
    with pytest.raises(ProviderTransportError):
        await unreachable.list_models()
    await unreachable.aclose()


@pytest.mark.asyncio
async def test_ollama_http_error_and_missing_model_doctor() -> None:
    denied = OllamaProvider(
        config(key=None, base_url="http://localhost:11434"),
        transport=httpx.MockTransport(lambda request: httpx.Response(429)),
    )
    with pytest.raises(ProviderRateLimitError):
        await denied.list_models()
    await denied.aclose()

    def missing(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": []})

    provider = OllamaProvider(
        config(key=None, base_url="http://localhost:11434"),
        transport=httpx.MockTransport(missing),
    )
    doctor = await provider.doctor()
    assert doctor["ok"] is False
    assert doctor["context_length"] is None
    await provider.aclose()


@pytest.mark.asyncio
async def test_ollama_rejects_malformed_chat_tool_calls() -> None:
    provider = OllamaProvider(
        config(key=None, base_url="http://localhost:11434"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"message": {"content": "", "tool_calls": ["bad"]}},
            )
        ),
    )
    with pytest.raises(ProviderProtocolError):
        await provider.complete([Message("user", "hello")])
    await provider.aclose()


@pytest.mark.asyncio
async def test_openai_chat_request_and_parsing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret-key"
        body = json.loads(request.content)
        assert body["response_format"]["json_schema"]["strict"] is True
        assert body["tools"][0]["function"]["name"] == "lookup"
        return httpx.Response(
            200,
            json={
                "id": "completion-1",
                "model": "actual-chat",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"item":"north"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 4,
                    "total_tokens": 15,
                },
            },
        )

    provider = OpenAICompatibleProvider(config(), transport=httpx.MockTransport(handler))
    result = await provider.complete(
        [Message("user", "where?")],
        [{"name": "lookup", "parameters": {"type": "object"}}],
        {"type": "object", "properties": {"answer": {"type": "string"}}},
    )
    assert result.response_id == "completion-1"
    assert result.model == "actual-chat"
    assert result.tool_calls[0].arguments["item"] == "north"
    assert result.usage.total_tokens == 15
    await provider.aclose()


@pytest.mark.asyncio
async def test_openai_responses_request_and_parsing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert body["text"]["format"]["type"] == "json_schema"
        assert body["tools"][0]["name"] == "lookup"
        return httpx.Response(
            200,
            json={
                "id": "response-1",
                "model": "actual-response",
                "output": [
                    {
                        "type": "message",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": '{"ok":true}'}],
                    },
                    {
                        "type": "function_call",
                        "call_id": "call-2",
                        "name": "lookup",
                        "arguments": '{"x":1}',
                    },
                ],
                "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            },
        )

    provider = OpenAICompatibleProvider(
        config(mode="responses"),
        transport=httpx.MockTransport(handler),
    )
    result = await provider.complete(
        [Message("user", "go")],
        [{"type": "function", "function": {"name": "lookup"}}],
        {"type": "object"},
    )
    assert result.content == '{"ok":true}'
    assert result.tool_calls[0].id == "call-2"
    assert result.usage.input_tokens == 5
    await provider.aclose()


@pytest.mark.asyncio
async def test_openai_responses_tool_continuation_uses_response_items() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "response-1",
                    "model": "actual-response",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call-2",
                            "name": "lookup",
                            "arguments": '{"x":1}',
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "response-2",
                "model": "actual-response",
                "output": [
                    {
                        "type": "message",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ],
            },
        )

    provider = OpenAICompatibleProvider(
        config(mode="responses"),
        transport=httpx.MockTransport(handler),
    )
    first = await provider.complete([Message("user", "find it")])
    await provider.complete(
        [
            Message("user", "find it"),
            first.message,
            Message("tool", '{"value":7}', name="lookup", tool_call_id="call-2"),
        ]
    )

    assert bodies[1] == {
        "model": "chosen-model",
        "input": [
            {"role": "user", "content": "find it"},
            {
                "type": "function_call",
                "call_id": "call-2",
                "name": "lookup",
                "arguments": '{"x":1}',
            },
            {
                "type": "function_call_output",
                "call_id": "call-2",
                "output": '{"value":7}',
            },
        ],
    }
    assert '"tool_calls"' not in json.dumps(bodies[1])
    assert not any(
        item.get("role") == "tool" or "tool_call_id" in item for item in bodies[1]["input"]
    )
    await provider.aclose()


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, ProviderAuthenticationError),
        (403, ProviderAuthenticationError),
        (429, ProviderRateLimitError),
        (500, ProviderProtocolError),
    ],
)
@pytest.mark.asyncio
async def test_openai_http_error_mapping(
    status: int,
    error: type[Exception],
) -> None:
    provider = OpenAICompatibleProvider(
        config(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json={"error": "secret-key"})
        ),
    )
    with pytest.raises(error) as caught:
        await provider.complete([Message("user", "hello")])
    assert "secret-key" not in str(caught.value)
    await provider.aclose()


@pytest.mark.asyncio
async def test_openai_list_models_and_doctor() -> None:
    provider = OpenAICompatibleProvider(
        config(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"data": [{"id": "chosen-model"}, {"id": "z-model"}]},
            )
        ),
    )
    assert await provider.list_models() == ("chosen-model", "z-model")
    assert (await provider.doctor())["model_available"] is True
    await provider.aclose()


@pytest.mark.asyncio
async def test_openai_transport_json_and_shape_errors() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("secret-key", request=request)

    transport_failure = OpenAICompatibleProvider(
        config(),
        transport=httpx.MockTransport(broken),
    )
    with pytest.raises(ProviderTransportError) as caught:
        await transport_failure.list_models()
    assert "secret-key" not in str(caught.value)
    await transport_failure.aclose()

    invalid_json = OpenAICompatibleProvider(
        config(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"not-json")
        ),
    )
    with pytest.raises(ProviderProtocolError):
        await invalid_json.list_models()
    await invalid_json.aclose()

    no_choices = OpenAICompatibleProvider(
        config(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"choices": []})
        ),
    )
    with pytest.raises(ProviderProtocolError):
        await no_choices.complete([Message("user", "hello")])
    await no_choices.aclose()

    invalid_arguments = OpenAICompatibleProvider(
        config(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call",
                                        "function": {
                                            "name": "bad",
                                            "arguments": "not-json",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        ),
    )
    with pytest.raises(ProviderProtocolError):
        await invalid_arguments.complete([Message("user", "hello")])
    await invalid_arguments.aclose()

    invalid_output = OpenAICompatibleProvider(
        config(mode="responses"),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"output": "bad"})
        ),
    )
    with pytest.raises(ProviderProtocolError):
        await invalid_output.complete([Message("user", "hello")])
    await invalid_output.aclose()


@pytest.mark.asyncio
async def test_openai_dynamic_headers_and_validation() -> None:
    async def headers() -> dict[str, str]:
        return {"X-Request-Auth": "fresh"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-request-auth"] == "fresh"
        return httpx.Response(200, json={"data": [{"id": "chosen-model"}]})

    provider = OpenAICompatibleProvider(
        config(key=None),
        header_provider=headers,
        transport=httpx.MockTransport(handler),
    )
    assert await provider.list_models() == ("chosen-model",)
    await provider.aclose()

    bad_headers = OpenAICompatibleProvider(
        config(key=None),
        header_provider=lambda: {"ok": 4},  # type: ignore[dict-item]
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ProviderConfigurationError):
        await bad_headers.list_models()
    await bad_headers.aclose()

    with pytest.raises(ProviderConfigurationError):
        OpenAICompatibleProvider(
            config(),
            client=httpx.AsyncClient(),
            transport=httpx.MockTransport(handler),
        )


@pytest.mark.asyncio
async def test_azure_static_key_and_entra_refresh_without_token_repr() -> None:
    seen: list[tuple[str | None, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.headers.get("api-key"), request.headers.get("authorization")))
        return httpx.Response(
            200,
            json={
                "model": "chosen-model",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            },
        )

    static = AzureFoundryProvider(config(), transport=httpx.MockTransport(handler))
    assert "secret-key" not in repr(static.config)
    await static.complete([Message("user", "hello")])
    await static.aclose()

    tokens = iter(["jwt-one", "jwt-two"])
    entra = AzureFoundryProvider(
        config(key=None),
        token_provider=lambda: next(tokens),
        transport=httpx.MockTransport(handler),
    )
    await entra.complete([Message("user", "one")])
    await entra.complete([Message("user", "two")])
    await entra.aclose()
    assert seen == [
        ("secret-key", None),
        (None, "Bearer jwt-one"),
        (None, "Bearer jwt-two"),
    ]
    assert "jwt-one" not in repr(entra)


@pytest.mark.asyncio
async def test_entra_token_provider_failure_is_sanitized() -> None:
    def fail() -> str:
        raise RuntimeError("jwt-must-not-leak")

    provider = AzureFoundryProvider(
        config(key=None),
        token_provider=fail,
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    with pytest.raises(ProviderAuthenticationError) as caught:
        await provider.complete([Message("user", "hello")])
    assert "jwt-must-not-leak" not in repr(caught.value)
    assert caught.value.__cause__ is None
    await provider.aclose()


@pytest.mark.asyncio
async def test_azure_async_token_and_configuration_errors() -> None:
    async def token() -> str:
        return "async-jwt"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer async-jwt"
        return httpx.Response(200, json={"data": []})

    provider = AzureFoundryProvider(
        config(key=None),
        token_provider=token,
        transport=httpx.MockTransport(handler),
    )
    assert await provider.list_models() == ()
    await provider.aclose()

    with pytest.raises(ProviderConfigurationError):
        AzureFoundryProvider(config(), token_provider=lambda: "jwt")

    empty = AzureFoundryProvider(
        config(key=None),
        token_provider=lambda: "",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ProviderConfigurationError):
        await empty.list_models()
    await empty.aclose()


def test_lazy_azure_identity_with_fake_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    class Credential:
        pass

    def default_credential(**kwargs: bool) -> Credential:
        calls.append(kwargs)
        return Credential()

    def get_provider(credential: Credential, scope: str) -> Callable[[], str]:
        calls.extend([credential, scope])
        return lambda: "ephemeral-jwt"

    azure = types.ModuleType("azure")
    identity = types.ModuleType("azure.identity")
    identity.DefaultAzureCredential = default_credential  # type: ignore[attr-defined]
    identity.get_bearer_token_provider = get_provider  # type: ignore[attr-defined]
    azure.identity = identity  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "azure", azure)
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    reset_credential_cache()
    provider = build_token_provider(
        config=EntraIdentityConfig(scope="https://cognitiveservices.azure.com/.default")
    )
    assert provider() == "ephemeral-jwt"
    assert calls[-1] == "https://cognitiveservices.azure.com/.default"
    reset_credential_cache()


def test_strict_message_contract() -> None:
    with pytest.raises(ValueError):
        Message("tool", "missing id")
    with pytest.raises(TypeError):
        ToolCall("id", "name", {"bad": object()})  # type: ignore[dict-item]
    multipart = Message("user", [{"type": "text", "text": "hello"}])
    assert multipart.to_dict()["content"] == [{"type": "text", "text": "hello"}]


def test_entra_config_parsing_and_strict_provider_config() -> None:
    parsed = EntraIdentityConfig.from_dict(
        {"scope": "", "exclude_interactive_browser": False},
        default_scope="custom/.default",
    )
    assert parsed.to_dict() == {
        "scope": "custom/.default",
        "exclude_interactive_browser": False,
    }
    assert EntraIdentityConfig(scope="").scope == "https://ai.azure.com/.default"
    with pytest.raises(TypeError):
        EntraIdentityConfig.from_dict({"exclude_interactive_browser": "no"})
    with pytest.raises(ValueError):
        ProviderConfig("model", "url", api_mode="bad")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ProviderConfig("model", "url", timeout_seconds=0)
