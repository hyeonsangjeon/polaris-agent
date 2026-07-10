"""Provider contracts and built-in HTTP providers."""

from .azure_foundry import AzureFoundryProvider
from .azure_identity import (
    COGNITIVE_SERVICES_SCOPE,
    DEFAULT_ENTRA_SCOPE,
    EntraIdentityConfig,
    build_credential,
    build_token_provider,
    has_azure_identity_installed,
    reset_credential_cache,
)
from .base import (
    CompletionResult,
    Message,
    OfflineViolation,
    Provider,
    ProviderAuthenticationError,
    ProviderCapabilities,
    ProviderCapabilityError,
    ProviderConfig,
    ProviderConfigurationError,
    ProviderError,
    ProviderProfile,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderRegistry,
    ProviderTransportError,
    ToolCall,
    Usage,
)
from .foundry_router import FoundryModelRouterProvider
from .ollama import OllamaProvider
from .openai_compatible import OpenAICompatibleProvider

__all__ = [
    "COGNITIVE_SERVICES_SCOPE",
    "DEFAULT_ENTRA_SCOPE",
    "AzureFoundryProvider",
    "CompletionResult",
    "EntraIdentityConfig",
    "FoundryModelRouterProvider",
    "Message",
    "OfflineViolation",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "Provider",
    "ProviderAuthenticationError",
    "ProviderCapabilities",
    "ProviderCapabilityError",
    "ProviderConfig",
    "ProviderConfigurationError",
    "ProviderError",
    "ProviderProfile",
    "ProviderProtocolError",
    "ProviderRateLimitError",
    "ProviderRegistry",
    "ProviderTransportError",
    "ToolCall",
    "Usage",
    "build_credential",
    "build_token_provider",
    "has_azure_identity_installed",
    "reset_credential_cache",
]
