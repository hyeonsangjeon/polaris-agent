import { describe, expect, it, vi } from "vitest";
import { createClient } from "./client";
import type { Transport } from "./types";

describe("secure client", () => {
  it("delegates credentials by file path without exposing a bearer token", async () => {
    const transport: Transport = vi.fn(async () => ({
      status: 200,
      body: { status: "ok" },
    }));
    const client = createClient(
      { daemonUrl: "http://127.0.0.1:8765", tokenFile: "/secure/polaris.token" },
      transport,
    );

    await client.health();

    const request = vi.mocked(transport).mock.calls[0][0];
    expect(request).toEqual({
      daemonUrl: "http://127.0.0.1:8765",
      tokenFile: "/secure/polaris.token",
      method: "GET",
      path: "/health",
    });
    expect(request).not.toHaveProperty("token");
    expect(request).not.toHaveProperty("authorization");
    expect(JSON.stringify(client)).not.toMatch(/bearer|authorization|token-value/i);
  });

  it("uses exact memory, scheduler, and channel routes and payloads", async () => {
    const transport: Transport = vi.fn(async () => ({ status: 200, body: [] }));
    const client = createClient({ daemonUrl: "http://127.0.0.1:8765", tokenFile: "/token" }, transport);
    const scope = { profile_id: "incident team", subject_key: "prod/api" };
    const memoryBody = {
      ...scope,
      content: "Prefer explicit rollback steps",
      kind: "preference" as const,
      trust_level: "user_asserted" as const,
    };
    const revision = {
      ...scope,
      content: "Prefer explicit rollback steps with evidence",
      expected_revision: 2,
      expected_hash: "hash-2",
    };
    const schedule = { kind: "cron" as const, cron: "0 * * * *", timezone: "UTC" };
    const job = {
      name: "hourly",
      schedule,
      payload: {
        mode: "single" as const,
        request: { prompt: "health", provider: "ollama" },
        delivery: { platform: "slack" as const, channel_id: "C1" },
      },
      catchup_policy: "fire_once" as const,
      max_catchup: 1,
    };

    await client.memory(scope);
    await client.searchMemory(scope, "rollback now", 25);
    await client.addMemory(memoryBody);
    await client.reviseMemory("mem/id", revision);
    await client.removeMemory("mem/id", {
      ...scope,
      expected_revision: 3,
      expected_hash: "hash-3",
    });
    await client.previewJob({ schedule, after: "2026-01-01T00:00:00Z", count: 5 });
    await client.createJob(job);
    await client.jobs();
    await client.jobRuns();
    await client.pauseJob("job/id");
    await client.resumeJob("job/id");
    await client.cancelJob("job/id");
    await client.retryJobRun("run/id");
    await client.channelStatus();
    await client.unknownOutbox();
    await client.markOutboxSent("delivery/id", "confirmed in Slack");
    await client.retryOutbox("delivery/id", "recipient confirmed missing");

    const requests = vi.mocked(transport).mock.calls.map(([request]) => request);
    expect(requests).toEqual([
      expect.objectContaining({ method: "GET", path: "/v1/memory?profile_id=incident+team&subject_key=prod%2Fapi&include_tombstones=false" }),
      expect.objectContaining({ method: "GET", path: "/v1/memory/search?query=rollback+now&profile_id=incident+team&subject_key=prod%2Fapi&limit=25" }),
      expect.objectContaining({ method: "POST", path: "/v1/memory", body: memoryBody }),
      expect.objectContaining({ method: "PUT", path: "/v1/memory/mem%2Fid", body: revision }),
      expect.objectContaining({ method: "DELETE", path: "/v1/memory/mem%2Fid", body: expect.objectContaining({ expected_revision: 3, expected_hash: "hash-3" }) }),
      expect.objectContaining({ method: "POST", path: "/v1/jobs/preview", body: { schedule, after: "2026-01-01T00:00:00Z", count: 5 } }),
      expect.objectContaining({ method: "POST", path: "/v1/jobs", body: job }),
      expect.objectContaining({ method: "GET", path: "/v1/jobs" }),
      expect.objectContaining({ method: "GET", path: "/v1/jobs/runs" }),
      expect.objectContaining({ method: "POST", path: "/v1/jobs/job%2Fid/pause" }),
      expect.objectContaining({ method: "POST", path: "/v1/jobs/job%2Fid/resume" }),
      expect.objectContaining({ method: "POST", path: "/v1/jobs/job%2Fid/cancel" }),
      expect.objectContaining({ method: "POST", path: "/v1/jobs/runs/run%2Fid/retry" }),
      expect.objectContaining({ method: "GET", path: "/v1/channels/status" }),
      expect.objectContaining({ method: "GET", path: "/v1/channels/outbox/unknown" }),
      expect.objectContaining({ method: "POST", path: "/v1/channels/outbox/delivery%2Fid/mark-sent", body: { note: "confirmed in Slack" } }),
      expect.objectContaining({ method: "POST", path: "/v1/channels/outbox/delivery%2Fid/retry", body: { note: "recipient confirmed missing" } }),
    ]);
  });
});
