"""Strict, secret-free JSON configuration."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
    model_validator,
)

from .paths import PolarisPaths, default_paths

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
EnvName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
ProviderKind = Literal[
    "ollama",
    "openai_compatible",
    "azure_foundry",
    "foundry_router",
]


class StrictConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ProviderSpec(StrictConfigModel):
    kind: ProviderKind
    model: NonEmpty
    base_url: HttpUrl
    api_key_env: EnvName | None = None
    api_mode: Literal["chat_completions", "responses"] = "chat_completions"
    timeout_seconds: float = Field(default=30.0, gt=0)
    headers: dict[str, str] = Field(default_factory=dict)
    azure_auth: Literal["api_key", "entra"] | None = None
    entra_scope: NonEmpty = "https://ai.azure.com/.default"
    aliases: tuple[NonEmpty, ...] = ()

    @field_validator("base_url")
    @classmethod
    def valid_endpoint(cls, value: HttpUrl) -> HttpUrl:
        if value.username is not None or value.password is not None:
            raise ValueError("provider endpoint must not contain credentials")
        if value.query is not None or value.fragment is not None:
            raise ValueError("provider endpoint must not contain a query or fragment")
        return value

    @model_validator(mode="after")
    def validate_auth(self) -> ProviderSpec:
        lowered = {key.lower() for key in self.headers}
        if lowered & {"authorization", "api-key", "x-api-key"}:
            raise ValueError("authentication headers must use api_key_env, not config headers")
        if self.kind in {"azure_foundry", "foundry_router"}:
            auth = self.azure_auth or "api_key"
            if auth == "api_key" and self.api_key_env is None:
                raise ValueError("Azure api_key authentication requires api_key_env")
            if auth == "entra" and self.api_key_env is not None:
                raise ValueError("Azure Entra authentication cannot use api_key_env")
        elif self.azure_auth is not None:
            raise ValueError("azure_auth is only valid for Foundry providers")
        if self.kind == "foundry_router" and self.api_mode != "responses":
            raise ValueError("foundry_router requires api_mode=responses")
        if len(set(self.aliases)) != len(self.aliases):
            raise ValueError("provider aliases must be unique")
        return self


class ToolConfig(StrictConfigModel):
    roots: tuple[Path, ...] = Field(default_factory=lambda: (Path.cwd().resolve(),))
    searxng_url: HttpUrl | None = None
    allow_private_http: bool = False

    @field_validator("roots")
    @classmethod
    def valid_roots(cls, roots: tuple[Path, ...]) -> tuple[Path, ...]:
        result: list[Path] = []
        for root in roots:
            expanded = root.expanduser()
            if not expanded.is_absolute():
                raise ValueError(f"tool root must be absolute: {root}")
            resolved = expanded.resolve()
            if not resolved.exists() or not resolved.is_dir():
                raise ValueError(f"tool root must be an existing directory: {root}")
            result.append(resolved)
        if len(set(result)) != len(result):
            raise ValueError("tool roots must be unique")
        return tuple(result)

    @field_validator("searxng_url")
    @classmethod
    def valid_searxng_endpoint(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and (
            value.username is not None
            or value.password is not None
            or value.query is not None
            or value.fragment is not None
        ):
            raise ValueError("SearXNG endpoint must not contain credentials, query, or fragment")
        return value


class DaemonConfig(StrictConfigModel):
    host: NonEmpty = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    api_token_env: EnvName | None = None
    token_file: Path | None = None
    graceful_timeout_seconds: float = Field(default=30.0, gt=0)


class WorkerTemplate(StrictConfigModel):
    provider: NonEmpty
    role: NonEmpty
    instructions: NonEmpty = "Research the question and cite evidence."


class OfflinePolicy(StrictConfigModel):
    enabled: bool = False
    allowed_hosts: tuple[NonEmpty, ...] = ()
    allow_private_ips: bool = True


class AppConfig(StrictConfigModel):
    data_dir: Path = Field(default_factory=lambda: default_paths().data_dir)
    providers: dict[str, ProviderSpec] = Field(default_factory=dict)
    tools: ToolConfig = Field(default_factory=ToolConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    workers: tuple[WorkerTemplate, ...] = ()
    verifier: NonEmpty | None = None
    synthesizer: NonEmpty | None = None
    offline: OfflinePolicy = Field(default_factory=OfflinePolicy)

    @field_validator("data_dir")
    @classmethod
    def absolute_data_dir(cls, value: Path) -> Path:
        path = value.expanduser()
        if not path.is_absolute():
            raise ValueError("data_dir must be absolute")
        return path.resolve()

    @field_validator("providers")
    @classmethod
    def valid_provider_names(cls, value: dict[str, ProviderSpec]) -> dict[str, ProviderSpec]:
        for name in value:
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name) is None:
                raise ValueError(f"invalid provider name: {name!r}")
        return value

    @model_validator(mode="after")
    def validate_references_and_offline(self) -> AppConfig:
        aliases: dict[str, str] = {}
        for name, spec in self.providers.items():
            for alias in spec.aliases:
                if alias in self.providers or alias in aliases:
                    raise ValueError(f"duplicate provider alias: {alias!r}")
                aliases[alias] = name
        known = set(self.providers) | set(aliases)
        references = [worker.provider for worker in self.workers]
        references.extend(item for item in (self.verifier, self.synthesizer) if item is not None)
        missing = sorted(set(references) - known)
        if missing:
            raise ValueError(f"unknown provider reference: {missing[0]!r}")
        if self.offline.enabled:
            for name, spec in self.providers.items():
                if not _offline_endpoint_allowed(
                    str(spec.base_url),
                    self.offline.allowed_hosts,
                    self.offline.allow_private_ips,
                ):
                    raise ValueError(
                        f"offline policy rejects non-local provider endpoint {name!r}"
                    )
        return self

    @property
    def paths(self) -> PolarisPaths:
        return PolarisPaths(
            data_dir=self.data_dir,
            config_file=self.data_dir / "config.json",
            journal_file=self.data_dir / "journal.sqlite3",
            artifact_dir=self.data_dir / "artifacts",
            token_file=self.daemon.token_file or self.data_dir / "api-token",
        )


def _offline_endpoint_allowed(
    url: str, allowed_hosts: tuple[str, ...], allow_private_ips: bool
) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    allowed = {item.lower().rstrip(".") for item in allowed_hosts}
    if host in allowed or host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or (
        allow_private_ips and (address.is_private or address.is_link_local)
    )


def secret_from_env(name: str | None, env: dict[str, str] | None = None) -> str | None:
    if name is None:
        return None
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
        raise ValueError("secret environment variable name is invalid")
    value = (os.environ if env is None else env).get(name)
    return value if value else None


def load_config(path: str | Path | None = None) -> AppConfig:
    source = Path(path) if path is not None else default_paths().config_file
    try:
        payload = source.read_bytes()
    except FileNotFoundError:
        return AppConfig()
    return AppConfig.model_validate_json(payload)


def save_config(config: AppConfig, path: str | Path | None = None) -> Path:
    if not isinstance(config, AppConfig):
        raise TypeError("config must be an AppConfig")
    destination = Path(path) if path is not None else default_paths().config_file
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(
        config.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=False
    ).encode() + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
