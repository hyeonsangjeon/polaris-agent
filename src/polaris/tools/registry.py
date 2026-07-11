# Adapted from tools/registry.py at b9b463f3bd6517b76687d9b3c9dea1e62f01f9e1.
# Copyright (c) Nous Research. MIT licensed; see THIRD_PARTY_NOTICES.md.
"""Typed, thread-safe tool registration and execution."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
ToolArguments: TypeAlias = Mapping[str, JsonValue]
ToolResult: TypeAlias = JsonValue
ToolHandler: TypeAlias = Callable[[ToolArguments], Awaitable[ToolResult]]
AvailabilityCheck: TypeAlias = Callable[[], bool]
ReconcileHandler: TypeAlias = Callable[[ToolArguments], Awaitable[ToolResult]]


class ToolRegistryError(RuntimeError):
    """Base class for tool registry errors."""


class DuplicateToolError(ToolRegistryError):
    """A name or alias is already registered."""


class UnknownToolError(ToolRegistryError):
    """A requested tool does not exist."""


class ToolUnavailableError(ToolRegistryError):
    """A registered tool is currently unavailable."""


class ToolResultTooLargeError(ToolRegistryError):
    """A tool result exceeded its declared size contract."""


class SafetyClass(StrEnum):
    """Journal-compatible tool safety classifications."""

    READ_ONLY = "read_only"
    IDEMPOTENT = "idempotent"
    RECONCILABLE = "reconcilable"
    OPAQUE_SIDE_EFFECT = "opaque_side_effect"


def _copy_json(value: object, label: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, list):
        return [_copy_json(item, label) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{label} keys must be strings")
            result[key] = _copy_json(item, label)
        return result
    raise TypeError(f"{label} must contain only JSON values")


@dataclass(frozen=True, slots=True)
class ToolEntry:
    """Metadata and handlers for one tool."""

    name: str
    toolset: str
    schema: Mapping[str, JsonValue]
    handler: ToolHandler
    availability_check: AvailabilityCheck | None = None
    description: str = ""
    safety_class: SafetyClass = SafetyClass.OPAQUE_SIDE_EFFECT
    reconcile_handler: ReconcileHandler | None = None
    max_result_size: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise TypeError("ToolEntry.name must be a non-empty string")
        if not isinstance(self.toolset, str) or not self.toolset.strip():
            raise TypeError("ToolEntry.toolset must be a non-empty string")
        copied = _copy_json(self.schema, "ToolEntry.schema")
        if not isinstance(copied, dict):
            raise TypeError("ToolEntry.schema must be a mapping")
        object.__setattr__(self, "schema", MappingProxyType(copied))
        if not callable(self.handler):
            raise TypeError("ToolEntry.handler must be callable")
        if self.availability_check is not None and not callable(self.availability_check):
            raise TypeError("ToolEntry.availability_check must be callable or None")
        if not isinstance(self.description, str):
            raise TypeError("ToolEntry.description must be a string")
        if not isinstance(self.safety_class, SafetyClass):
            raise TypeError("ToolEntry.safety_class must be SafetyClass")
        if self.reconcile_handler is not None and not callable(self.reconcile_handler):
            raise TypeError("ToolEntry.reconcile_handler must be callable or None")
        if self.safety_class is SafetyClass.RECONCILABLE and self.reconcile_handler is None:
            raise ValueError("reconcilable tools require a reconcile_handler")
        if self.max_result_size is not None and (
            not isinstance(self.max_result_size, int)
            or isinstance(self.max_result_size, bool)
            or self.max_result_size <= 0
        ):
            raise TypeError("ToolEntry.max_result_size must be a positive integer or None")


@dataclass(slots=True)
class _Availability:
    checked_at: float
    value: bool


class ToolRegistry:
    """Thread-safe registry with TTL-cached availability probes."""

    def __init__(
        self,
        *,
        availability_ttl: float = 30.0,
        last_good_grace: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if availability_ttl < 0 or last_good_grace < 0:
            raise ValueError("availability cache durations must be non-negative")
        self._entries: dict[str, ToolEntry] = {}
        self._aliases: dict[str, str] = {}
        self._availability: dict[AvailabilityCheck, _Availability] = {}
        self._last_good: dict[AvailabilityCheck, float] = {}
        self._availability_ttl = float(availability_ttl)
        self._last_good_grace = float(last_good_grace)
        self._clock = clock
        self._generation = 0
        self._lock = threading.RLock()

    @staticmethod
    def _name(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise TypeError("tool names must be non-empty strings")
        return value.strip()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def register(
        self,
        entry: ToolEntry,
        *,
        aliases: Iterable[str] = (),
        override: bool = False,
    ) -> ToolEntry:
        if not isinstance(entry, ToolEntry):
            raise TypeError("entry must be ToolEntry")
        canonical = self._name(entry.name)
        normalized_aliases = tuple(self._name(alias) for alias in aliases)
        if (
            canonical in normalized_aliases
            or len(set(normalized_aliases)) != len(normalized_aliases)
        ):
            raise DuplicateToolError(f"duplicate alias for tool {canonical!r}")
        with self._lock:
            occupied = set(self._entries) | set(self._aliases)
            if not override:
                collision = next(
                    (name for name in (canonical, *normalized_aliases) if name in occupied),
                    None,
                )
                if collision is not None:
                    raise DuplicateToolError(
                        f"tool name or alias already registered: {collision!r}"
                    )
            elif canonical in self._aliases:
                raise DuplicateToolError(f"cannot override alias with tool: {canonical!r}")
            previous = self._entries.get(canonical)
            if previous is not None:
                self._aliases = {
                    alias: target for alias, target in self._aliases.items() if target != canonical
                }
            for alias in normalized_aliases:
                target = self._aliases.get(alias)
                if alias in self._entries and alias != canonical:
                    raise DuplicateToolError(f"alias collides with tool: {alias!r}")
                if target is not None and target != canonical:
                    raise DuplicateToolError(f"alias already registered: {alias!r}")
            self._entries[canonical] = entry
            self._aliases.update({alias: canonical for alias in normalized_aliases})
            self._generation += 1
            return entry

    def register_alias(self, alias: str, target: str) -> None:
        alias = self._name(alias)
        with self._lock:
            canonical = self._aliases.get(target, target)
            if canonical not in self._entries:
                raise UnknownToolError(f"unknown tool: {target!r}")
            if alias in self._entries or alias in self._aliases:
                raise DuplicateToolError(f"tool name or alias already registered: {alias!r}")
            self._aliases[alias] = canonical
            self._generation += 1

    def get_entry(self, name: str) -> ToolEntry:
        with self._lock:
            canonical = self._aliases.get(name, name)
            try:
                return self._entries[canonical]
            except KeyError as exc:
                raise UnknownToolError(f"unknown tool: {name!r}") from exc

    def aliases(self) -> Mapping[str, str]:
        with self._lock:
            return MappingProxyType(dict(self._aliases))

    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._entries))

    def clone(self) -> ToolRegistry:
        """Create an isolated registry snapshot with the same immutable entries."""
        with self._lock:
            entries = tuple(self._entries.values())
            aliases = dict(self._aliases)
            clone = ToolRegistry(
                availability_ttl=self._availability_ttl,
                last_good_grace=self._last_good_grace,
                clock=self._clock,
            )
        for entry in entries:
            clone.register(entry)
        for alias, target in aliases.items():
            clone.register_alias(alias, target)
        return clone

    snapshot = clone

    def _is_available(self, entry: ToolEntry) -> bool:
        check = entry.availability_check
        if check is None:
            return True
        now = self._clock()
        with self._lock:
            cached = self._availability.get(check)
            if cached is not None and now - cached.checked_at < self._availability_ttl:
                return cached.value
        try:
            available = bool(check())
        except Exception:
            available = False
        with self._lock:
            if available:
                self._last_good[check] = now
                self._availability[check] = _Availability(now, True)
                return True
            last_good = self._last_good.get(check)
            if last_good is not None and now - last_good < self._last_good_grace:
                self._availability.pop(check, None)
                return True
            self._availability[check] = _Availability(now, False)
            return False

    def get_definitions(
        self,
        names: Iterable[str] | None = None,
        *,
        toolsets: Iterable[str] | None = None,
    ) -> tuple[dict[str, JsonValue], ...]:
        selected_names = None if names is None else set(names)
        selected_toolsets = None if toolsets is None else set(toolsets)
        with self._lock:
            entries = tuple(self._entries.values())
            aliases = dict(self._aliases)
        if selected_names is not None:
            selected_names = {aliases.get(name, name) for name in selected_names}
        result: list[dict[str, JsonValue]] = []
        for entry in sorted(entries, key=lambda item: item.name):
            if selected_names is not None and entry.name not in selected_names:
                continue
            if selected_toolsets is not None and entry.toolset not in selected_toolsets:
                continue
            if not self._is_available(entry):
                continue
            function = dict(entry.schema)
            function["name"] = entry.name
            if entry.description and "description" not in function:
                function["description"] = entry.description
            result.append({"type": "function", "function": function})
        return tuple(result)

    async def execute(self, name: str, arguments: ToolArguments) -> ToolResult:
        entry = self.get_entry(name)
        copied = _copy_json(arguments, "tool arguments")
        if not isinstance(copied, dict):
            raise TypeError("tool arguments must be a mapping")
        if not self._is_available(entry):
            raise ToolUnavailableError(f"tool is unavailable: {entry.name!r}")
        result = await entry.handler(MappingProxyType(copied))
        normalized = _copy_json(result, "tool result")
        if entry.max_result_size is not None:
            size = len(json.dumps(normalized, ensure_ascii=False, separators=(",", ":")))
            if size > entry.max_result_size:
                raise ToolResultTooLargeError(
                    f"tool result exceeded max_result_size for {entry.name!r}"
                )
        return normalized

    async def reconcile(self, name: str, arguments: ToolArguments) -> ToolResult:
        entry = self.get_entry(name)
        if entry.reconcile_handler is None:
            raise ToolRegistryError(f"tool does not support reconciliation: {entry.name!r}")
        copied = _copy_json(arguments, "reconcile arguments")
        if not isinstance(copied, dict):
            raise TypeError("reconcile arguments must be a mapping")
        return _copy_json(
            await entry.reconcile_handler(MappingProxyType(copied)),
            "reconcile result",
        )

    def invalidate_checks(self) -> None:
        with self._lock:
            self._availability.clear()
            self._last_good.clear()
            self._generation += 1


registry = ToolRegistry()
