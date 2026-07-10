from __future__ import annotations

import os
import stat
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

import polaris.tools.defaults as tool_defaults
from polaris.config import (
    AppConfig,
    OfflinePolicy,
    ProviderSpec,
    ToolConfig,
    load_config,
    save_config,
)
from polaris.factory import (
    build_tools,
    create_provider,
    create_providers,
    ensemble_provider_map,
)
from polaris.paths import PolarisPaths
from polaris.providers import (
    AzureFoundryProvider,
    FoundryModelRouterProvider,
    OllamaProvider,
    ProviderConfigurationError,
)


def spec(**values: object) -> ProviderSpec:
    payload: dict[str, object] = {
        "kind": "openai_compatible",
        "model": "test-model",
        "base_url": "https://models.example/v1",
    }
    payload.update(values)
    return ProviderSpec.model_validate(payload)


def test_config_write_is_atomic_private_and_round_trips(tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "config.json"
    config = AppConfig(data_dir=tmp_path.resolve(), tools=ToolConfig(roots=(tmp_path,)))
    save_config(config, destination)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert load_config(destination) == config
    assert not list(destination.parent.glob("*.tmp"))


def test_config_rejects_invalid_endpoint_and_root(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        spec(base_url="file:///etc/passwd")
    with pytest.raises(ValidationError, match="existing directory"):
        ToolConfig(roots=(tmp_path / "missing",))


def test_offline_policy_requires_local_or_explicit_private_endpoint(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="offline policy"):
        AppConfig(
            data_dir=tmp_path,
            providers={"remote": spec()},
            tools=ToolConfig(roots=(tmp_path,)),
            offline=OfflinePolicy(enabled=True),
        )
    config = AppConfig(
        data_dir=tmp_path,
        providers={"private": spec(base_url="http://10.1.2.3:8000/v1")},
        tools=ToolConfig(roots=(tmp_path,)),
        offline=OfflinePolicy(enabled=True),
    )
    assert config.offline.enabled

    explicitly_allowed = AppConfig(
        data_dir=tmp_path,
        providers={"nas": spec(base_url="http://ollama.internal:11434")},
        tools=ToolConfig(roots=(tmp_path,)),
        offline=OfflinePolicy(
            enabled=True,
            allowed_hosts=("ollama.internal",),
            allow_private_ips=False,
        ),
    )
    assert explicitly_allowed.offline.allowed_hosts == ("ollama.internal",)


def test_offline_factory_omits_network_tools_without_constructing_them(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def unexpected_network_tool(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("offline registry constructed a network tool")

    monkeypatch.setattr(tool_defaults, "HTTPFetchTool", unexpected_network_tool)
    monkeypatch.setattr(tool_defaults, "SearXNGSearchTool", unexpected_network_tool)
    config = AppConfig(
        data_dir=tmp_path,
        tools=ToolConfig.model_validate(
            {"roots": (tmp_path,), "searxng_url": "http://search.internal"}
        ),
        offline=OfflinePolicy(enabled=True),
    )

    names = build_tools(config).names()

    assert "http_fetch" not in names
    assert "search" not in names
    assert "web_search" not in names
    assert {"terminal", "read_file", "write_file"} <= set(names)


def test_factory_passes_offline_private_policy_to_ollama(tmp_path: Path) -> None:
    config = AppConfig(
        data_dir=tmp_path,
        providers={
            "ollama": spec(
                kind="ollama",
                base_url="http://192.168.10.20:11434",
                api_key_env=None,
            )
        },
        tools=ToolConfig(roots=(tmp_path,)),
        offline=OfflinePolicy(enabled=True, allow_private_ips=True),
    )

    provider = create_providers(config)["ollama"]

    assert isinstance(provider, OllamaProvider)
    __import__("asyncio").run(provider.aclose())


def test_factory_uses_environment_secret_without_repr_or_config_leak() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, json={"data": [{"id": "test-model"}]})

    provider_spec = spec(api_key_env="MODEL_TOKEN")
    provider = create_provider(
        "remote",
        provider_spec,
        transport=httpx.MockTransport(handler),
        env={"MODEL_TOKEN": "super-secret"},
    )
    assert "super-secret" not in repr(provider_spec)
    assert "super-secret" not in repr(provider.config)

    models = __import__("asyncio").run(provider.list_models())
    __import__("asyncio").run(provider.aclose())
    assert models == ("test-model",)
    assert captured["authorization"] == "Bearer super-secret"


def test_factory_reports_secret_by_name_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_MODEL_TOKEN", raising=False)
    with pytest.raises(ProviderConfigurationError, match="MISSING_MODEL_TOKEN"):
        create_provider("remote", spec(api_key_env="MISSING_MODEL_TOKEN"))
    assert "MISSING_MODEL_TOKEN" not in os.environ


def test_factory_builds_aliases_tools_and_azure_headers(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": []})

    azure = ProviderSpec.model_validate(
        {
            "kind": "azure_foundry",
            "model": "deployment",
            "base_url": "https://example.openai.azure.com/openai/v1",
            "api_key_env": "AZURE_KEY",
            "azure_auth": "api_key",
            "aliases": ("verify",),
        }
    )
    config = AppConfig(
        data_dir=tmp_path,
        providers={"azure": azure},
        tools=ToolConfig(roots=(tmp_path,)),
        verifier="verify",
        synthesizer="azure",
    )
    providers = create_providers(
        config,
        transports={"azure": httpx.MockTransport(handler)},
        env={"AZURE_KEY": "secret-key"},
    )
    assert isinstance(providers["azure"], AzureFoundryProvider)
    assert providers["verify"] is providers["azure"]
    assert ensemble_provider_map(config, providers) == {
        "verify": providers["azure"],
        "azure": providers["azure"],
    }
    assert "read_file" in build_tools(config).names()
    __import__("asyncio").run(providers["azure"].list_models())
    __import__("asyncio").run(providers["azure"].aclose())
    assert requests[0].headers["api-key"] == "secret-key"


def test_factory_constructs_ollama_and_entra_provider() -> None:
    ollama = ProviderSpec.model_validate(
        {
            "kind": "ollama",
            "model": "local",
            "base_url": "http://localhost:11434",
        }
    )
    assert isinstance(create_provider("local", ollama), OllamaProvider)

    entra = ProviderSpec.model_validate(
        {
            "kind": "azure_foundry",
            "model": "deployment",
            "base_url": "https://example.openai.azure.com/openai/v1",
            "azure_auth": "entra",
        }
    )
    provider = create_provider("entra", entra, token_provider=lambda: "token")
    assert isinstance(provider, AzureFoundryProvider)
    __import__("asyncio").run(provider.aclose())

    router = ProviderSpec.model_validate(
        {
            "kind": "foundry_router",
            "model": "model-router",
            "base_url": "https://resource.services.ai.azure.com/openai/v1",
            "api_mode": "responses",
            "azure_auth": "entra",
        }
    )
    routed = create_provider("router", router, token_provider=lambda: "token")
    assert isinstance(routed, FoundryModelRouterProvider)
    __import__("asyncio").run(routed.aclose())


def test_foundry_router_config_requires_responses_mode() -> None:
    with pytest.raises(ValidationError, match="requires api_mode=responses"):
        ProviderSpec.model_validate(
            {
                "kind": "foundry_router",
                "model": "model-router",
                "base_url": "https://resource.services.ai.azure.com/openai/v1",
                "api_key_env": "AZURE_KEY",
            }
        )


def test_config_rejects_secret_headers_credentials_and_unknown_references(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="authentication headers"):
        spec(headers={"Authorization": "raw-secret"})
    with pytest.raises(ValidationError, match="credentials"):
        spec(base_url="https://user:pass@example.test/v1")
    with pytest.raises(ValidationError, match="unknown provider"):
        AppConfig(
            data_dir=tmp_path,
            tools=ToolConfig(roots=(tmp_path,)),
            verifier="missing",
        )


def test_paths_honor_home_xdg_and_config_overrides(tmp_path: Path) -> None:
    home = PolarisPaths.from_env({"POLARIS_HOME": str(tmp_path / "home")})
    assert home.config_file == (tmp_path / "home" / "config.json").resolve()
    xdg = PolarisPaths.from_env(
        {
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
        }
    )
    assert xdg.data_dir == (tmp_path / "data" / "polaris").resolve()
    assert xdg.config_file == (tmp_path / "config" / "polaris" / "config.json").resolve()
    overridden = PolarisPaths.from_env(
        {
            "POLARIS_HOME": str(tmp_path / "home"),
            "POLARIS_CONFIG": str(tmp_path / "custom.json"),
        }
    )
    assert overridden.config_file == (tmp_path / "custom.json").resolve()
