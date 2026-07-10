"""Thin Microsoft Foundry Model Router provider profile.

Foundry owns model selection, routing mode, model subsets, and failover. Polaris
only supplies durable execution and records the actual model returned in
``response.model``.
"""

from __future__ import annotations

import httpx

from .azure_foundry import AzureFoundryProvider, TokenProvider
from .azure_identity import EntraIdentityConfig
from .base import ProviderConfig, ProviderConfigurationError


class FoundryModelRouterProvider(AzureFoundryProvider):
    """Azure Foundry Responses provider configured for a model-router deployment."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        entra: EntraIdentityConfig | None = None,
        token_provider: TokenProvider | None = None,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if config.api_mode != "responses":
            raise ProviderConfigurationError(
                "Foundry Model Router requires the Responses API"
            )
        super().__init__(
            config,
            entra=entra,
            token_provider=token_provider,
            client=client,
            transport=transport,
        )
