# Adapted from agent/azure_identity_adapter.py at b9b463f3bd6517b76687d9b3c9dea1e62f01f9e1.
# Copyright (c) Nous Research. MIT licensed; see THIRD_PARTY_NOTICES.md.
"""Lazy Microsoft Entra identity integration without token persistence."""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from .base import ProviderConfigurationError

DEFAULT_ENTRA_SCOPE = "https://ai.azure.com/.default"
COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"


@dataclass(frozen=True, slots=True)
class EntraIdentityConfig:
    """Serializable knobs for the Azure SDK credential chain."""

    scope: str = DEFAULT_ENTRA_SCOPE
    exclude_interactive_browser: bool = True

    def __post_init__(self) -> None:
        scope = self.scope.strip() if isinstance(self.scope, str) else ""
        if not scope:
            scope = DEFAULT_ENTRA_SCOPE
        if not isinstance(self.exclude_interactive_browser, bool):
            raise TypeError("exclude_interactive_browser must be a bool")
        object.__setattr__(self, "scope", scope)

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "scope": self.scope,
            "exclude_interactive_browser": self.exclude_interactive_browser,
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, object] | None,
        *,
        default_scope: str | None = None,
    ) -> EntraIdentityConfig:
        values = data or {}
        raw_scope = values.get("scope")
        scope = raw_scope.strip() if isinstance(raw_scope, str) else ""
        raw_exclude = values.get("exclude_interactive_browser", True)
        if not isinstance(raw_exclude, bool):
            raise TypeError("exclude_interactive_browser must be a bool")
        return cls(
            scope=scope or default_scope or DEFAULT_ENTRA_SCOPE,
            exclude_interactive_browser=raw_exclude,
        )


def has_azure_identity_installed() -> bool:
    try:
        import azure.identity  # noqa: F401
    except ImportError:
        return False
    return True


def _require_azure_identity() -> Any:
    try:
        import azure.identity as azure_identity
    except ImportError as exc:
        raise ProviderConfigurationError(
            "Entra authentication requires the optional azure-identity package"
        ) from exc
    return azure_identity


@functools.lru_cache(maxsize=4)
def build_credential(config: EntraIdentityConfig) -> Any:
    azure_identity = _require_azure_identity()
    kwargs: dict[str, bool] = {}
    if not config.exclude_interactive_browser:
        kwargs["exclude_interactive_browser_credential"] = False
    try:
        return azure_identity.DefaultAzureCredential(**kwargs)
    except Exception as exc:
        raise ProviderConfigurationError(
            f"could not initialize Entra credentials: {type(exc).__name__}"
        ) from None


def build_token_provider(
    scope: str | None = None,
    *,
    config: EntraIdentityConfig | None = None,
) -> Callable[[], str]:
    effective = config or EntraIdentityConfig(scope=scope or DEFAULT_ENTRA_SCOPE)
    azure_identity = _require_azure_identity()
    credential = build_credential(effective)
    try:
        provider = azure_identity.get_bearer_token_provider(credential, effective.scope)
    except Exception as exc:
        raise ProviderConfigurationError(
            f"could not initialize Entra token provider: {type(exc).__name__}"
        ) from None
    if not callable(provider):
        raise ProviderConfigurationError("azure-identity returned an invalid token provider")
    return cast(Callable[[], str], provider)


def reset_credential_cache() -> None:
    build_credential.cache_clear()
