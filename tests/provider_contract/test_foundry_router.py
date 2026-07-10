from __future__ import annotations

import httpx
import pytest

from polaris.providers import (
    FoundryModelRouterProvider,
    Message,
    ProviderConfig,
    ProviderConfigurationError,
)


@pytest.mark.asyncio
async def test_foundry_router_uses_responses_and_records_selected_model() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/responses")
        return httpx.Response(
            200,
            json={
                "id": "response-router",
                "model": "gpt-4.1-nano",
                "output": [
                    {
                        "type": "message",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "routed"}],
                    }
                ],
                "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
            },
        )

    provider = FoundryModelRouterProvider(
        ProviderConfig(
            model="model-router",
            base_url="https://resource.services.ai.azure.com/openai/v1",
            api_key="test-key",
            api_mode="responses",
        ),
        transport=httpx.MockTransport(handler),
    )
    result = await provider.complete([Message("user", "Route this")])
    assert result.model == "gpt-4.1-nano"
    assert result.content == "routed"
    await provider.aclose()


def test_foundry_router_rejects_chat_completions_mode() -> None:
    with pytest.raises(ProviderConfigurationError, match="Responses"):
        FoundryModelRouterProvider(
            ProviderConfig(
                model="model-router",
                base_url="https://resource.services.ai.azure.com/openai/v1",
                api_key="test-key",
                api_mode="chat_completions",
            )
        )
