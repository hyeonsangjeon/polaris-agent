# Adapted from plugins/model-providers/azure-foundry/__init__.py at
# b9b463f3bd6517b76687d9b3c9dea1e62f01f9e1.
# Copyright (c) Nous Research. MIT licensed; see THIRD_PARTY_NOTICES.md.
"""Azure Foundry provider with static-key or per-request Entra authentication."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable

import httpx

from .azure_identity import EntraIdentityConfig, build_token_provider
from .base import ProviderConfig, ProviderConfigurationError
from .openai_compatible import OpenAICompatibleProvider

TokenProvider = Callable[[], str | Awaitable[str]]


class AzureFoundryProvider(OpenAICompatibleProvider):
    """OpenAI-compatible Azure transport with Azure-specific authentication."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        entra: EntraIdentityConfig | None = None,
        token_provider: TokenProvider | None = None,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if config.api_key and (entra is not None or token_provider is not None):
            raise ProviderConfigurationError("configure either a static API key or Entra auth")
        if not config.api_key and token_provider is None:
            token_provider = build_token_provider(config=entra or EntraIdentityConfig())
        self._azure_token_provider = token_provider
        super().__init__(config, client=client, transport=transport)

    async def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        headers.update(self.config.headers)
        if self.config.api_key:
            headers["api-key"] = self.config.api_key
            return headers
        if self._azure_token_provider is None:
            raise ProviderConfigurationError("Entra token provider is not configured")
        token = self._azure_token_provider()
        if inspect.isawaitable(token):
            token = await token
        if not isinstance(token, str) or not token:
            raise ProviderConfigurationError("Entra token provider returned no token")
        headers["Authorization"] = f"Bearer {token}"
        return headers
