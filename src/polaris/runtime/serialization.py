"""Stable serialization for provider values stored in the journal."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from polaris.providers import CompletionResult, Message, ToolCall, Usage


def serialize_tool_call(call: ToolCall) -> dict[str, object]:
    return {"id": call.id, "name": call.name, "arguments": dict(call.arguments)}


def deserialize_tool_call(value: Mapping[str, Any]) -> ToolCall:
    arguments = value.get("arguments", {})
    if not isinstance(arguments, Mapping):
        raise TypeError("serialized tool call arguments must be a mapping")
    return ToolCall(
        id=str(value["id"]),
        name=str(value["name"]),
        arguments=cast(Mapping[str, Any], arguments),
    )


def serialize_message(message: Message) -> dict[str, object]:
    content: object = message.content
    if isinstance(content, tuple):
        content = [dict(part) for part in content]
    return {
        "role": message.role,
        "content": content,
        "name": message.name,
        "tool_calls": [serialize_tool_call(call) for call in message.tool_calls],
        "tool_call_id": message.tool_call_id,
    }


def deserialize_message(value: Mapping[str, Any]) -> Message:
    calls = value.get("tool_calls", ())
    if not isinstance(calls, (list, tuple)):
        raise TypeError("serialized message tool_calls must be a sequence")
    content = value.get("content")
    if not (
        content is None
        or isinstance(content, str)
        or (
            isinstance(content, (list, tuple))
            and all(isinstance(part, Mapping) for part in content)
        )
    ):
        raise TypeError("serialized message content is invalid")
    return Message(
        role=cast(Any, value["role"]),
        content=cast(Any, content),
        name=cast(str | None, value.get("name")),
        tool_calls=tuple(deserialize_tool_call(cast(Mapping[str, Any], call)) for call in calls),
        tool_call_id=cast(str | None, value.get("tool_call_id")),
    )


def serialize_completion(result: CompletionResult) -> dict[str, object]:
    usage = result.usage
    return {
        "message": serialize_message(result.message),
        "model": result.model,
        "usage": {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "total_duration_ns": usage.total_duration_ns,
            "load_duration_ns": usage.load_duration_ns,
            "prompt_eval_duration_ns": usage.prompt_eval_duration_ns,
            "eval_duration_ns": usage.eval_duration_ns,
        },
        "finish_reason": result.finish_reason,
        "response_id": result.response_id,
    }


def deserialize_completion(value: Mapping[str, Any]) -> CompletionResult:
    message = value.get("message")
    usage = value.get("usage", {})
    if not isinstance(message, Mapping):
        raise TypeError("serialized completion message must be a mapping")
    if not isinstance(usage, Mapping):
        raise TypeError("serialized completion usage must be a mapping")
    return CompletionResult(
        message=deserialize_message(cast(Mapping[str, Any], message)),
        model=str(value["model"]),
        usage=Usage(
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            total_tokens=int(usage.get("total_tokens", 0)),
            total_duration_ns=cast(int | None, usage.get("total_duration_ns")),
            load_duration_ns=cast(int | None, usage.get("load_duration_ns")),
            prompt_eval_duration_ns=cast(int | None, usage.get("prompt_eval_duration_ns")),
            eval_duration_ns=cast(int | None, usage.get("eval_duration_ns")),
        ),
        finish_reason=cast(str | None, value.get("finish_reason")),
        response_id=cast(str | None, value.get("response_id")),
    )


message_to_dict = serialize_message
message_from_dict = deserialize_message
tool_call_to_dict = serialize_tool_call
tool_call_from_dict = deserialize_tool_call
completion_to_dict = serialize_completion
completion_from_dict = deserialize_completion
