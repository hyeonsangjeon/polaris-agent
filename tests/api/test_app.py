from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from polaris.artifacts import ArtifactStore
from polaris.config import AppConfig, MemoryConfig, ProviderSpec, ToolConfig
from polaris.daemon import create_app
from polaris.journal import Journal, RunStatus
from polaris.memory import MemoryScope, MemoryStore
from polaris.providers import CompletionResult, Message, Provider, ProviderConfig
from polaris.providers.base import JsonValue
from polaris.service import AgentService
from polaris.tools import ToolRegistry


class FakeProvider(Provider):
    def __init__(self) -> None:
        self.config = ProviderConfig("fake-model", "http://127.0.0.1:1")

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, object]] | None = None,
        response_schema: Mapping[str, object] | None = None,
    ) -> CompletionResult:
        return CompletionResult(Message("assistant", "done"), "fake-model")

    async def list_models(self) -> tuple[str, ...]:
        return ("fake-model",)

    async def doctor(self) -> Mapping[str, JsonValue]:
        return {"ok": True}

    async def aclose(self) -> None:
        pass


def make_service(
    root: Path,
    *,
    memory_profile: str = "default",
    env: dict[str, str] | None = None,
) -> tuple[AgentService, FakeProvider]:
    spec = ProviderSpec.model_validate(
        {
            "kind": "ollama",
            "model": "fake-model",
            "base_url": "http://127.0.0.1:1",
            "api_key_env": "FAKE_PROVIDER_KEY",
        }
    )
    config = AppConfig(
        data_dir=root,
        providers={
            "fake": spec,
            "router": ProviderSpec.model_validate(
                {
                    "kind": "foundry_router",
                    "model": "model-router",
                    "base_url": "https://resource.services.ai.azure.com/openai/v1",
                    "api_mode": "responses",
                    "azure_auth": "entra",
                }
            ),
        },
        tools=ToolConfig(roots=(root,)),
        memory=MemoryConfig(profile_id=memory_profile),
    )
    provider = FakeProvider()
    return (
        AgentService(
            config,
            journal=Journal(":memory:"),
            artifact_store=ArtifactStore(root / "artifacts"),
            providers={"fake": provider, "router": provider},
            tools=ToolRegistry(),
            env=env,
        ),
        provider,
    )


@asynccontextmanager
async def api_client(
    root: Path,
    *,
    memory_profile: str = "default",
    env: dict[str, str] | None = None,
) -> AsyncIterator[tuple[httpx.AsyncClient, AgentService]]:
    service, _ = make_service(root, memory_profile=memory_profile, env=env)
    transport = httpx.ASGITransport(app=create_app(service, "test-token"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, service
    await service.close()


@pytest.mark.asyncio
async def test_health_is_public_and_every_api_route_requires_auth(tmp_path: Path) -> None:
    async with api_client(tmp_path) as (client, _):
        assert (await client.get("/health")).status_code == 200
        response = await client.get("/v1/runs")
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_run_submit_list_detail_and_schema_errors(tmp_path: Path) -> None:
    headers = {"Authorization": "Bearer test-token"}
    async with api_client(tmp_path) as (client, _):
        invalid = await client.post(
            "/v1/runs/single",
            headers=headers,
            json={"prompt": "hello", "unexpected": True},
        )
        assert invalid.status_code == 422

        created = await client.post(
            "/v1/runs/single",
            headers=headers,
            json={"prompt": "hello", "provider": "fake", "schedule": False},
        )
        assert created.status_code == 202
        run_id = created.json()["id"]
        listed = await client.get("/v1/runs", headers=headers)
        detail = await client.get(f"/v1/runs/{run_id}", headers=headers)
        assert listed.json()[0]["id"] == run_id
        assert detail.json()["status"] == "created"
        missing = await client.get("/v1/runs/missing", headers=headers)
        assert missing.status_code == 404
        assert missing.json()["error"] == "not_found"


@pytest.mark.asyncio
async def test_single_run_uses_configured_memory_profile_when_omitted(
    tmp_path: Path,
) -> None:
    headers = {"Authorization": "Bearer test-token"}
    async with api_client(tmp_path, memory_profile="operations") as (client, _):
        created = await client.post(
            "/v1/runs/single",
            headers=headers,
            json={"prompt": "hello", "provider": "fake", "schedule": False},
        )

        assert created.status_code == 202
        assert created.json()["config"]["memory_scope"] == {
            "profile_id": "operations",
            "subject_key": "local",
        }


@pytest.mark.asyncio
async def test_approval_and_sse_resume(tmp_path: Path) -> None:
    headers = {"Authorization": "Bearer test-token"}
    async with api_client(tmp_path) as (client, service):
        run = await service.submit_single("hello", provider="fake", schedule=False)
        approval = service.journal.request_approval(run.id, request={"tool": "write"})
        decision = await client.post(
            f"/v1/approvals/{approval.id}",
            headers=headers,
            json={"decision": "approved", "decided_by": "test"},
        )
        assert decision.status_code == 200
        assert decision.json()["decision"] == "approved"
        await service.cancel(run.id)
        first_event = service.timeline(run.id)[0]
        response = await client.get(
            f"/v1/runs/{run.id}/events",
            headers={**headers, "Last-Event-ID": str(first_event.id)},
        )
        assert response.status_code == 200
        assert f"id: {first_event.id}\n" not in response.text
        assert "event:" in response.text
        assert service.get(run.id).status is RunStatus.CANCELLED


@pytest.mark.asyncio
async def test_auxiliary_endpoints_and_resume_cancel(tmp_path: Path) -> None:
    headers = {"Authorization": "Bearer test-token"}
    async with api_client(tmp_path) as (client, _):
        created = await client.post(
            "/v1/runs/single",
            headers=headers,
            json={"prompt": "hello", "provider": "fake", "schedule": False},
        )
        run_id = created.json()["id"]
        assert (await client.get("/v1/providers/doctor", headers=headers)).status_code == 200
        assert (await client.get("/v1/models", headers=headers)).json() == {
            "fake": ["fake-model"]
        }
        assert (await client.get("/v1/tools", headers=headers)).json() == {"tools": []}
        assert (
            await client.get(f"/v1/runs/{run_id}/timeline", headers=headers)
        ).status_code == 200
        assert (
            await client.get(f"/v1/runs/{run_id}/artifacts", headers=headers)
        ).json() == []
        resumed = await client.post(f"/v1/runs/{run_id}/resume", headers=headers)
        assert resumed.status_code == 200
        await __import__("asyncio").sleep(0)
        cancelled = await client.post(f"/v1/runs/{run_id}/cancel", headers=headers)
        assert cancelled.status_code == 200


@pytest.mark.asyncio
async def test_fanout_validation_and_last_event_id_error(tmp_path: Path) -> None:
    headers = {"Authorization": "Bearer test-token"}
    async with api_client(tmp_path) as (client, service):
        response = await client.post(
            "/v1/runs/fanout",
            headers=headers,
            json={
                "question": "question",
                "workers": [{"id": "one", "provider": "fake", "role": "researcher"}],
                "verifier": "fake",
                "synthesizer": "fake",
                "budget": {},
                "schedule": False,
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["id"]
        service.journal.mark_run_status(run_id, RunStatus.CANCELLED)
        invalid = await client.get(
            f"/v1/runs/{run_id}/events",
            headers={**headers, "Last-Event-ID": "invalid"},
        )
        assert invalid.status_code == 400


@pytest.mark.asyncio
async def test_foundry_router_submission_endpoint(tmp_path: Path) -> None:
    headers = {"Authorization": "Bearer " + "test-token"}
    async with api_client(tmp_path) as (client, service):
        response = await client.post(
            "/v1/runs/foundry-router",
            headers=headers,
            json={
                "question": "question",
                "provider": "router",
                "budget": {"call_limit": 3, "token_limit": 300},
                "schedule": False,
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["id"]
        children = [item for item in service.list_runs() if item.parent_run_id == run_id]
        assert len(children) == 1


@pytest.mark.asyncio
async def test_memory_jobs_and_channels_api_are_authenticated_and_strict(
    tmp_path: Path,
) -> None:
    headers = {"Authorization": "Bearer " + "test-token"}
    async with api_client(tmp_path) as (client, _):
        added = await client.post(
            "/v1/memory",
            headers=headers,
            json={
                "profile_id": "default",
                "subject_key": "local",
                "content": "prefers concise output",
                "kind": "preference",
                "trust_level": "verified",
                "provenance_run_id": "run-1",
                "provenance_session_id": "session-1",
                "provenance_message_id": "message-1",
            },
        )
        assert added.status_code == 201, added.text
        entry = added.json()
        searched = await client.get(
            "/v1/memory/search",
            headers=headers,
            params={"query": "concise"},
        )
        assert searched.json()[0]["entry"]["id"] == entry["id"]
        revised = await client.put(
            f"/v1/memory/{entry['id']}",
            headers=headers,
            json={
                "content": "prefers short output",
                "expected_revision": entry["revision"],
                "expected_hash": entry["content_hash"],
            },
        )
        assert revised.status_code == 200
        content_only = revised.json()
        assert content_only["kind"] == "preference"
        assert content_only["trust_level"] == "verified"
        assert content_only["provenance_run_id"] == "run-1"
        assert content_only["provenance_session_id"] == "session-1"
        assert content_only["provenance_message_id"] == "message-1"
        explicit = await client.put(
            f"/v1/memory/{entry['id']}",
            headers=headers,
            json={
                "content": "operator supplied replacement",
                "expected_revision": content_only["revision"],
                "expected_hash": content_only["content_hash"],
                "kind": "fact",
                "trust_level": "model_inferred",
                "provenance_run_id": None,
                "provenance_session_id": "session-2",
                "provenance_message_id": None,
            },
        )
        assert explicit.status_code == 200
        explicitly_revised = explicit.json()
        assert explicitly_revised["kind"] == "fact"
        assert explicitly_revised["trust_level"] == "model_inferred"
        assert explicitly_revised["provenance_run_id"] is None
        assert explicitly_revised["provenance_session_id"] == "session-2"
        assert explicitly_revised["provenance_message_id"] is None
        removed = await client.request(
            "DELETE",
            f"/v1/memory/{entry['id']}",
            headers=headers,
            json={"expected_revision": explicitly_revised["revision"]},
        )
        assert removed.json()["tombstoned"] is True

        preview = await client.post(
            "/v1/jobs/preview",
            headers=headers,
            json={
                "schedule": {
                    "kind": "cron",
                    "cron": "0 * * * *",
                    "timezone": "UTC",
                },
                "after": "2026-01-01T00:00:00Z",
                "count": 2,
            },
        )
        assert preview.status_code == 200
        assert len(preview.json()) == 2
        created = await client.post(
            "/v1/jobs",
            headers=headers,
            json={
                "name": "hourly",
                "schedule": {
                    "kind": "cron",
                    "cron": "0 * * * *",
                    "timezone": "UTC",
                },
                "payload": {
                    "mode": "single",
                    "request": {"prompt": "hello", "provider": "fake"},
                },
            },
        )
        assert created.status_code == 201, created.text
        job_id = created.json()["id"]
        assert (await client.get("/v1/jobs", headers=headers)).json()[0]["id"] == job_id
        paused = await client.post(f"/v1/jobs/{job_id}/pause", headers=headers)
        assert paused.json()["state"] == "paused"
        resumed = await client.post(f"/v1/jobs/{job_id}/resume", headers=headers)
        assert resumed.json()["state"] == "scheduled"
        cancelled = await client.post(f"/v1/jobs/{job_id}/cancel", headers=headers)
        assert cancelled.json()["state"] == "cancelled"
        assert (
            await client.post(f"/v1/jobs/{job_id}/pause", headers=headers)
        ).status_code == 409
        assert (
            await client.post(f"/v1/jobs/{job_id}/resume", headers=headers)
        ).status_code == 409
        assert (
            await client.get("/v1/channels/status", headers=headers)
        ).json()["telegram_enabled"] is False
        assert (
            await client.get("/v1/channels/outbox/unknown", headers=headers)
        ).json() == []


@pytest.mark.asyncio
async def test_memory_list_api_never_returns_tamper_or_new_runtime_secret(
    tmp_path: Path,
) -> None:
    secret = "new-runtime-secret-value"
    scope = MemoryScope("default", "local")
    with MemoryStore(tmp_path / "journal.sqlite3") as seed:
        secret_entry = seed.append(scope, f"credential={secret}")
        tampered_entry = seed.append(scope, "safe original")
    with sqlite3.connect(tmp_path / "journal.sqlite3") as connection:
        connection.execute(
            "UPDATE memory_entries SET content = ? WHERE id = ?",
            ("benign-looking database tamper", tampered_entry.id),
        )

    headers = {"Authorization": "Bearer test-token"}
    async with api_client(tmp_path, env={"FAKE_PROVIDER_KEY": secret}) as (client, _):
        response = await client.get("/v1/memory", headers=headers)
        token_response = await client.post(
            "/v1/memory",
            headers=headers,
            json={"content": "credential=test-token"},
        )

    serialized = response.text
    entries = {entry["id"]: entry for entry in response.json()}
    assert secret not in serialized
    assert "benign-looking database tamper" not in serialized
    assert entries[secret_entry.id]["blocked_reason"] == "secret:configured"
    assert "integrity:content_hash_mismatch" in entries[tampered_entry.id]["blocked_reason"]
    assert "test-token" not in token_response.text
    assert token_response.json()["blocked_reason"] == "secret:configured"
