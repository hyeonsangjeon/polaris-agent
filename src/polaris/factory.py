"""Construction of configured providers and tools."""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from polaris.providers import (
    AzureFoundryProvider,
    EntraIdentityConfig,
    FoundryModelRouterProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    Provider,
    ProviderConfig,
    ProviderConfigurationError,
)
from polaris.tools import ToolRegistry, build_default_registry

from .config import AppConfig, ProviderSpec, secret_from_env

TransportMap = Mapping[str, httpx.AsyncBaseTransport]


def create_provider(
    name: str,
    spec: ProviderSpec,
    *,
    offline: bool = False,
    offline_allowed_hosts: tuple[str, ...] = (),
    offline_allow_private_ips: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    token_provider: object | None = None,
    env: dict[str, str] | None = None,
) -> Provider:
    secret = secret_from_env(spec.api_key_env, env)
    if spec.api_key_env is not None and secret is None:
        raise ProviderConfigurationError(
            f"provider {name!r} requires environment variable {spec.api_key_env}"
        )
    config = ProviderConfig(
        model=spec.model,
        base_url=str(spec.base_url).rstrip("/"),
        api_key=secret if spec.kind in {"azure_foundry", "foundry_router"} else None,
        api_mode=spec.api_mode,
        timeout_seconds=spec.timeout_seconds,
        offline=offline,
        offline_allowed_hosts=offline_allowed_hosts,
        offline_allow_private_ips=offline_allow_private_ips,
        headers=spec.headers,
    )
    if spec.kind == "ollama":
        return OllamaProvider(config, transport=transport)
    if spec.kind == "openai_compatible":
        async def auth_headers() -> dict[str, str]:
            return {"Authorization": f"Bearer {secret}"} if secret else {}

        return OpenAICompatibleProvider(
            config,
            transport=transport,
            header_provider=auth_headers if secret else None,
        )
    auth = spec.azure_auth or "api_key"
    entra = (
        EntraIdentityConfig(scope=spec.entra_scope)
        if auth == "entra" and token_provider is None
        else None
    )
    provider_type = (
        FoundryModelRouterProvider
        if spec.kind == "foundry_router"
        else AzureFoundryProvider
    )
    return provider_type(
        config,
        entra=entra,
        token_provider=token_provider,  # type: ignore[arg-type]
        transport=transport,
    )


def create_providers(
    config: AppConfig,
    *,
    transports: TransportMap | None = None,
    env: dict[str, str] | None = None,
    token_providers: Mapping[str, object] | None = None,
) -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    try:
        for name, spec in config.providers.items():
            provider = create_provider(
                name,
                spec,
                offline=config.offline.enabled,
                offline_allowed_hosts=config.offline.allowed_hosts,
                offline_allow_private_ips=config.offline.allow_private_ips,
                transport=(transports or {}).get(name),
                token_provider=(token_providers or {}).get(name),
                env=env,
            )
            providers[name] = provider
            for alias in spec.aliases:
                providers[alias] = provider
    except BaseException:
        # Construction has no event loop in which to close already-created async clients.
        # They remain reachable only until this exception unwinds and have made no requests.
        raise
    return providers


def build_tools(config: AppConfig) -> ToolRegistry:
    return build_default_registry(
        config.tools.roots,
        str(config.tools.searxng_url).rstrip("/") if config.tools.searxng_url else None,
        config.tools.allow_private_http,
        include_network=not config.offline.enabled,
    )


def ensemble_provider_map(
    config: AppConfig, providers: Mapping[str, Provider]
) -> dict[str, Provider]:
    names = {worker.provider for worker in config.workers}
    names.update(item for item in (config.verifier, config.synthesizer) if item is not None)
    result: dict[str, Provider] = {}
    for name in names:
        try:
            result[name] = providers[name]
        except KeyError as exc:
            raise ProviderConfigurationError(
                f"ensemble provider {name!r} is not configured"
            ) from exc
    return result


build_provider = create_provider
build_providers = create_providers
build_tool_registry = build_tools
