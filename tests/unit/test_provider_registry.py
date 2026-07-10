from __future__ import annotations

import pytest

from polaris.providers import (
    Message,
    ProviderCapabilities,
    ProviderConfig,
    ProviderConfigurationError,
    ProviderProfile,
    ProviderRegistry,
    ToolCall,
    Usage,
)


def test_provider_registry_aliases_generation_and_collisions() -> None:
    registry = ProviderRegistry()
    profile = ProviderProfile(
        name="local",
        aliases=("loopback",),
        api_modes=("chat_completions", "responses"),
        capabilities=ProviderCapabilities(tools=True, local=True),
    )
    registry.register(profile)
    assert registry.get("LOOPBACK") is profile
    assert registry.list() == (profile,)
    assert registry.generation == 1
    assert registry.aliases()["loopback"] == "local"

    with pytest.raises(ProviderConfigurationError):
        registry.register(ProviderProfile(name="loopback"))
    with pytest.raises(ProviderConfigurationError):
        registry.get("missing")
    duplicate_alias = ProviderProfile(name="second", aliases=("same", "same"))
    with pytest.raises(ProviderConfigurationError):
        registry.register(duplicate_alias)


def test_strict_contract_values_and_serialization() -> None:
    call = ToolCall("call", "lookup", {"nested": {"ok": True}})
    message = Message("assistant", None, tool_calls=(call,))
    raw_calls = message.to_dict()["tool_calls"]
    assert isinstance(raw_calls, list)
    raw_call = raw_calls[0]
    assert isinstance(raw_call, dict)
    function = raw_call["function"]
    assert isinstance(function, dict)
    assert function["name"] == "lookup"
    assert Usage(1, 2, 3).input_tokens == 1
    assert ProviderConfig("model", "http://localhost").timeout_seconds == 30.0

    with pytest.raises(ValueError):
        Message("invalid", "text")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Message("user", object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Message("assistant", "", tool_calls=[])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Usage(prompt_tokens=-1)
    with pytest.raises(TypeError):
        Usage(total_duration_ns=-1)
    with pytest.raises(TypeError):
        ProviderCapabilities(tools=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ProviderProfile("bad", aliases=["alias"])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ProviderProfile("bad", api_modes=())
