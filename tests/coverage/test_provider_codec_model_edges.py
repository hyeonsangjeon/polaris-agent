from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from polaris.ensemble import Claim, Evidence, ResearchConfig, validate_evidence_integrity
from polaris.journal.codec import canonical_json, decode_json, normalize_timestamp, sha256_hex
from polaris.providers import (
    EntraIdentityConfig,
    Message,
    OllamaProvider,
    ProviderConfig,
    ProviderConfigurationError,
    ProviderProtocolError,
    ToolCall,
    azure_identity,
)
from polaris.runtime import (
    deserialize_completion,
    deserialize_message,
    deserialize_tool_call,
    serialize_message,
)


class Value(Enum):
    ONE = "one"


@dataclass
class Payload:
    value: int


def test_codec_normalizes_and_canonicalizes_supported_types() -> None:
    naive = datetime(2024, 1, 2, 3, 4, 5)
    aware = datetime(2024, 1, 2, 4, 4, 5, tzinfo=UTC)
    assert normalize_timestamp(naive) == "2024-01-02T03:04:05.000000Z"
    assert normalize_timestamp(aware) == "2024-01-02T04:04:05.000000Z"
    assert normalize_timestamp("2024-01-02T03:04:05Z").endswith("Z")
    assert normalize_timestamp(None).endswith("Z")

    encoded = canonical_json(
        {
            "enum": Value.ONE,
            "date": naive,
            "dataclass": Payload(2),
            "mapping": MappingProxyType({"x": 1}),
            "set": {"b", "a"},
        }
    )
    assert encoded == (
        '{"dataclass":{"value":2},"date":"2024-01-02T03:04:05.000000Z",'
        '"enum":"one","mapping":{"x":1},"set":["a","b"]}'
    )
    assert sha256_hex(b"bytes") == hashlib.sha256(b"bytes").hexdigest()
    frozen = decode_json('{"items":[{"x":1}]}')
    assert isinstance(frozen, MappingProxyType)
    assert isinstance(frozen["items"], tuple)
    assert decode_json(None) is None
    with pytest.raises(TypeError, match="not JSON serializable"):
        canonical_json(object())


@pytest.mark.parametrize(
    ("function", "value", "message"),
    [
        (deserialize_tool_call, {"id": "x", "name": "n", "arguments": []}, "mapping"),
        (
            deserialize_message,
            {"role": "assistant", "content": "", "tool_calls": {}},
            "sequence",
        ),
        (
            deserialize_message,
            {"role": "assistant", "content": 3, "tool_calls": []},
            "content",
        ),
        (deserialize_completion, {"message": [], "model": "m"}, "message"),
        (
            deserialize_completion,
            {"message": {"role": "assistant", "content": ""}, "model": "m", "usage": []},
            "usage",
        ),
    ],
)
def test_runtime_deserialization_rejects_corrupt_journal_values(
    function: Any, value: dict[str, object], message: str
) -> None:
    with pytest.raises(TypeError, match=message):
        function(value)


def test_message_serialization_handles_plain_and_multipart_content() -> None:
    plain = Message("assistant", None, tool_calls=(ToolCall("id", "name", {}),))
    assert serialize_message(plain)["content"] is None
    multipart = Message("user", [{"type": "text", "text": "hello"}])
    serialized = serialize_message(multipart)
    assert serialized["content"] == [{"type": "text", "text": "hello"}]
    assert deserialize_message(serialized) == multipart


def _ollama(payload: object) -> OllamaProvider:
    return OllamaProvider(
        ProviderConfig(model="chosen", base_url="http://localhost:11434"),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    )


@pytest.mark.asyncio
async def test_ollama_invalid_json_tags_and_completion_shapes() -> None:
    invalid_json = OllamaProvider(
        ProviderConfig(model="chosen", base_url="http://localhost:11434"),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"{")),
    )
    with pytest.raises(ProviderProtocolError, match="invalid JSON"):
        await invalid_json.list_models()
    await invalid_json.aclose()

    tags = _ollama({"models": "not-list"})
    with pytest.raises(ProviderProtocolError, match="models"):
        await tags.list_models()
    await tags.aclose()

    for payload, message in (
        ({"message": []}, "message"),
        ({"message": {"content": 1}}, "content"),
        ({"message": {"content": ""}, "model": 1}, "model"),
        ({"message": {"content": "", "tool_calls": {}}}, "tool calls"),
        (
            {"message": {"content": "", "tool_calls": [{"function": []}]}},
            "function",
        ),
        (
            {
                "message": {
                    "content": "",
                    "tool_calls": [{"function": {"name": 1, "arguments": {}}}],
                }
            },
            "incomplete",
        ),
    ):
        provider = _ollama(payload)
        with pytest.raises(ProviderProtocolError, match=message):
            await provider.complete([Message("user", "hello")])
        await provider.aclose()

    provider = _ollama({"message": {"content": ""}})
    with pytest.raises(TypeError, match="Message"):
        await provider.complete(["bad"])  # type: ignore[list-item]
    await provider.aclose()


@pytest.mark.asyncio
async def test_ollama_model_filtering_usage_defaults_and_doctor_shapes() -> None:
    tags = _ollama(
        {
            "models": [
                1,
                {},
                {"name": ""},
                {"model": "chosen"},
                {"name": "chosen"},
            ]
        }
    )
    assert await tags.list_models() == ("chosen",)
    await tags.aclose()

    completion = _ollama(
        {
            "message": {
                "content": None,
                "tool_calls": [
                    {"id": "call", "function": {"name": "lookup", "arguments": {"x": 1}}}
                ],
            },
            "prompt_eval_count": True,
            "eval_count": -1,
            "done": True,
        }
    )
    result = await completion.complete([Message("user", "hello")])
    assert result.usage.total_tokens == 0
    assert result.finish_reason == "stop"
    assert result.tool_calls[0].id == "call"
    await completion.aclose()

    responses = iter(
        [
            {"models": [{"name": "chosen"}]},
            {"capabilities": "invalid", "model_info": "invalid"},
        ]
    )
    doctor = OllamaProvider(
        ProviderConfig(model="chosen", base_url="http://localhost:11434"),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=next(responses))),
    )
    status = await doctor.doctor()
    assert status["capabilities"] == []
    assert status["context_length"] is None
    await doctor.aclose()


def test_azure_identity_optional_dependency_and_factory_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "azure", None)
    monkeypatch.delitem(sys.modules, "azure.identity", raising=False)
    assert azure_identity.has_azure_identity_installed() is False
    with pytest.raises(ProviderConfigurationError, match="optional"):
        azure_identity._require_azure_identity()

    class BrokenCredential:
        @staticmethod
        def DefaultAzureCredential(**_kwargs: bool) -> object:
            raise RuntimeError("credential secret")

    monkeypatch.setattr(azure_identity, "_require_azure_identity", lambda: BrokenCredential)
    azure_identity.reset_credential_cache()
    with pytest.raises(ProviderConfigurationError) as caught:
        azure_identity.build_credential(EntraIdentityConfig(exclude_interactive_browser=False))
    assert "credential secret" not in str(caught.value)

    class BrokenProvider:
        @staticmethod
        def get_bearer_token_provider(_credential: object, _scope: str) -> object:
            raise RuntimeError("token secret")

    monkeypatch.setattr(azure_identity, "_require_azure_identity", lambda: BrokenProvider)
    monkeypatch.setattr(azure_identity, "build_credential", lambda _config: object())
    with pytest.raises(ProviderConfigurationError) as caught:
        azure_identity.build_token_provider()
    assert "token secret" not in str(caught.value)

    class InvalidProvider:
        @staticmethod
        def get_bearer_token_provider(_credential: object, _scope: str) -> object:
            return "not callable"

    monkeypatch.setattr(azure_identity, "_require_azure_identity", lambda: InvalidProvider)
    with pytest.raises(ProviderConfigurationError, match="invalid token provider"):
        azure_identity.build_token_provider(scope="scope")

    with pytest.raises(TypeError, match="bool"):
        EntraIdentityConfig(exclude_interactive_browser="false")  # type: ignore[arg-type]


def _claim(
    *,
    claim_id: str = "c",
    evidence_ids: tuple[str, ...] = ("e",),
    supporters: tuple[str, ...] = ("w1",),
    opponents: tuple[str, ...] = (),
    status: str = "consensus",
) -> Claim:
    return Claim(
        id=claim_id,
        statement="statement",
        evidence_ids=evidence_ids,
        supporters=supporters,
        opponents=opponents,
        status=status,  # type: ignore[arg-type]
        confidence=0.5,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"evidence_ids": ("e", "e")},
        {"supporters": ("w1", "w1")},
        {"supporters": ("w1",), "opponents": ("w1",), "status": "disputed"},
        {"supporters": (), "status": "consensus"},
        {"evidence_ids": (), "status": "consensus"},
        {"supporters": ("w1",), "opponents": (), "status": "disputed"},
    ],
)
def test_claim_coherence_rejects_ambiguous_states(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _claim(**kwargs)  # type: ignore[arg-type]


def test_evidence_model_and_integrity_duplicate_hash_boundaries() -> None:
    quote = "quote"
    digest = hashlib.sha256(quote.encode()).hexdigest()
    evidence = Evidence(source_id="e", quote=quote, content_hash=digest)
    with pytest.raises(ValidationError, match="empty"):
        Evidence(source_id="e", quote=quote, title=" ", content_hash=digest)

    with pytest.raises(ValueError, match="source_id"):
        validate_evidence_integrity((), (evidence, evidence), set())
    claim = _claim()
    with pytest.raises(ValueError, match="claim ids"):
        validate_evidence_integrity((claim, claim), (evidence,), {"w1"})
    bad = Evidence(source_id="e", quote=quote, content_hash="0" * 64)
    with pytest.raises(ValueError, match="content_hash"):
        validate_evidence_integrity((claim,), (bad,), {"w1"})

    budget = ResearchConfig(
        verifier_name="verify",
        synthesizer_name="synthesize",
        worker_budget=__import__("polaris.journal", fromlist=["Budget"]).Budget(call_limit=2),
    )
    assert budget.worker_budget.call_limit == 2
