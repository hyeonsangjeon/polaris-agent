# Adapted from providers/base.py at b9b463f3bd6517b76687d9b3c9dea1e62f01f9e1.
# Copyright (c) Nous Research. MIT licensed; see THIRD_PARTY_NOTICES.md.
"""Strict provider contracts and the provider profile registry."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, TypeAlias, cast

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
MessageRole: TypeAlias = Literal["system", "developer", "user", "assistant", "tool"]
ApiMode: TypeAlias = Literal["chat_completions", "responses"]


class ProviderError(RuntimeError):
    """Base class for provider failures."""


class ProviderConfigurationError(ProviderError):
    """The provider configuration is invalid or incomplete."""


class ProviderAuthenticationError(ProviderError):
    """The provider rejected its credentials."""


class ProviderRateLimitError(ProviderError):
    """The provider rejected a request due to a rate limit."""


class ProviderTransportError(ProviderError):
    """The provider endpoint could not be reached."""


class ProviderProtocolError(ProviderError):
    """The provider returned an invalid or unsuccessful response."""


class ProviderCapabilityError(ProviderError):
    """The requested feature is unsupported by the provider or model."""


class OfflineViolation(ProviderConfigurationError):
    """Offline mode was configured with a non-local endpoint."""


def _require_str(value: object, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise TypeError(f"{field_name} must be a non-empty string")
    return value


def _require_optional_str(value: object, field_name: str) -> None:
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string or None")


def _json_copy(value: object, field_name: str = "value") -> JsonValue:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, list):
        return [_json_copy(item, field_name) for item in value]
    if isinstance(value, Mapping):
        result: JsonObject = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{field_name} object keys must be strings")
            result[key] = _json_copy(item, field_name)
        return result
    raise TypeError(f"{field_name} must contain only JSON values")


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A normalized model-requested tool invocation."""

    id: str
    name: str
    arguments: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_str(self.id, "ToolCall.id")
        _require_str(self.name, "ToolCall.name")
        copied = _json_copy(self.arguments, "ToolCall.arguments")
        if not isinstance(copied, dict):
            raise TypeError("ToolCall.arguments must be a mapping")
        object.__setattr__(self, "arguments", MappingProxyType(copied))

    def to_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": dict(self.arguments),
            },
        }


@dataclass(frozen=True, slots=True)
class Message:
    """A normalized provider message."""

    role: MessageRole
    content: str | Sequence[Mapping[str, JsonValue]] | None
    name: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if self.role not in {"system", "developer", "user", "assistant", "tool"}:
            raise ValueError(f"invalid message role: {self.role!r}")
        _require_optional_str(self.name, "Message.name")
        _require_optional_str(self.tool_call_id, "Message.tool_call_id")
        if self.content is not None and not isinstance(self.content, (str, list, tuple)):
            raise TypeError("Message.content must be text, a sequence of content parts, or None")
        if isinstance(self.content, (list, tuple)):
            parts: list[Mapping[str, JsonValue]] = []
            for part in self.content:
                copied = _json_copy(part, "Message.content")
                if not isinstance(copied, dict):
                    raise TypeError("Message content parts must be mappings")
                parts.append(MappingProxyType(copied))
            object.__setattr__(self, "content", tuple(parts))
        if not isinstance(self.tool_calls, tuple) or not all(
            isinstance(call, ToolCall) for call in self.tool_calls
        ):
            raise TypeError("Message.tool_calls must be a tuple of ToolCall values")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool messages require tool_call_id")

    def to_dict(self) -> JsonObject:
        content: JsonValue
        if isinstance(self.content, tuple):
            content = [dict(part) for part in self.content]
        else:
            content = cast(JsonValue, self.content)
        result: JsonObject = {"role": self.role, "content": content}
        if self.name is not None:
            result["name"] = self.name
        if self.tool_calls:
            result["tool_calls"] = [call.to_dict() for call in self.tool_calls]
        if self.tool_call_id is not None:
            result["tool_call_id"] = self.tool_call_id
        return result


@dataclass(frozen=True, slots=True)
class Usage:
    """Normalized token and timing accounting."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    total_duration_ns: int | None = None
    load_duration_ns: int | None = None
    prompt_eval_duration_ns: int | None = None
    eval_duration_ns: int | None = None

    def __post_init__(self) -> None:
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise TypeError(f"Usage.{name} must be a non-negative integer")
        for name in (
            "total_duration_ns",
            "load_duration_ns",
            "prompt_eval_duration_ns",
            "eval_duration_ns",
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise TypeError(f"Usage.{name} must be a non-negative integer or None")

    @property
    def input_tokens(self) -> int:
        return self.prompt_tokens

    @property
    def output_tokens(self) -> int:
        return self.completion_tokens


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """A successful completion normalized across provider APIs."""

    message: Message
    model: str
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None
    response_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.message, Message):
            raise TypeError("CompletionResult.message must be a Message")
        _require_str(self.model, "CompletionResult.model")
        if not isinstance(self.usage, Usage):
            raise TypeError("CompletionResult.usage must be Usage")
        _require_optional_str(self.finish_reason, "CompletionResult.finish_reason")
        _require_optional_str(self.response_id, "CompletionResult.response_id")

    @property
    def content(self) -> str | Sequence[Mapping[str, JsonValue]] | None:
        return self.message.content

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        return self.message.tool_calls


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Transport-neutral provider configuration."""

    model: str
    base_url: str
    api_key: str | None = field(default=None, repr=False)
    api_mode: ApiMode = "chat_completions"
    timeout_seconds: float = 30.0
    offline: bool = False
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    offline_allowed_hosts: tuple[str, ...] = ()
    offline_allow_private_ips: bool = False

    def __post_init__(self) -> None:
        _require_str(self.model, "ProviderConfig.model")
        _require_str(self.base_url, "ProviderConfig.base_url")
        _require_optional_str(self.api_key, "ProviderConfig.api_key")
        if self.api_mode not in {"chat_completions", "responses"}:
            raise ValueError(f"invalid api_mode: {self.api_mode!r}")
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
        ):
            raise TypeError("ProviderConfig.timeout_seconds must be a positive number")
        if not isinstance(self.offline, bool):
            raise TypeError("ProviderConfig.offline must be a bool")
        if not isinstance(self.offline_allowed_hosts, tuple) or not all(
            isinstance(host, str) and host.strip() for host in self.offline_allowed_hosts
        ):
            raise TypeError(
                "ProviderConfig.offline_allowed_hosts must be a tuple of non-empty strings"
            )
        if not isinstance(self.offline_allow_private_ips, bool):
            raise TypeError("ProviderConfig.offline_allow_private_ips must be a bool")
        object.__setattr__(
            self,
            "offline_allowed_hosts",
            tuple(host.strip().lower().rstrip(".") for host in self.offline_allowed_hosts),
        )
        headers: dict[str, str] = {}
        if not isinstance(self.headers, Mapping):
            raise TypeError("ProviderConfig.headers must be a mapping")
        for key, value in self.headers.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError("ProviderConfig.headers must contain string keys and values")
            headers[key] = value
        object.__setattr__(self, "headers", MappingProxyType(headers))
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Features that a provider profile can advertise declaratively."""

    tools: bool = False
    structured_output: bool = False
    vision: bool = False
    model_listing: bool = True
    local: bool = False

    def __post_init__(self) -> None:
        for name in ("tools", "structured_output", "vision", "model_listing", "local"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"ProviderCapabilities.{name} must be a bool")


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """Declarative provider metadata; it does not own clients or credentials."""

    name: str
    aliases: tuple[str, ...] = ()
    display_name: str = ""
    default_base_url: str = ""
    api_modes: tuple[ApiMode, ...] = ("chat_completions",)
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)

    def __post_init__(self) -> None:
        _require_str(self.name, "ProviderProfile.name")
        if not isinstance(self.aliases, tuple) or not all(
            isinstance(alias, str) and alias.strip() for alias in self.aliases
        ):
            raise TypeError("ProviderProfile.aliases must be a tuple of non-empty strings")
        if not isinstance(self.display_name, str) or not isinstance(self.default_base_url, str):
            raise TypeError("profile display_name and default_base_url must be strings")
        if (
            not isinstance(self.api_modes, tuple)
            or not self.api_modes
            or any(mode not in {"chat_completions", "responses"} for mode in self.api_modes)
        ):
            raise TypeError("ProviderProfile.api_modes must contain supported API modes")
        if not isinstance(self.capabilities, ProviderCapabilities):
            raise TypeError("ProviderProfile.capabilities must be ProviderCapabilities")


class ProviderRegistry:
    """Thread-safe registry for declarative profiles and aliases."""

    def __init__(self) -> None:
        self._profiles: dict[str, ProviderProfile] = {}
        self._aliases: dict[str, str] = {}
        self._lock = threading.RLock()
        self._generation = 0

    @staticmethod
    def _key(name: str) -> str:
        return _require_str(name, "provider name").strip().lower()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def register(self, profile: ProviderProfile) -> None:
        if not isinstance(profile, ProviderProfile):
            raise TypeError("profile must be a ProviderProfile")
        canonical = self._key(profile.name)
        aliases = tuple(self._key(alias) for alias in profile.aliases)
        if canonical in aliases or len(set(aliases)) != len(aliases):
            raise ProviderConfigurationError(
                f"duplicate alias in provider profile {profile.name!r}"
            )
        with self._lock:
            occupied = set(self._profiles) | set(self._aliases)
            collisions = [key for key in (canonical, *aliases) if key in occupied]
            if collisions:
                raise ProviderConfigurationError(
                    f"provider name or alias already registered: {collisions[0]!r}"
                )
            self._profiles[canonical] = profile
            self._aliases.update({alias: canonical for alias in aliases})
            self._generation += 1

    def get(self, name: str) -> ProviderProfile:
        key = self._key(name)
        with self._lock:
            canonical = self._aliases.get(key, key)
            try:
                return self._profiles[canonical]
            except KeyError as exc:
                raise ProviderConfigurationError(f"unknown provider: {name!r}") from exc

    def list(self) -> tuple[ProviderProfile, ...]:
        with self._lock:
            return tuple(self._profiles[key] for key in sorted(self._profiles))

    def aliases(self) -> Mapping[str, str]:
        with self._lock:
            return MappingProxyType(dict(self._aliases))


class Provider(ABC):
    """Asynchronous model-provider interface."""

    config: ProviderConfig

    @abstractmethod
    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, JsonValue]] | None = None,
        response_schema: Mapping[str, JsonValue] | None = None,
    ) -> CompletionResult:
        """Complete a conversation or raise a typed provider error."""

    @abstractmethod
    async def list_models(self) -> tuple[str, ...]:
        """Return model identifiers exposed by the endpoint."""

    @abstractmethod
    async def doctor(self) -> Mapping[str, JsonValue]:
        """Probe endpoint and configured model capabilities."""

    @abstractmethod
    async def aclose(self) -> None:
        """Close underlying client resources."""

    async def __aenter__(self) -> Provider:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()
