from __future__ import annotations

from collections.abc import Mapping

import pytest

from polaris.tools import (
    DuplicateToolError,
    SafetyClass,
    ToolEntry,
    ToolRegistry,
    ToolRegistryError,
    ToolResultTooLargeError,
    ToolUnavailableError,
    UnknownToolError,
)
from polaris.tools.registry import JsonValue


async def echo(arguments: Mapping[str, JsonValue]) -> JsonValue:
    return {"echo": dict(arguments)}


async def reconcile(arguments: Mapping[str, JsonValue]) -> JsonValue:
    return {"found": arguments.get("id")}


def entry(
    *,
    name: str = "echo",
    available: object = None,
    safety: SafetyClass = SafetyClass.READ_ONLY,
    max_size: int | None = None,
) -> ToolEntry:
    check = available if callable(available) else None
    return ToolEntry(
        name=name,
        toolset="core",
        schema={"description": "Echo", "parameters": {"type": "object"}},
        handler=echo,
        availability_check=check,
        description="Echo values",
        safety_class=safety,
        reconcile_handler=reconcile if safety is SafetyClass.RECONCILABLE else None,
        max_result_size=max_size,
    )


@pytest.mark.asyncio
async def test_registration_definitions_alias_and_execution() -> None:
    registry = ToolRegistry()
    registry.register(entry(), aliases=("repeat",))
    definitions = registry.get_definitions(["repeat"])
    function = definitions[0]["function"]
    assert isinstance(function, dict)
    assert function["name"] == "echo"
    assert await registry.execute("repeat", {"x": 1}) == {"echo": {"x": 1}}
    assert registry.get_entry("repeat").safety_class is SafetyClass.READ_ONLY
    assert registry.generation == 1

    registry.register_alias("again", "repeat")
    assert registry.aliases()["again"] == "echo"
    assert registry.generation == 2


@pytest.mark.asyncio
async def test_availability_ttl_last_good_and_invalidation() -> None:
    now = [0.0]
    states = iter([True, False, False, False])
    calls = 0

    def check() -> bool:
        nonlocal calls
        calls += 1
        return next(states)

    registry = ToolRegistry(
        availability_ttl=1,
        last_good_grace=5,
        clock=lambda: now[0],
    )
    registry.register(entry(available=check))
    assert registry.get_definitions()
    assert registry.get_definitions()
    assert calls == 1

    now[0] = 2
    assert registry.get_definitions()
    assert calls == 2
    now[0] = 7
    assert registry.get_definitions() == ()
    assert calls == 3

    registry.invalidate_checks()
    assert registry.generation == 2
    with pytest.raises(ToolUnavailableError):
        await registry.execute("echo", {})


@pytest.mark.asyncio
async def test_duplicate_override_safety_reconcile_and_size() -> None:
    registry = ToolRegistry()
    registry.register(entry())
    with pytest.raises(DuplicateToolError):
        registry.register(entry())
    replacement = entry(safety=SafetyClass.RECONCILABLE)
    registry.register(replacement, override=True)
    assert registry.get_entry("echo") is replacement
    assert await registry.reconcile("echo", {"id": "job-1"}) == {"found": "job-1"}

    registry.register(entry(name="small", max_size=2))
    with pytest.raises(ToolResultTooLargeError):
        await registry.execute("small", {"long": "result"})


def test_safety_values_and_reconcile_contract() -> None:
    assert [value.value for value in SafetyClass] == [
        "read_only",
        "idempotent",
        "reconcilable",
        "opaque_side_effect",
    ]
    with pytest.raises(ValueError):
        ToolEntry(
            name="bad",
            toolset="core",
            schema={},
            handler=echo,
            safety_class=SafetyClass.RECONCILABLE,
        )


@pytest.mark.asyncio
async def test_registry_unknown_alias_and_non_reconcilable_errors() -> None:
    registry = ToolRegistry()
    with pytest.raises(UnknownToolError):
        registry.get_entry("missing")
    with pytest.raises(UnknownToolError):
        registry.register_alias("alias", "missing")
    registry.register(entry())
    with pytest.raises(DuplicateToolError):
        registry.register_alias("echo", "echo")
    with pytest.raises(ToolRegistryError):
        await registry.reconcile("echo", {})


def test_tool_entry_and_registry_validation() -> None:
    with pytest.raises(ValueError):
        ToolRegistry(availability_ttl=-1)
    with pytest.raises(TypeError):
        ToolEntry("", "core", {}, echo)
    with pytest.raises(TypeError):
        ToolEntry("name", "", {}, echo)
    with pytest.raises(TypeError):
        ToolEntry("name", "core", [], echo)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ToolEntry("name", "core", {}, echo, max_result_size=0)
    with pytest.raises(DuplicateToolError):
        ToolRegistry().register(entry(), aliases=("echo",))


def test_availability_exception_is_unavailable() -> None:
    def broken() -> bool:
        raise RuntimeError("probe failed")

    registry = ToolRegistry()
    registry.register(entry(available=broken))
    assert registry.get_definitions() == ()
